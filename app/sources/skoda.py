"""Skoda approved-used source module.

Searches the stock locator behind https://www.skoda.co.uk/apps/stock/carSearch
and returns cars in the CarFinder standard listing format (see
``app/sources/__init__.py``).

The locator has a JSON API (``/apps/stock/210/en-GB/api/search``):

* filters use their own codes: Model=BIAR (Octavia Estate), Fuel=B (petrol),
  Transmission=H (manual), and price / mileage only accept fixed steps, so
  CarFinder sends the nearest step and then applies the exact limits itself;
* each car includes the plate, sale price, mileage, the real first
  registration date and the dealer's latitude/longitude (distance is worked
  out from the person's postcode, straight line).

Skoda's own body label is unreliable (it calls a Kamiq or Scala an "Estate"),
so body type comes from the model name.
"""
from __future__ import annotations

import json
import re
import time
from datetime import date
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlsplit

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

SOURCE_KEY = "skoda"
SOURCE_NAME = "Skoda Approved Used"
MAKES = ("Skoda",)

SITE_ROOT = "https://www.skoda.co.uk"
API_URL = f"{SITE_ROOT}/apps/stock/210/en-GB/api/search"
DETAIL_URL = f"{SITE_ROOT}/apps/stock/carDetail/{{id}}?CarType=U"
PAGE_SIZE = 50
MAX_PAGES = 10
MODEL_CACHE_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "cache" / "skoda_models.json"

# Model codes seen on the site (Sept 2026). New models are discovered
# automatically and cached in data/cache/skoda_models.json.
KNOWN_MODELS: dict[str, str] = {
    "BIAW": "Citigo", "BIBQ": "Elroq", "BIBL": "Enyaq", "BIBP": "Enyaq Coupé",
    "BIAQ": "Fabia Estate", "BIAJ": "Fabia Hatch", "BIBK": "Kamiq", "BIBM": "Karoq",
    "BIBH": "Kodiaq", "BIAR": "Octavia Estate", "BIAD": "Octavia Hatch",
    "BIAY": "Rapid Spaceback", "BIBI": "Scala", "BIAT": "Superb Estate",
    "BIAK": "Superb Hatch", "BIAZ": "Yeti",
}

FUEL_CODES = {"Petrol": "B", "Diesel": "D", "Electric": "E", "Hybrid": "H"}
GEAR_CODES = {"Manual": "H", "Automatic": "A"}
PRICE_STEPS = list(range(10000, 60001, 5000))
MILEAGE_STEPS = [5000, 10000, 20000, 30000, 40000, 50000, 60000, 70000, 80000, 90000, 100000, 125000]
FIRST_YEAR_FILTER = 2016

SUV_MODELS = ("kamiq", "karoq", "kodiaq", "enyaq", "elroq", "yeti", "epiq")
HATCH_MODELS = ("scala", "citigo", "rapid", "superb hatch", "fabia hatch", "octavia hatch")


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(round(float(str(value).replace(",", "").replace("£", ""))))
    except (TypeError, ValueError):
        return None


def _squash(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower().replace("é", "e"))


def body_type_for_model(model_name: str | None, site_body: str | None = None) -> str | None:
    name = (model_name or "").lower()
    if "estate" in name or "combi" in name:
        return "Estate"
    if any(m in name for m in HATCH_MODELS) or "hatch" in name:
        return "Hatchback"
    if any(m in name for m in SUV_MODELS):
        return "SUV"
    body = (site_body or "").strip()
    return body if body in {"Hatchback", "Estate", "SUV", "Saloon", "MPV"} else None


def step_down(value: int | None, steps: list[int]) -> int | None:
    """Largest step at or below value (for "from" filters)."""
    if value is None:
        return None
    below = [s for s in steps if s <= value]
    return below[-1] if below else None


def step_up(value: int | None, steps: list[int]) -> int | None:
    """Smallest step at or above value (for "to" filters)."""
    if value is None:
        return None
    above = [s for s in steps if s >= value]
    return above[0] if above else None


# ---------------------------------------------------------------------------
# Model codes
# ---------------------------------------------------------------------------

def _get(params: list[tuple[str, str]], timeout: int = 30) -> dict[str, Any]:
    response = requests.get(
        API_URL,
        params=params,
        headers={"User-Agent": "Mozilla/5.0 (CarFinder)", "Accept": "application/json"},
        timeout=timeout,
    )
    response.raise_for_status()
    data = response.json()
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(f"Skoda search: {data.get('error')}")
    return data


def load_model_codes() -> dict[str, str]:
    codes = dict(KNOWN_MODELS)
    try:
        cached = json.loads(MODEL_CACHE_PATH.read_text(encoding="utf-8"))
        if isinstance(cached, dict):
            codes.update({str(k): str(v) for k, v in cached.items()})
    except (OSError, json.JSONDecodeError):
        pass
    return codes


def discover_model_codes() -> dict[str, str]:
    """Ask the site for its current model codes and learn any new ones."""
    codes = load_model_codes()
    data = _get([("CarType", "U"), ("PageNo", "1"), ("PageSize", "1"), ("SortKey", "DATE_OFFER"), ("SortDirection", "1")])
    items = (((data.get("criteria") or {}).get("criterias") or {}).get("Model") or {}).get("possibleItems") or []
    for item in items:
        code = str(item.get("key") or "")
        if not code or code in codes:
            continue
        sample = _get([("CarType", "U"), ("PageNo", "1"), ("PageSize", "1"), ("SortKey", "DATE_OFFER"),
                       ("SortDirection", "1"), ("Model", code)])
        cars = (sample.get("results") or {}).get("cars") or []
        if cars and cars[0].get("model"):
            codes[code] = str(cars[0]["model"])
    MODEL_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    MODEL_CACHE_PATH.write_text(json.dumps(codes, ensure_ascii=False, indent=1), encoding="utf-8")
    return codes


def model_codes_for(model: str, codes: dict[str, str]) -> list[str]:
    """Every code whose model name contains what the user typed.

    "Octavia" -> Octavia Estate and Octavia Hatch; "Octavia Estate" -> just that.
    """
    wanted = _squash(model)
    if not wanted:
        return []
    return sorted(code for code, name in codes.items() if wanted in _squash(name))


# ---------------------------------------------------------------------------
# Car search -> API parameters
# ---------------------------------------------------------------------------

def build_params(car: dict[str, Any], model_codes: list[str]) -> list[tuple[str, str]]:
    pasted = str(car.get("search_url") or "").strip()
    if pasted:
        # A URL copied from the Skoda site: pass its filters straight through.
        skip = {"pageno", "pagesize", "sort", "sortkey", "sortdirection"}
        params = [(k, v) for k, v in parse_qsl(urlsplit(pasted).query) if k.lower() not in skip]
        if not any(k == "CarType" for k, _ in params):
            params.append(("CarType", "U"))
        return params

    params: list[tuple[str, str]] = [("CarType", "U")]
    params.extend(("Model", code) for code in model_codes)
    fuel = FUEL_CODES.get(str(car.get("fuel") or "Any"))
    if fuel:
        params.append(("Fuel", fuel))
    gear = GEAR_CODES.get(str(car.get("transmission") or "Any"))
    if gear:
        params.append(("Transmission", gear))
    low = step_down(car.get("price_min"), PRICE_STEPS)
    if low:
        params.append(("PriceFrom", str(low)))
    high = step_up(car.get("price_max"), PRICE_STEPS)
    if high:
        params.append(("PriceTo", str(high)))
    miles = step_up(car.get("mileage_max"), MILEAGE_STEPS)
    if miles:
        params.append(("KilometersTo", str(miles)))
    year = car.get("year_min")
    if year and FIRST_YEAR_FILTER <= int(year) <= date.today().year:
        params.append(("YearOfRegistrationFrom", str(int(year))))
    return params


# ---------------------------------------------------------------------------
# Skoda car -> standard listing
# ---------------------------------------------------------------------------

def car_to_row(obj: dict[str, Any], home: tuple[float, float] | None) -> dict[str, Any] | None:
    reg = normalise_plate(obj.get("plate"))
    if not reg:
        return None
    tech = obj.get("technicalData") or {}
    dealer = obj.get("dealer") or {}
    images = obj.get("images") or {}
    photo_count = obj.get("imageCount") if obj.get("imageCount") is not None else len(images)
    if photo_count and int(photo_count) > 1:
        photo_status, photo_reason = "photos", f"{photo_count} images on the Skoda listing"
    elif photo_count == 1:
        photo_status, photo_reason = "awaiting", "Only one image on the Skoda listing; treating as stock/awaiting"
    else:
        photo_status, photo_reason = "unknown", "No image count on the Skoda listing"

    dealer_location = None
    if dealer.get("latitude") is not None and dealer.get("longitude") is not None:
        dealer_location = (float(dealer["latitude"]), float(dealer["longitude"]))
    model = str(obj.get("model") or "").strip() or None
    trim = str(obj.get("title") or obj.get("subTitle") or "").strip() or None
    first_reg = str(obj.get("initialReg") or "")[:10] or None
    year = _int(obj.get("modelYear")) or (_int(first_reg[:4]) if first_reg else None)
    previous = (obj.get("strikethroughPrice") or {}).get("value") if isinstance(obj.get("strikethroughPrice"), dict) else None
    title = " ".join(str(p) for p in (year, "Skoda", model, trim, reg) if p)

    return standardise({
        "registration": reg,
        "make": "Skoda",
        "model": model,
        "trim": trim,
        "year": year,
        "first_registered": first_reg,
        "colour": ((obj.get("colors") or {}).get("exterior") or {}).get("text"),
        "fuel": tech.get("fuel") or obj.get("fuelTypeCode"),
        "transmission": tech.get("gear"),
        "body_type": body_type_for_model(model, obj.get("modelBody")),
        "seats": None,
        "mileage": _int((obj.get("mileage") or {}).get("value")),
        "price": _int((obj.get("salePrice") or {}).get("value")),
        "previous_price": _int(previous),
        "dealer": str(dealer.get("name") or "").strip() or None,
        "location": str(dealer.get("city") or "").strip() or None,
        "distance_miles": distance_miles(home, dealer_location),
        "url": DETAIL_URL.format(id=obj.get("id")) if obj.get("id") else SITE_ROOT + "/apps/stock/carSearch?CarType=U",
        "photo_status": photo_status,
        "photo_count": photo_count,
        "photo_reason": photo_reason,
        "title": title,
        "raw_text": json.dumps(obj, ensure_ascii=False, sort_keys=True)[:20000],
        "source": SOURCE_KEY,
        "status": "active",
        "last_seen": now_iso(),
    })


def is_wanted(row: dict[str, Any], obj: dict[str, Any], car: dict[str, Any]) -> bool:
    """Exact car-search rules, applied after the site's own (coarser) filters."""
    if str(obj.get("brand") or "BI") != "BI":
        return False  # part-exchanged other makes that Skoda dealers also list
    wanted_model = _squash(car.get("model"))
    if wanted_model and wanted_model not in _squash(row.get("model")):
        return False
    wanted_trim = str(car.get("trim") or "").strip().lower()
    if wanted_trim and str(row.get("trim") or "").strip().lower() != wanted_trim:
        return False
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
    power_kw = _int(((obj.get("technicalData") or {}).get("motor") or {}).get("value"))
    if power_kw is not None and car.get("power_min") and power_kw * 1.36 < int(car["power_min"]):
        return False  # Skoda gives kW; the car search uses PS
    return body_and_seats_match(car, row.get("body_type"), row.get("seats"))


def extract_rows(data: dict[str, Any], car: dict[str, Any], home: tuple[float, float] | None) -> tuple[list[dict[str, Any]], set[str]]:
    rows: list[dict[str, Any]] = []
    raw_regs: set[str] = set()
    for obj in (data.get("results") or {}).get("cars") or []:
        reg = normalise_plate(obj.get("plate"))
        if reg:
            raw_regs.add(reg)
        row = car_to_row(obj, home)
        if row and is_wanted(row, obj, car):
            rows.append(row)
    return rows, raw_regs


def search(car: dict[str, Any], settings: dict[str, Any], timings: dict[str, Any] | None = None) -> SearchResult:
    """Run one car search against Skoda Approved Used."""
    codes = load_model_codes()
    model_codes: list[str] = []
    if car.get("model") and not car.get("search_url"):
        model_codes = model_codes_for(str(car["model"]), codes)
        if not model_codes:
            model_codes = model_codes_for(str(car["model"]), discover_model_codes())
        if not model_codes:
            raise RuntimeError(f"Skoda has no model matching '{car['model']}' in used stock right now")

    params = build_params(car, model_codes)
    home = postcode_location(settings.get("home_postcode"))
    result = SearchResult(search_url=f"{SITE_ROOT}/apps/stock/carSearch?" + "&".join(f"{k}={v}" for k, v in params))
    seen: set[str] = set()
    pages_log: list[dict[str, Any]] = []
    debug: list[str] = []

    for page in range(1, MAX_PAGES + 1):
        started = time.perf_counter()
        data = _get(params + [("PageNo", str(page)), ("PageSize", str(PAGE_SIZE)),
                              ("SortKey", "PRICE_SALE"), ("SortDirection", "0")])
        fetch_seconds = time.perf_counter() - started
        if timings is not None:
            timings["search_fetch_seconds"] = round(float(timings.get("search_fetch_seconds") or 0) + fetch_seconds, 3)
        rows, raw_regs = extract_rows(data, car, home)
        result.raw_regs.update(raw_regs)
        for row in rows:
            if row["registration"] not in seen:
                seen.add(row["registration"])
                result.rows.append(row)
        info = (data.get("results") or {}).get("pageInfo") or {}
        cars_on_page = len((data.get("results") or {}).get("cars") or [])
        pages_log.append({"search": car.get("name"), "page": page, "fetch_seconds": round(fetch_seconds, 3),
                          "vehicle_objects": cars_on_page, "rows": len(rows), "raw_regs": len(raw_regs)})
        debug.append(json.dumps({"page": page, "pageInfo": info, "cars": cars_on_page}))
        if cars_on_page < PAGE_SIZE or page >= int(info.get("pageCount") or 1):
            break

    if timings is not None:
        timings.setdefault("pages", []).extend(pages_log)
    result.debug_text = "\n".join(debug)
    result.debug_html = ""
    if home is None and settings.get("home_postcode"):
        result.debug_text += "\nPostcode lookup failed: distances unknown."
    return result
