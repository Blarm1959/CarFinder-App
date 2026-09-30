"""Audi approved-used source module.

Searches the service behind https://www.audi.co.uk/en/used-car-search/ :
``https://scs.audi.de/api/v2/search/filter/ukuc/en`` (market ``ukuc`` = UK used).

* the site sends a fixed public client key in a ``Token`` header (it is part of
  the audi.co.uk page, not a login);
* filters are ``filter=carline.a4avant,fuel.D,gear-type.manual`` (values of the
  same kind are OR-ed); price and mileage ranges are not accepted, so
  CarFinder sorts by price and applies the exact limits itself;
* paging uses ``from`` and ``size``;
* each car has the plate, retail price, mileage, real first-registration date,
  body type (``avant`` = estate) and the dealer's latitude/longitude.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any

import requests

from app.db import now_iso
from app.geo import distance_miles, postcode_location
from app.sources import (
    SearchResult,
    body_and_seats_match,
    fuel_matches,
    normalise_plate,
    standardise,
    transmission_matches,
)

SOURCE_KEY = "audi"
SOURCE_NAME = "Audi Approved Used"
MAKES = ("Audi",)

API_ROOT = "https://scs.audi.de/api"
SEARCH_URL = f"{API_ROOT}/v2/search/filter/ukuc/en"
CARLINES_URL = f"{API_ROOT}/v1/structure/carlines/ukuc/en"
# Public client key sent by audi.co.uk's own used-car search page.
CLIENT_TOKEN = "FJ54W6H"
PAGE_SIZE = 50
MAX_PAGES = 10
CACHE_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "cache" / "audi_carlines.json"

BODY_TYPES = {
    "body-type.avant": "Estate",
    "body-type.allroad-quattro": "Estate",
    "body-type.limousine": "Saloon",
    "body-type.sportback": "Hatchback",
    "body-type.suv": "SUV",
}
FUEL_FILTERS = {"Petrol": "fuel.B", "Diesel": "fuel.D"}
GEAR_FILTERS = {"Manual": "gear-type.manual", "Automatic": "gear-type.automatic"}


def _squash(text: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(text or "").lower().replace("é", "e"))


def _headers() -> dict[str, str]:
    return {"Accept": "application/json", "Token": CLIENT_TOKEN, "User-Agent": "Mozilla/5.0 (CarFinder)"}


def _get(url: str, params: list[tuple[str, str]] | None = None, timeout: int = 30) -> dict[str, Any]:
    response = requests.get(url, params=params or [], headers=_headers(), timeout=timeout)
    if response.status_code in (401, 403) or "Token" in response.text[:60]:
        raise RuntimeError("Audi search refused the request: audi.co.uk may have changed its client key")
    response.raise_for_status()
    return response.json()


# ---------------------------------------------------------------------------
# Models ("carlines")
# ---------------------------------------------------------------------------

def load_carlines(refresh: bool = False) -> dict[str, str]:
    """carline id -> name, e.g. "a4avant" -> "A4 Avant"."""
    if not refresh:
        try:
            cached = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
            if isinstance(cached, dict) and cached:
                return cached
        except (OSError, json.JSONDecodeError):
            pass
    data = _get(CARLINES_URL)
    carlines = {str(v.get("id")): str(v.get("text") or v.get("id"))
                for v in (data.get("carlines") or {}).values() if isinstance(v, dict) and v.get("id")}
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(carlines, ensure_ascii=False, indent=1), encoding="utf-8")
    return carlines


def carlines_for(model: str, carlines: dict[str, str]) -> list[str]:
    """Carlines whose name starts with what the user typed.

    "A4" -> A4 Avant, A4 Saloon, A4 allroad; "Q5 Sportback" -> just that.
    Starting-with (not contains) keeps "A4" from matching "RS 4" or "S4".
    """
    wanted = _squash(model)
    if not wanted:
        return []
    matches = []
    for code, name in carlines.items():
        plain = _squash(re.sub(r"^new\s+", "", name, flags=re.I))
        if plain.startswith(wanted) or _squash(code).startswith(wanted):
            matches.append(code)
    return sorted(matches)


# ---------------------------------------------------------------------------
# Car search -> API parameters
# ---------------------------------------------------------------------------

def build_filter(car: dict[str, Any], carline_codes: list[str]) -> str:
    parts = [f"carline.{code}" for code in carline_codes]
    fuel = FUEL_FILTERS.get(str(car.get("fuel") or "Any"))
    if fuel:
        parts.append(fuel)
    gear = GEAR_FILTERS.get(str(car.get("transmission") or "Any"))
    if gear:
        parts.append(gear)
    if not carline_codes and car.get("body_type") == "Estate":
        parts.extend(["body-type.avant", "body-type.allroad-quattro"])
    return ",".join(parts)


# ---------------------------------------------------------------------------
# Audi car -> standard listing
# ---------------------------------------------------------------------------

def car_to_row(v: dict[str, Any], home: tuple[float, float] | None) -> dict[str, Any] | None:
    used = v.get("used") or {}
    reg = normalise_plate(used.get("numberplate"))
    if not reg:
        return None
    retail = next((p for p in v.get("typedPrices") or [] if p.get("type") == "retail"), None)
    price = int(round(float(retail["amount"]))) if retail and retail.get("amount") is not None else None
    first_reg = None
    if used.get("initialRegistrationDate"):
        first_reg = time.strftime("%Y-%m-%d", time.gmtime(int(used["initialRegistrationDate"]) / 1000))
    carline_name = str((v.get("symbolicCarline") or {}).get("description") or "").strip()
    model = re.sub(r"^Audi\s+", "", carline_name) or None
    trim = str((v.get("trimline") or {}).get("description") or "").strip() or None
    dealer = v.get("dealer") or {}
    geo = dealer.get("geoLocation") or {}
    dealer_location = (float(geo["lat"]), float(geo["lon"])) if geo.get("lat") is not None and geo.get("lon") is not None else None
    photos = [p for p in v.get("pictures") or [] if p.get("type") == "photo"]
    if len(photos) > 1:
        photo_status, photo_reason = "photos", f"{len(photos)} dealer photos"
    elif len(photos) == 1:
        photo_status, photo_reason = "awaiting", "Only one dealer photo; treating as stock/awaiting"
    else:
        photo_status, photo_reason = "awaiting", "No dealer photos yet"
    power_ps = None
    match = re.search(r"\((\d+)\s*PS\)", str(v.get("powerDisplay") or ""))
    if match:
        power_ps = int(match.group(1))
    year = int(first_reg[:4]) if first_reg else (int(v["modelYear"]) if v.get("modelYear") else None)
    title = " ".join(str(p) for p in (year, "Audi", model, trim, reg) if p)
    row = standardise({
        "registration": reg,
        "make": "Audi",
        "model": model,
        "trim": trim,
        "year": year,
        "first_registered": first_reg,
        "colour": (v.get("extColor") or {}).get("description") or (v.get("topColor") or {}).get("description"),
        "fuel": (v.get("fuel") or {}).get("description"),
        "transmission": (v.get("gearType") or {}).get("description"),
        "body_type": BODY_TYPES.get(str((v.get("bodyType") or {}).get("code") or "")),
        "seats": None,
        "mileage": int(used["mileage"]) if used.get("mileage") is not None else None,
        "price": price,
        "previous_price": None,
        "dealer": str(dealer.get("name") or "").strip() or None,
        "location": str(dealer.get("city") or "").strip() or None,
        "distance_miles": distance_miles(home, dealer_location),
        "url": v.get("weblink") or v.get("entryUrl"),
        "photo_status": photo_status,
        "photo_count": len(photos),
        "photo_reason": photo_reason,
        "title": title,
        "raw_text": json.dumps({k: v.get(k) for k in ("carId", "symbolicCarline", "trimline", "bodyType", "fuel",
                                                      "gearType", "typedPrices", "used", "dealer", "powerDisplay")},
                               ensure_ascii=False, sort_keys=True)[:20000],
        "source": SOURCE_KEY,
        "status": "active",
        "last_seen": now_iso(),
    })
    row["_power_ps"] = power_ps
    return row


def is_wanted(row: dict[str, Any], car: dict[str, Any], power_ps: int | None) -> bool:
    wanted_model = _squash(car.get("model"))
    if wanted_model and not _squash(row.get("model")).startswith(wanted_model):
        return False
    wanted_trim = str(car.get("trim") or "").strip().lower()
    if wanted_trim and str(row.get("trim") or "").strip().lower() != wanted_trim:
        return False
    if not fuel_matches(str(car.get("fuel") or "Any"), str(row.get("fuel") or "")):
        return False
    if not transmission_matches(str(car.get("transmission") or "Any"), str(row.get("transmission") or "")):
        return False
    price, mileage, year = row.get("price"), row.get("mileage"), row.get("year")
    if price is None:
        return False  # finance-only listings have no cash price
    if car.get("price_min") is not None and price < int(car["price_min"]):
        return False
    if car.get("price_max") is not None and price > int(car["price_max"]):
        return False
    if mileage is not None and car.get("mileage_max") is not None and mileage > int(car["mileage_max"]):
        return False
    if year is not None and car.get("year_min") is not None and year < int(car["year_min"]):
        return False
    if power_ps is not None and car.get("power_min") and power_ps < int(car["power_min"]):
        return False
    return body_and_seats_match(car, row.get("body_type"), row.get("seats"))


def extract_rows(data: dict[str, Any], car: dict[str, Any], home: tuple[float, float] | None
                 ) -> tuple[list[dict[str, Any]], set[str], int]:
    rows: list[dict[str, Any]] = []
    raw_regs: set[str] = set()
    vehicles = data.get("vehicleBasic") or []
    for v in vehicles:
        row = car_to_row(v, home)
        if not row:
            continue
        raw_regs.add(row["registration"])
        power_ps = row.pop("_power_ps", None)
        if is_wanted(row, car, power_ps):
            rows.append(row)
    return rows, raw_regs, len(vehicles)


def search(car: dict[str, Any], settings: dict[str, Any], timings: dict[str, Any] | None = None) -> SearchResult:
    carline_codes: list[str] = []
    if car.get("model"):
        carline_codes = carlines_for(str(car["model"]), load_carlines())
        if not carline_codes:
            carline_codes = carlines_for(str(car["model"]), load_carlines(refresh=True))
        if not carline_codes:
            raise RuntimeError(f"Audi has no model matching '{car['model']}' in used stock right now")
    filter_text = build_filter(car, carline_codes)
    home = postcode_location(settings.get("home_postcode"))
    result = SearchResult(search_url="https://www.audi.co.uk/en/used-car-search/" + (f"#filter={filter_text}" if filter_text else ""))
    seen: set[str] = set()
    pages_log: list[dict[str, Any]] = []
    for page in range(MAX_PAGES):
        params = [("size", str(PAGE_SIZE)), ("from", str(page * PAGE_SIZE)), ("sort", "prices.retail:asc")]
        if filter_text:
            params.append(("filter", filter_text))
        started = time.perf_counter()
        data = _get(SEARCH_URL, params)
        fetch_seconds = time.perf_counter() - started
        if timings is not None:
            timings["search_fetch_seconds"] = round(float(timings.get("search_fetch_seconds") or 0) + fetch_seconds, 3)
        rows, raw_regs, count = extract_rows(data, car, home)
        result.raw_regs.update(raw_regs)
        for row in rows:
            if row["registration"] not in seen:
                seen.add(row["registration"])
                result.rows.append(row)
        pages_log.append({"search": car.get("name"), "page": page + 1, "fetch_seconds": round(fetch_seconds, 3),
                          "vehicle_objects": count, "rows": len(rows), "raw_regs": len(raw_regs),
                          "total": data.get("totalCount")})
        if count < PAGE_SIZE or (page + 1) * PAGE_SIZE >= int(data.get("totalCount") or 0):
            break
    if timings is not None:
        timings.setdefault("pages", []).extend(pages_log)
    result.debug_text = "\n".join(json.dumps(p) for p in pages_log)
    return result
