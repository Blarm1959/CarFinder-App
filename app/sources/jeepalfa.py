"""Jeep and Alfa Romeo approved-used source via Spoticar UK.

Both brands are part of Stellantis' manufacturer-approved Spoticar stock.  The
existing Spoticar parser is reused, while this module supplies the additional
brand routes and keeps brand-code checking deliberately tolerant because the
site has used more than one code family over time.
"""
from __future__ import annotations

import json
import math
import random
import re
import time
from typing import Any

from bs4 import BeautifulSoup

from app.geo import postcode_location
from app.sources import SearchResult, body_and_seats_match
from app.sources import spoticar

SOURCE_KEY = "jeepalfa"
SOURCE_NAME = "Spoticar (Jeep and Alfa Romeo Approved Used)"
MAKES = ("Jeep", "Alfa Romeo")
BRANDS = {
    "Jeep": ("jeep", ("JEE", "JEEP")),
    "Alfa Romeo": ("alfa-romeo", ("ALF", "ALFA", "ARO")),
}
KNOWN_MODELS = {
    "Jeep": ["avenger", "compass", "grand cherokee", "renegade", "wrangler"],
    "Alfa Romeo": ["giulia", "giulietta", "junior", "stelvio", "tonale"],
}


def _plain(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def make_key(make: Any) -> str | None:
    wanted = _plain(make)
    for name in MAKES:
        if _plain(name) == wanted:
            return name
    return None


def model_matches(wanted: str, name: str | None) -> bool:
    typed, have = _plain(wanted), _plain(name)
    return not typed or have.startswith(typed) or typed in have


def models_for(model: str, known: list[str]) -> list[str]:
    if not _plain(model):
        return []
    return [name for name in known if model_matches(model, name)]


def card_wanted(c: dict[str, Any], car: dict[str, Any], make: str) -> bool:
    code = str(c.get("brand_code") or "").upper()
    # Do not reject unknown/changed Spoticar brand codes; model/make page filtering
    # still constrains the results.  Reject only an obvious known code for another make.
    known_other = {x for m, (_, codes) in BRANDS.items() if m != make for x in codes}
    if code and code in known_other:
        return False
    if not c.get("used") or c.get("kind") not in (None, "", "VP"):
        return False
    if car.get("model") and not model_matches(str(car["model"]), c.get("model")):
        return False
    wanted_trim = str(car.get("trim") or "").strip().lower()
    if wanted_trim and wanted_trim not in f"{c.get('trim') or ''} {c.get('label') or ''}".lower():
        return False
    if not spoticar.fuel_matches(str(car.get("fuel") or "Any"), str(c.get("fuel") or "")):
        return False
    if not spoticar.transmission_matches(str(car.get("transmission") or "Any"), str(c.get("transmission") or "")):
        return False
    price, mileage, year = c.get("price"), c.get("mileage"), c.get("year")
    if price is None:
        return False
    if car.get("price_min") is not None and price < int(car["price_min"]): return False
    if car.get("price_max") is not None and price > int(car["price_max"]): return False
    if mileage is not None and car.get("mileage_max") is not None and mileage > int(car["mileage_max"]): return False
    if year is not None and car.get("year_min") is not None and year < int(car["year_min"]): return False
    return True


def search(car: dict[str, Any], settings: dict[str, Any], timings: dict[str, Any] | None = None) -> SearchResult:
    make = make_key(car.get("make"))
    if not make:
        raise RuntimeError(f"Jeep/Alfa source does not search make '{car.get('make')}'")
    brand_value = BRANDS[make][0]
    model_names: list[str] = []
    if car.get("model"):
        model_names = models_for(str(car["model"]), KNOWN_MODELS[make])
        if not model_names:
            first_soup = BeautifulSoup(spoticar._get(spoticar.build_url([("brand", brand_value)], 1)), "lxml")
            site_models = [str(i.get("value")) for i in first_soup.select('input[name="model"]') if i.get("value")]
            model_names = models_for(str(car["model"]), site_models)
        if not model_names:
            raise RuntimeError(f"Spoticar has no {make} model matching '{car['model']}' right now")

    filters = spoticar.build_filters(car, brand_value, model_names)
    home = postcode_location(settings.get("home_postcode"))
    result = SearchResult(search_url=spoticar.build_url(filters, 1))
    seen: set[str] = set(); seen_ids: set[str] = set(); logs: list[dict[str, Any]] = []
    pages = 1; page = 1
    while page <= min(pages, spoticar.MAX_PAGES):
        if page > 1:
            time.sleep(spoticar.PAGE_DELAY + random.uniform(0, spoticar.PAGE_JITTER))
        started = time.perf_counter()
        html = spoticar._get(spoticar.build_url(filters, page))
        secs = time.perf_counter() - started
        cards, total = spoticar.parse_page(html, make)
        if page == 1 and spoticar.looks_blocked(html, cards, total):
            raise RuntimeError(f"Spoticar returned a page with no car list ({len(html)} bytes), probably a bot check")
        if total is not None:
            pages = max(1, math.ceil(total / spoticar.PAGE_SIZE))
        new_cards = [c for c in cards if c.get("id") not in seen_ids]
        added = 0
        for c in new_cards:
            seen_ids.add(str(c.get("id")))
            if c.get("registration"):
                result.raw_regs.add(c["registration"])
            if not card_wanted(c, car, make):
                continue
            row = spoticar.card_to_row(c, make, home)
            if not row or row["registration"] in seen:
                continue
            if body_and_seats_match(car, row.get("body_type"), row.get("seats")):
                seen.add(row["registration"]); result.rows.append(row); added += 1
        logs.append({"search": car.get("name"), "page": page, "fetch_seconds": round(secs, 3),
                     "vehicle_objects": len(cards), "rows": added, "total": total})
        if not new_cards or len(cards) < spoticar.PAGE_SIZE:
            break
        page += 1
    if timings is not None:
        timings["search_fetch_seconds"] = round(float(timings.get("search_fetch_seconds") or 0) + sum(x["fetch_seconds"] for x in logs), 3)
        timings.setdefault("pages", []).extend(logs)
    result.debug_text = "\n".join(json.dumps(x) for x in logs)
    return result
