"""Shared engine for VW Group "VTP" stock APIs (SEAT and Cupra UK).

SEAT and Cupra use the same Vehicle Trading Platform service,
``https://vtpapi.seat.com/restapi/v1/<stock>/search/car``:

* filters are "matrix" parameters on the path, e.g. ``;t_model=BHBK;t_petr=B``;
  price and mileage only accept fixed steps, so CarFinder sends the nearest
  step and then applies the exact limits itself;
* paging and sorting are request headers (X-Page, X-Page-Items, X-Sort,
  X-Sort-Direction) plus a site header (X-Pattern);
* each car's details are a list of ``{"key", "value"}`` items: numberplate,
  model, prices, mileage, initialreg (real first registration), seat (number
  of seats), and the dealer with latitude/longitude.

``app/sources/seat.py`` and ``app/sources/cupra.py`` are thin wrappers that
pass their brand settings to :func:`search`.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from datetime import date
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

CACHE_DIR = Path(__file__).resolve().parent.parent.parent / "data" / "cache"
PAGE_SIZE = 50
MAX_PAGES = 10

FUEL_CODES = {"Petrol": "B", "Diesel": "D", "Electric": "E", "Hybrid": "H"}
GEAR_CODES = {"Manual": "H", "Automatic": "A"}
PRICE_STEPS = list(range(5000, 75001, 5000))
MILEAGE_STEPS = [5000, 10000, 15000, 20000, 25000, 30000, 40000, 50000, 80000, 100000, 125000, 150000, 200000]


@dataclass(frozen=True)
class Brand:
    key: str                 # source key, e.g. "seat"
    make: str                # shown to users, e.g. "SEAT"
    api_base: str            # .../restapi/v1/<stock>
    pattern: str             # X-Pattern header
    detail_url: str          # advert link, with {key}
    known_models: dict[str, str]
    body_by_model: dict[str, str]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _squash(text: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(text or "").lower())


def _int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(round(float(str(value).replace(",", "").replace("£", ""))))
    except (TypeError, ValueError):
        return None


def items_to_dict(items: list[dict[str, Any]] | None) -> dict[str, dict[str, Any]]:
    return {str(i.get("key")): i for i in items or [] if isinstance(i, dict)}


def sub_value(item: dict[str, Any] | None, key: str) -> Any:
    for value in (item or {}).get("values") or []:
        if value.get("key") == key:
            return value
    return None


def step_down(value: int | None, steps: list[int]) -> int | None:
    if value is None:
        return None
    below = [s for s in steps if s <= value]
    return below[-1] if below else None


def step_up(value: int | None, steps: list[int]) -> int | None:
    if value is None:
        return None
    above = [s for s in steps if s >= value]
    return above[0] if above else None


def body_type_for(brand: Brand, model: str | None) -> str | None:
    name = (model or "").lower()
    if "estate" in name or "sportstourer" in name.replace(" ", ""):
        return "Estate"
    for word, body in brand.body_by_model.items():
        if word in name:
            return body
    return None


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _get(brand: Brand, matrix: str, page: int = 1, page_items: int = PAGE_SIZE, timeout: int = 30) -> dict[str, Any]:
    response = requests.get(
        f"{brand.api_base}/search/car{matrix}",
        headers={
            "X-Pattern": brand.pattern,
            "X-Page": str(page),
            "X-Page-Items": str(page_items),
            "X-Sort": "PRICE_SALE",
            "X-Sort-Direction": "ASC",
            "Accept": "application/json",
            "Accept-Language": "en-GB",
            "User-Agent": "Mozilla/5.0 (CarFinder)",
        },
        timeout=timeout,
    )
    response.raise_for_status()
    return response.json()


def cars_in(data: dict[str, Any]) -> list[dict[str, Any]]:
    result = ((data.get("results") or {}).get("result") or {})
    return [entry.get("car") or {} for entry in result.get("cars") or []]


# ---------------------------------------------------------------------------
# Model codes
# ---------------------------------------------------------------------------

def _cache_path(brand: Brand) -> Path:
    return CACHE_DIR / f"{brand.key}_models.json"


def load_model_codes(brand: Brand) -> dict[str, str]:
    codes = dict(brand.known_models)
    try:
        cached = json.loads(_cache_path(brand).read_text(encoding="utf-8"))
        if isinstance(cached, dict):
            codes.update({str(k): str(v) for k, v in cached.items()})
    except (OSError, json.JSONDecodeError):
        pass
    return codes


def discover_model_codes(brand: Brand) -> dict[str, str]:
    codes = load_model_codes(brand)
    data = _get(brand, "", page_items=1)
    criterias = ((data.get("criteria") or {}).get("search") or {}).get("criterias") or []
    model_criteria = next((c for c in criterias if (c.get("criteria") or {}).get("key") == "t_model"), {})
    for item in model_criteria.get("possibleItems") or []:
        code = str(item.get("key") or "")
        if code and code not in codes:
            cars = cars_in(_get(brand, f";t_model={code}", page_items=1))
            name = (items_to_dict(cars[0].get("items")).get("model") or {}).get("value") if cars else None
            if name:
                codes[code] = str(name)
    path = _cache_path(brand)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(codes, ensure_ascii=False, indent=1), encoding="utf-8")
    return codes


def model_codes_for(model: str, codes: dict[str, str]) -> list[str]:
    """Codes whose model name contains what the user typed ("Leon" -> Leon 5dr + Leon Estate)."""
    wanted = _squash(model)
    if not wanted:
        return []
    return sorted(code for code, name in codes.items() if wanted in _squash(name))


# ---------------------------------------------------------------------------
# Car search -> matrix filters
# ---------------------------------------------------------------------------

def build_matrix(car: dict[str, Any], model_code: str | None) -> str:
    parts: list[str] = []
    if model_code:
        parts.append(f"t_model={model_code}")
    fuel = FUEL_CODES.get(str(car.get("fuel") or "Any"))
    if fuel:
        parts.append(f"t_petr={fuel}")
    gear = GEAR_CODES.get(str(car.get("transmission") or "Any"))
    if gear:
        parts.append(f"t_gear={gear}")
    low = step_down(car.get("price_min"), PRICE_STEPS)
    if low:
        parts.append(f"t_pe_fr={low}")
    high = step_up(car.get("price_max"), PRICE_STEPS)
    if high:
        parts.append(f"t_pe_to={high}")
    miles = step_up(car.get("mileage_max"), MILEAGE_STEPS)
    if miles:
        parts.append(f"t_km_to={miles}")
    year = car.get("year_min")
    if year and 2005 <= int(year) <= date.today().year:
        parts.append(f"t_ez_fr={int(year)}")
    return "".join(f";{p}" for p in parts)


# ---------------------------------------------------------------------------
# VTP car -> standard listing
# ---------------------------------------------------------------------------

def car_to_row(brand: Brand, car_obj: dict[str, Any], home: tuple[float, float] | None) -> dict[str, Any] | None:
    items = items_to_dict(car_obj.get("items"))
    reg = normalise_plate((items.get("numberplate") or {}).get("value"))
    if not reg:
        return None

    model = str((items.get("model") or {}).get("value") or "").strip() or None
    trim = str((items.get("smod") or {}).get("value") or "").strip() or None
    first_reg = str((items.get("initialreg") or {}).get("value") or "")[:10] or None
    motor = items.get("motor")
    fuel = (sub_value(motor, "fuel") or {}).get("value")
    power_ps = _int((sub_value(motor, "power.ps") or {}).get("value"))
    sale = sub_value(items.get("prices"), "sale") or {}
    listed = sub_value(items.get("prices"), "list") or {}
    price = _int(sale.get("raw_value") if sale.get("raw_value") is not None else sale.get("value"))
    previous = _int(listed.get("raw_value"))
    mileage_item = items.get("mileage") or {}
    mileage = _int(mileage_item.get("raw_value") if mileage_item.get("raw_value") is not None else mileage_item.get("value"))
    seats = _int((items.get("seat") or {}).get("value"))

    colour = None
    exterior = sub_value(items.get("color"), "exterior")
    marketing = sub_value(exterior, "marketing")
    if marketing:
        colour = (sub_value(marketing, "out") or {}).get("value")
    colour = colour or (sub_value(exterior, "generic") or {}).get("value")

    dealer_items = items_to_dict(((car_obj.get("hypermediadealer") or {}).get("dealer") or {}).get("items"))
    position = dealer_items.get("position")
    dealer_location = None
    lat, lon = (sub_value(position, "latitude") or {}).get("value"), (sub_value(position, "longitude") or {}).get("value")
    if lat and lon:
        try:
            dealer_location = (float(lat), float(lon))
        except ValueError:
            dealer_location = None

    photo_count = 0
    for group in car_obj.get("images") or []:
        image_group = group.get("imageGroup") or {}
        if (image_group.get("key") or group.get("key")) == "dealerImages":
            photo_count = len(image_group.get("images") or [])
    if photo_count > 1:
        photo_status, photo_reason = "photos", f"{photo_count} dealer images"
    elif photo_count == 1:
        photo_status, photo_reason = "awaiting", "Only one dealer image; treating as stock/awaiting"
    else:
        photo_status, photo_reason = "awaiting", "No dealer images yet"

    year = _int(first_reg[:4]) if first_reg else None
    title = " ".join(str(p) for p in (year, brand.make, model, trim, reg) if p)
    key = car_obj.get("key")
    row = standardise({
        "registration": reg,
        "make": brand.make,
        "model": model,
        "trim": trim,
        "year": year,
        "first_registered": first_reg,
        "colour": colour,
        "fuel": fuel,
        "transmission": (items.get("gear") or {}).get("value"),
        "body_type": body_type_for(brand, model),
        "seats": seats,
        "mileage": mileage,
        "price": price,
        "previous_price": previous if previous and price and previous > price else None,
        "dealer": str((dealer_items.get("name") or {}).get("value") or "").strip() or None,
        "location": str((dealer_items.get("city") or {}).get("value") or "").strip() or None,
        "distance_miles": distance_miles(home, dealer_location),
        "url": brand.detail_url.format(key=key) if key else None,
        "photo_status": photo_status,
        "photo_count": photo_count,
        "photo_reason": photo_reason,
        "title": title,
        "raw_text": json.dumps(car_obj, ensure_ascii=False, sort_keys=True)[:20000],
        "source": brand.key,
        "status": "active",
        "last_seen": now_iso(),
    })
    row["_power_ps"] = power_ps  # used by is_wanted, removed before saving
    return row


def is_wanted(row: dict[str, Any], car: dict[str, Any], power_ps: int | None) -> bool:
    wanted_model = _squash(car.get("model"))
    if wanted_model and wanted_model not in _squash(row.get("model")):
        return False
    wanted_trim = str(car.get("trim") or "").strip().lower()
    if wanted_trim and wanted_trim not in str(row.get("trim") or "").lower():
        return False  # VTP trims read like "FR / FR Sport", so "FR" matches
    if not fuel_matches(str(car.get("fuel") or "Any"), str(row.get("fuel") or "")):
        return False
    if not transmission_matches(str(car.get("transmission") or "Any"), str(row.get("transmission") or "")):
        return False
    price, mileage, year = row.get("price"), row.get("mileage"), row.get("year")
    if price is not None:
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


def extract_rows(brand: Brand, data: dict[str, Any], car: dict[str, Any], home: tuple[float, float] | None
                 ) -> tuple[list[dict[str, Any]], set[str], int]:
    rows: list[dict[str, Any]] = []
    raw_regs: set[str] = set()
    cars = cars_in(data)
    for car_obj in cars:
        row = car_to_row(brand, car_obj, home)
        if not row:
            continue
        raw_regs.add(row["registration"])
        power_ps = row.pop("_power_ps", None)
        if is_wanted(row, car, power_ps):
            rows.append(row)
    return rows, raw_regs, len(cars)


def search(brand: Brand, car: dict[str, Any], settings: dict[str, Any], timings: dict[str, Any] | None = None) -> SearchResult:
    model_codes: list[str | None] = [None]
    if car.get("model"):
        codes = model_codes_for(str(car["model"]), load_model_codes(brand))
        if not codes:
            codes = model_codes_for(str(car["model"]), discover_model_codes(brand))
        if not codes:
            raise RuntimeError(f"{brand.make} has no model matching '{car['model']}' in used stock right now")
        model_codes = list(codes)

    home = postcode_location(settings.get("home_postcode"))
    result = SearchResult(search_url=f"{brand.api_base}/search/car" + build_matrix(car, model_codes[0]))
    seen: set[str] = set()
    pages_log: list[dict[str, Any]] = []
    for code in model_codes:
        matrix = build_matrix(car, code)
        for page in range(1, MAX_PAGES + 1):
            started = time.perf_counter()
            data = _get(brand, matrix, page=page)
            fetch_seconds = time.perf_counter() - started
            if timings is not None:
                timings["search_fetch_seconds"] = round(float(timings.get("search_fetch_seconds") or 0) + fetch_seconds, 3)
            rows, raw_regs, count = extract_rows(brand, data, car, home)
            result.raw_regs.update(raw_regs)
            for row in rows:
                if row["registration"] not in seen:
                    seen.add(row["registration"])
                    result.rows.append(row)
            pages_log.append({"search": car.get("name"), "model_code": code, "page": page,
                              "fetch_seconds": round(fetch_seconds, 3), "vehicle_objects": count,
                              "rows": len(rows), "raw_regs": len(raw_regs)})
            if count < PAGE_SIZE:
                break
    if timings is not None:
        timings.setdefault("pages", []).extend(pages_log)
    result.debug_text = "\n".join(json.dumps(p) for p in pages_log)
    return result
