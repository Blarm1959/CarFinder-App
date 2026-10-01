"""Toyota (Toyota Approved Used) source module.

Searches the used-car stock shown at https://www.toyota.co.uk/used-vehicles-in-stock.
The page is a Toyota Europe web component that asks a JSON API for results:

* ``POST https://usc-webcomponents.toyota-europe.com/v1/api/usedcars/results/gb/en?brand=toyota``
  with a JSON body (UK distributor code 94081, ``filterContext`` "used");
* ``offset`` counts cars (not pages) and ``resultCount`` can be up to 100;
* filters are ``{"filterId": ..., "valueIds": [...]}`` for lists (brand, model, fuel,
  gearbox) or ``{"filterId": ..., "min": .., "max": ..}`` for ranges; the price
  range filter is called ``cash`` (``usedCarPrice`` is silently ignored);
* Toyota dealers also list other makes they took in part exchange, so the brand
  filter (Toyota = "38") is always sent and the brand is checked again per car;
* every car record carries the registration, VIN, price, mileage, first
  registration date, colour, seats and the dealer's name, town and
  latitude/longitude, so distance is the straight line from the home postcode;
* vans and pick-ups (``carType`` "CV", e.g. Hilux, Proace, Corolla Commercial) are skipped.

An unknown filter value is ignored by the API rather than refused, so every
limit is applied again here, as for Spoticar.  Pages are fetched with curl_cffi
(Chrome TLS handshake) when it is installed, in case the API is put behind a bot
check like Akamai; otherwise with plain requests.
"""
from __future__ import annotations

import json
import random
import re
import time
from typing import Any

import requests

try:  # Same approach as Spoticar: a real Chrome TLS handshake if a bot check appears.
    from curl_cffi import requests as cffi_requests
except ImportError:  # pragma: no cover - depends on what is installed
    cffi_requests = None

from app.db import now_iso
from app.geo import distance_miles, postcode_location
from app.sources import (
    SearchResult,
    body_and_seats_match,
    classify_body_type,
    fuel_matches,
    normalise_plate,
    standardise,
    transmission_matches,
)

SOURCE_KEY = "toyota"
SOURCE_NAME = "Toyota Approved Used"
MAKES = ("Toyota",)

SITE_ROOT = "https://www.toyota.co.uk"
LIST_PATH = "/used-vehicles-in-stock"
API_URL = "https://usc-webcomponents.toyota-europe.com/v1/api/usedcars/results/gb/en?brand=toyota"
DISTRIBUTOR_CODE = "94081"
BRAND_ID = "38"
PAGE_SIZE = 100
MAX_PAGES = 40
SORT_ORDER = "cashAsc"
IMPERSONATE = "chrome"
PAGE_DELAY = 1.0
PAGE_JITTER = 1.0
RETRY_WAITS = (10.0, 30.0)

# The API's used-car model codes for Toyota (Oct 2026) and the model names it reports.
KNOWN_MODELS: dict[str, str] = {
    "AS": "Avensis", "AU": "Auris", "AX": "Aygo X", "AY": "Aygo", "BZ": "bZ4X Touring", "CB": "C-HR+",
    "CH": "C-HR", "CM": "Camry", "CO": "Corolla", "CR": "Corolla HB/TS", "CTS": "Corolla Touring Sports",
    "DE": "bZ4X", "GR": "GR86", "GT": "GT86", "GY": "GR Yaris", "HI": "Highlander", "HL": "Hilux", "IQ": "iQ",
    "LC": "Land Cruiser", "PA": "Proace", "PB": "Proace Verso", "PD": "Proace City", "PF": "Proace City Verso",
    "PI": "Prius", "PM": "Proace Max", "RA": "RAV4", "RE": "RAV4 PHEV", "SU": "Supra", "UB": "Urban Cruiser",
    "VE": "Verso", "YA": "Yaris", "YB": "Yaris Cross", "YG": "Yaris GRMN",
}

FUEL_VALUES = {"Petrol": ["1"], "Diesel": ["2"], "Hybrid": ["5", "6"], "Electric": ["3"]}
GEAR_VALUES = {"Manual": ["MT"], "Automatic": ["AT"]}

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-GB,en;q=0.9",
    "Content-Type": "application/json",
    "Origin": SITE_ROOT,
    "Referer": SITE_ROOT + LIST_PATH,
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
}


def _plain(text: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(text or "").lower())


def _int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        digits = re.sub(r"[^0-9]", "", str(value))
        return int(digits) if digits else None


def model_name(name: Any) -> str:
    """"Toyota C-HR" -> "C-HR" (some model names carry the make)."""
    text = str(name or "").strip()
    return text[7:].strip() if text.lower().startswith("toyota ") else text


def model_matches(wanted: str, name: str | None) -> bool:
    """True when a model name starts with what the user typed ("Yaris" matches "Yaris Cross")."""
    typed = _plain(model_name(wanted))
    return not typed or _plain(model_name(name)).startswith(typed)


def model_codes_for(model: str, known: dict[str, str]) -> list[str]:
    if not _plain(model):
        return []
    return [code for code, name in known.items() if model_matches(model, name)]


# ---------------------------------------------------------------------------
# Car search -> API request
# ---------------------------------------------------------------------------

def build_filters(car: dict[str, Any], model_codes: list[str]) -> list[dict[str, Any]]:
    filters: list[dict[str, Any]] = [{"filterId": "usedCarBrand", "valueIds": [BRAND_ID]}]
    if model_codes:
        filters.append({"filterId": "usedCarModel", "valueIds": model_codes})
    if car.get("price_min") is not None or car.get("price_max") is not None:
        filters.append({"filterId": "cash", "min": _int(car.get("price_min")), "max": _int(car.get("price_max"))})
    if car.get("mileage_max") is not None:
        filters.append({"filterId": "usedCarMileage", "min": None, "max": int(car["mileage_max"])})
    if car.get("year_min") is not None:
        filters.append({"filterId": "usedCarYear", "min": int(car["year_min"]), "max": None})
    fuel = FUEL_VALUES.get(str(car.get("fuel") or "Any"))
    if fuel:
        filters.append({"filterId": "usedCarFuelType", "valueIds": fuel})
    gear = GEAR_VALUES.get(str(car.get("transmission") or "Any"))
    if gear:
        filters.append({"filterId": "usedCarTransmission", "valueIds": gear})
    return filters


def build_body(filters: list[dict[str, Any]], offset: int, count: int = PAGE_SIZE) -> dict[str, Any]:
    return {"uscEnv": "production", "filters": filters, "filterContext": "used", "offset": offset,
            "resultCount": count, "sortOrder": SORT_ORDER, "distributorCode": DISTRIBUTOR_CODE}


_session: Any = None


def _fetcher() -> str:
    return "curl_cffi (Chrome)" if cffi_requests is not None else "plain requests (curl_cffi not installed)"


def _make_session() -> Any:
    if cffi_requests is not None:
        return cffi_requests.Session(impersonate=IMPERSONATE)
    session = requests.Session()
    session.headers.update(HEADERS)
    return session


def _http() -> Any:
    global _session
    if _session is None:
        _session = _make_session()
    return _session


def _post(body: dict[str, Any], timeout: int = 30) -> dict[str, Any]:
    """POST a results query; on a 403 wait, start a fresh session and try again (RETRY_WAITS)."""
    global _session
    headers = {k: v for k, v in HEADERS.items() if k != "User-Agent"} if cffi_requests is not None else None
    attempts = len(RETRY_WAITS) + 1
    for attempt in range(attempts):
        response = _http().post(API_URL, data=json.dumps(body), headers=headers, timeout=timeout)
        if response.status_code != 403:
            response.raise_for_status()
            try:
                data = response.json()
            except ValueError as exc:
                raise RuntimeError(f"Toyota returned something that is not car data ({len(response.text)} bytes), "
                                   f"probably a bot check; fetched with {_fetcher()}") from exc
            if not isinstance(data, dict) or "results" not in data:
                raise RuntimeError(f"Toyota returned an unexpected answer (no results list); fetched with {_fetcher()}")
            return data
        if attempt < len(RETRY_WAITS):
            _session = None
            time.sleep(RETRY_WAITS[attempt])
    server = response.headers.get("Server") or "the site"
    raise RuntimeError(f"Toyota blocked the request (HTTP 403 from {server}, {attempts} tries; "
                       f"fetched with {_fetcher()})")


def site_model_codes(model: str) -> list[str]:
    """Model codes the API offers right now whose names match (for models added after KNOWN_MODELS)."""
    data = _post(build_body([{"filterId": "usedCarBrand", "valueIds": [BRAND_ID]}], 0, 1))
    codes = [str(a.get("valueId")) for a in (data.get("aggregations") or {}).get("usedCarModel") or []]
    found: list[str] = []
    for code in codes:
        if code in KNOWN_MODELS or not code:
            continue
        time.sleep(0.3)
        probe = _post(build_body([{"filterId": "usedCarBrand", "valueIds": [BRAND_ID]},
                                  {"filterId": "usedCarModel", "valueIds": [code]}], 0, 1))
        first = (probe.get("results") or [None])[0]
        name = (((first or {}).get("product") or {}).get("model") or {}).get("description")
        if name and model_matches(model, name):
            found.append(code)
    return found


# ---------------------------------------------------------------------------
# API record -> car
# ---------------------------------------------------------------------------

def body_for(body_type: str | None, model: str | None) -> str | None:
    """Body type from the record ("Compact SUV", "Touring Sport", "5dr", "Crossover")."""
    text = f" {body_type or ''} {model or ''} ".lower()
    if any(w in text for w in ("coupe", "coupé", "convertible", "cab ", "pick-up", "van ")):
        return None
    if any(w in text for w in ("touring", "wagon", "estate")):
        return "Estate"
    if any(w in text for w in ("suv", "crossover", "sports utility", "4x4")):
        return "SUV"
    if any(w in text for w in ("mpv", "people carrier", "verso")):
        return "MPV"
    if any(w in text for w in ("saloon", "sedan")):
        return "Saloon"
    if "hatch" in text or re.search(r"\b[35] ?d(oo)?r\b", text):
        return "Hatchback"
    return classify_body_type(body_type, model)


def colour_name(text: Any) -> str | None:
    """"Pure White (Solid Paint)" -> "Pure White"."""
    colour = re.sub(r"\s*\(.*?\)\s*", " ", str(text or "")).strip()
    return colour or None


def parse_record(rec: dict[str, Any]) -> dict[str, Any]:
    """Everything CarFinder needs from one API record (no filtering)."""
    product = rec.get("product") or {}
    engine = product.get("engine") or {}
    gearbox = product.get("transmission") or {}
    dealer = rec.get("dealer") or {}
    geo = dealer.get("geoLocation") or {}
    mileage = rec.get("mileage") or {}
    miles = _int(mileage.get("value"))
    if miles is not None and str(((mileage.get("unit") or {}).get("description") or "")).lower() == "km":
        miles = int(round(miles * 0.621371))
    first_reg = str(((rec.get("history") or {}).get("registrationDate")) or "")[:10] or None
    built = str(rec.get("productionDate") or "")[:10] or None
    year_text = first_reg or built
    model = model_name((product.get("model") or {}).get("description"))
    trim = str(product.get("versionName") or "").strip()
    if model and trim.lower().startswith(model.lower() + " "):
        trim = trim[len(model):].strip()  # "Yaris Cross Icon FWD" -> "Icon FWD"
    try:
        lat_lon = (float(geo["lat"]), float(geo["lon"]))
    except (KeyError, TypeError, ValueError):
        lat_lon = None
    car_id = rec.get("id")
    return {
        "id": car_id,
        "brand_code": str((product.get("brand") or {}).get("code") or ""),
        "brand": (product.get("brand") or {}).get("description"),
        "registration": normalise_plate(rec.get("licensePlate")),
        "vin": rec.get("vin"),
        "model": model or None,
        "model_code": (product.get("model") or {}).get("code"),
        "trim": trim or None,
        "year": _int(year_text[:4]) if year_text else None,
        "first_registered": first_reg,
        "mileage": miles,
        "price": _int((rec.get("price") or {}).get("sellingPriceInclVAT")) or None,
        "fuel": (engine.get("marketingFuelType") or {}).get("description")
        or (engine.get("fuelType") or {}).get("description"),
        "transmission": (gearbox.get("transmissionType") or {}).get("description") or gearbox.get("name"),
        "body_type": body_for(product.get("bodyType"), model),
        "body_text": product.get("bodyType"),
        "seats": _int(product.get("seats")),
        "colour": colour_name(rec.get("exteriorColour")),
        "photo_count": len(rec.get("images") or []),
        "dealer": dealer.get("name"),
        "location": (dealer.get("address") or {}).get("city"),
        "lat_lon": lat_lon,
        "url": f"{SITE_ROOT}{LIST_PATH}/pdp.toyota-{_plain(model) or 'car'}-{car_id}" if car_id else None,
        "available": str((rec.get("vehicleStatus") or {}).get("code") or "1") == "1",
        "car_type": str((product.get("carType") or {}).get("code") or ""),
    }


def record_wanted(c: dict[str, Any], car: dict[str, Any]) -> bool:
    if c.get("brand_code") and c["brand_code"] != BRAND_ID:
        return False
    if not c.get("available") or c.get("car_type") == "CV":
        return False
    if car.get("model") and not model_matches(str(car["model"]), c.get("model")):
        return False
    wanted_trim = str(car.get("trim") or "").strip().lower()
    if wanted_trim and wanted_trim not in str(c.get("trim") or "").lower():
        return False
    if not fuel_matches(str(car.get("fuel") or "Any"), str(c.get("fuel") or "")):
        return False
    if not transmission_matches(str(car.get("transmission") or "Any"), str(c.get("transmission") or "")):
        return False
    price, mileage, year = c.get("price"), c.get("mileage"), c.get("year")
    if price is None:
        return False
    if car.get("price_min") is not None and price < int(car["price_min"]):
        return False
    if car.get("price_max") is not None and price > int(car["price_max"]):
        return False
    if mileage is not None and car.get("mileage_max") is not None and mileage > int(car["mileage_max"]):
        return False
    if year is not None and car.get("year_min") is not None and year < int(car["year_min"]):
        return False
    return True


def record_to_row(c: dict[str, Any], home: tuple[float, float] | None) -> dict[str, Any] | None:
    reg = c.get("registration")
    if not reg:
        return None
    photo_count = int(c.get("photo_count") or 0)
    if photo_count > 1:
        photo_status, photo_reason = "photos", f"{photo_count} dealer images"
    elif photo_count == 1:
        photo_status, photo_reason = "awaiting", "Only one image; treating as stock/awaiting"
    else:
        photo_status, photo_reason = "awaiting", "No dealer images yet"
    title = " ".join(str(p) for p in (c.get("year"), "Toyota", c.get("model"), c.get("trim"), reg) if p)
    raw = {k: c.get(k) for k in ("id", "vin", "model_code", "body_text", "first_registered", "mileage", "price",
                                 "dealer", "location", "lat_lon", "url")}
    return standardise({
        "registration": reg,
        "make": "Toyota",
        "model": c.get("model"),
        "trim": c.get("trim"),
        "year": c.get("year"),
        "first_registered": c.get("first_registered"),
        "colour": c.get("colour"),
        "fuel": c.get("fuel"),
        "transmission": c.get("transmission"),
        "body_type": c.get("body_type"),
        "seats": c.get("seats"),
        "mileage": c.get("mileage"),
        "price": c.get("price"),
        "previous_price": None,
        "dealer": c.get("dealer"),
        "location": c.get("location"),
        "distance_miles": distance_miles(home, c.get("lat_lon")) if c.get("lat_lon") else None,
        "url": c.get("url") or SITE_ROOT + LIST_PATH,
        "photo_status": photo_status,
        "photo_count": photo_count,
        "photo_reason": photo_reason,
        "title": title,
        "raw_text": json.dumps(raw, ensure_ascii=False),
        "source": SOURCE_KEY,
        "status": "active",
        "last_seen": now_iso(),
    })


def search(car: dict[str, Any], settings: dict[str, Any], timings: dict[str, Any] | None = None) -> SearchResult:
    if _plain(car.get("make")) != "toyota":
        raise RuntimeError(f"Toyota source does not search make '{car.get('make')}'")
    model_codes: list[str] = []
    if car.get("model"):
        model_codes = model_codes_for(str(car["model"]), KNOWN_MODELS) or site_model_codes(str(car["model"]))
        if not model_codes:
            raise RuntimeError(f"Toyota has no model matching '{car['model']}' right now")

    filters = build_filters(car, model_codes)
    home = postcode_location(settings.get("home_postcode"))
    result = SearchResult(search_url=SITE_ROOT + LIST_PATH)
    seen: set[str] = set()
    seen_ids: set[str] = set()
    pages_log: list[dict[str, Any]] = []
    total: int | None = None
    page = 0
    while page < MAX_PAGES:
        if page:
            time.sleep(PAGE_DELAY + random.uniform(0, PAGE_JITTER))
        started = time.perf_counter()
        try:
            data = _post(build_body(filters, page * PAGE_SIZE))
        except RuntimeError as exc:
            raise RuntimeError(f"{exc} on results page {page + 1}") from exc
        fetch_seconds = time.perf_counter() - started
        if timings is not None:
            timings["search_fetch_seconds"] = round(float(timings.get("search_fetch_seconds") or 0) + fetch_seconds, 3)
        total = _int(data.get("totalResultCount"))
        records = [parse_record(r) for r in data.get("results") or [] if isinstance(r, dict)]
        new = [c for c in records if c.get("id") not in seen_ids]
        rows = 0
        for c in new:
            seen_ids.add(str(c.get("id")))
            if c.get("registration") and c.get("brand_code") in ("", BRAND_ID):
                result.raw_regs.add(c["registration"])
            if not record_wanted(c, car):
                continue
            row = record_to_row(c, home)
            if not row or row["registration"] in seen:
                continue
            if body_and_seats_match(car, row.get("body_type"), row.get("seats")):
                seen.add(row["registration"])
                result.rows.append(row)
                rows += 1
        pages_log.append({"search": car.get("name"), "page": page + 1, "fetch_seconds": round(fetch_seconds, 3),
                          "vehicle_objects": len(records), "rows": rows, "total": total})
        page += 1
        if not new or len(records) < PAGE_SIZE or (total is not None and page * PAGE_SIZE >= total):
            break
    if timings is not None:
        timings.setdefault("pages", []).extend(pages_log)
    result.debug_text = "\n".join(json.dumps(p) for p in pages_log)
    return result
