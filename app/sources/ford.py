"""Ford Direct approved-used source.

Ford UK's public approved-used search calls the eUsed API at
``www.servicescache.ford.com``.  Each request carries two public browser-generated
EUSL headers:

* ``x-eusl-consumer: b-gux_approved_used-prod``;
* ``x-eusl-k`` = base64(``<milliseconds>:<16 random bytes as hex>``).

The header algorithm is the same one shipped to every visitor in Ford's
``guxfoeApprovedUsed.js`` bundle, so no login, cookie or copied token is needed.
A fresh token is generated for every API request.
"""
from __future__ import annotations

import base64
import json
import re
import secrets
import time
from datetime import date
from typing import Any

import requests

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

SOURCE_KEY = "ford"
SOURCE_NAME = "Ford Direct approved used"
MAKES = ("Ford",)

PUBLIC_PAGE = "https://www.ford.co.uk/shop/direct-used/cars"
RESULTS_PAGE = "https://secure.ford.co.uk/shop/price-and-locate/approved-used/direct-used-cars/results"
API_ROOT = "https://www.servicescache.ford.com/api/eUsed/v1"
SEARCH_OPTIONS_URL = API_ROOT + "/searchOptions"
SEARCH_VEHICLES_URL = API_ROOT + "/searchVehicles"

CONSUMER = "b-gux_approved_used-prod"
PAGE_SIZE = 48
MAX_PAGES = 50
RETRY_WAITS = (5.0, 15.0)

# Current Ford Direct model names seen in October 2026.  The live search-options
# endpoint is preferred, so this is only a fallback if that lookup is unavailable.
KNOWN_MODELS: tuple[str, ...] = ("Capri", "Focus", "Kuga", "Puma")

BASE_HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-GB,en;q=0.9",
    "Content-Type": "application/json;charset=UTF-8",
    "Origin": "https://secure.ford.co.uk",
    "Referer": "https://secure.ford.co.uk/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/154.0.0.0 Safari/537.36"
    ),
}

_session: requests.Session | None = None


def _plain(value: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(value or "").lower())


def _int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        digits = re.sub(r"[^0-9]", "", str(value))
        return int(digits) if digits else None


def _float(value: Any) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _first_value(container: Any, key: str = "value") -> str | None:
    if isinstance(container, list):
        for item in container:
            if isinstance(item, dict) and item.get(key) not in (None, ""):
                return str(item[key]).strip() or None
    return None


def _model_key(value: Any) -> str:
    key = _plain(value)
    return key[4:] if key.startswith("ford") and len(key) > 4 else key


def model_matches(wanted: str, name: str | None) -> bool:
    typed, have = _model_key(wanted), _model_key(name)
    return not typed or have.startswith(typed) or typed in have


def models_for(model: str, known: tuple[str, ...] | list[str]) -> list[str]:
    if not _plain(model):
        return []
    return [name for name in known if model_matches(model, name)]


def _eusl_headers() -> dict[str, str]:
    """Ford's public guxfoeApprovedUsed.js EUSL header generation."""
    timestamp = int(time.time() * 1000)
    nonce = secrets.token_hex(16)
    token = base64.b64encode(f"{timestamp}:{nonce}".encode("ascii")).decode("ascii")
    return {**BASE_HEADERS, "x-eusl-consumer": CONSUMER, "x-eusl-k": token}


def _http() -> requests.Session:
    global _session
    if _session is None:
        _session = requests.Session()
    return _session


def _request(method: str, url: str, *, timeout: int = 60, params: dict[str, Any] | None = None,
             payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """One Ford API request, regenerating EUSL headers for every retry."""
    global _session
    response = None
    for attempt in range(len(RETRY_WAITS) + 1):
        response = _http().request(method, url, params=params, json=payload, headers=_eusl_headers(), timeout=timeout)
        if response.status_code not in (403, 429):
            response.raise_for_status()
            try:
                data = response.json()
            except ValueError as exc:
                raise RuntimeError("Ford returned something that is not car data") from exc
            status = data.get("status") if isinstance(data, dict) else None
            if not isinstance(data, dict) or not isinstance(status, dict) or status.get("statusCode") != 200:
                raise RuntimeError(f"Ford eUsed API returned an unexpected answer ({status or 'no status'})")
            return data
        if attempt < len(RETRY_WAITS):
            _session = None
            time.sleep(RETRY_WAITS[attempt])
    code = response.status_code if response is not None else "?"
    raise RuntimeError(f"Ford blocked the eUsed request (HTTP {code} after {len(RETRY_WAITS) + 1} tries)")


def parse_site_models(data: dict[str, Any]) -> tuple[str, ...]:
    models: list[str] = []
    for vehicle_type in (data or {}).get("vehicles") or []:
        if not isinstance(vehicle_type, dict):
            continue
        if str(vehicle_type.get("description") or "").lower() not in ("", "personal"):
            continue
        for item in vehicle_type.get("models") or []:
            if not isinstance(item, dict):
                continue
            name = str(item.get("searchTerm") or item.get("description") or "").strip()
            if name and name not in models:
                models.append(name)
    return tuple(models)


def site_models() -> tuple[str, ...]:
    data = _request("GET", SEARCH_OPTIONS_URL, timeout=30,
                    params={"locale": "en_GB", "vehicleType": "Personal"})
    payload = data.get("data") if isinstance(data, dict) else None
    return parse_site_models(payload if isinstance(payload, dict) else {})


def build_search(car: dict[str, Any], models: list[str], home: tuple[float, float] | None,
                 settings: dict[str, Any], starting_record: int = 0) -> dict[str, Any]:
    """Build the same JSON shape as Ford's Approved Used browser application."""
    low = _int(car.get("price_min"))
    high = _int(car.get("price_max"))
    mileage = _int(car.get("mileage_max"))
    year_min = _int(car.get("year_min"))
    power_min = _int(car.get("power_min"))

    payload: dict[str, Any] = {
        "locale": "en_GB",
        "vehicleCategory": "10",  # Personal cars
        "price": {"minPrice": str(low if low is not None else 0),
                  "maxPrice": str(high if high is not None else 999999)},
        "enginePower": {"min": str(power_min if power_min is not None else 0), "max": "9999"},
        "ageOfVehicle": {"min": "0", "max": str(max(0, date.today().year - year_min) if year_min else 99)},
        "mileage": {"min": "0", "max": str(mileage if mileage is not None else 999999)},
        "resultOrder": {"orderBy": "Price", "sortOrder": "Ascending"},
        "pagination": {"maxRecords": PAGE_SIZE, "startingRecord": int(starting_record)},
    }
    if home:
        lat, lon = home
        payload["distance"] = str(_int(settings.get("search_radius_miles")) or 900)
        payload["longLatCoordinates"] = f"{lon:.7f},{lat:.7f}"
    if models:
        payload["model"] = [{"searchTerm": name} for name in models]
    return payload


def _inventory(data: dict[str, Any]) -> dict[str, Any]:
    inventory = ((data.get("data") or {}).get("VehicleInventoryList") if isinstance(data, dict) else None)
    if not isinstance(inventory, dict) or not isinstance(inventory.get("VehicleInventoryItem"), list):
        raise RuntimeError("Ford returned an unexpected answer (no VehicleInventoryList)")
    return inventory


def fetch_vehicles(payload: dict[str, Any], timeout: int = 60) -> dict[str, Any]:
    return _inventory(_request("POST", SEARCH_VEHICLES_URL, timeout=timeout, payload=payload))


def _description(container: Any) -> str | None:
    if not isinstance(container, dict):
        return None
    text = str(container.get("ShortDescription") or "").strip()
    return text or None


def _colour(appearance: dict[str, Any]) -> str | None:
    exterior = (appearance or {}).get("ExteriorColor") or {}
    text = str(exterior.get("ShortDescription") or "").strip()
    if not text:
        text = str((exterior.get("Code") or {}).get("value") or "").strip()
    if not text:
        return None
    return text.title() if text.isupper() or text.islower() else text


def _dealer_location(vendor: dict[str, Any]) -> tuple[str | None, tuple[float, float] | None]:
    address = ((vendor.get("ContactInformation") or {}).get("Address") or {}) if isinstance(vendor, dict) else {}
    locality = address.get("Locality") or {}
    town = _first_value(locality.get("NameElement") or [])
    coords = address.get("LocationByCoordinates") or {}
    lat = _float((coords.get("Latitude") or {}).get("DegreesMeasure"))
    lon = _float((coords.get("Longitude") or {}).get("DegreesMeasure"))
    return town, ((lat, lon) if lat is not None and lon is not None else None)


def advert_url(vehicle_id: Any, dealer_id: Any) -> str:
    if vehicle_id in (None, "") or dealer_id in (None, ""):
        return PUBLIC_PAGE
    return f"{RESULTS_PAGE}#/vehicleDetails/{vehicle_id}/{dealer_id}"


def parse_vehicle(item: dict[str, Any]) -> dict[str, Any]:
    vehicle = item.get("Vehicle") or {}
    vendor = item.get("VendorInformation") or {}
    identity = vehicle.get("Identity") or {}
    config = vehicle.get("Configuration") or {}
    history = vehicle.get("History") or {}
    condition = vehicle.get("CurrentCondition") or {}
    appearance = config.get("Appearance") or {}
    engine = config.get("Engine") or {}

    town, dealer_coords = _dealer_location(vendor)
    reg = normalise_plate(str(identity.get("RegistrationNumber") or ""))
    brand = _description(vehicle.get("Brand"))
    make = "Ford" if _plain(brand) == "ford" else brand
    model = _description(vehicle.get("Model"))
    trim = _description(vehicle.get("Variant"))
    body_text = _description(config.get("BodyStyle"))
    body_type = classify_body_type(body_text)
    seats = _int(config.get("NumberOfSeats"))
    if not seats or seats <= 0:
        seats = None
    powers = [(_int(p.get("value")) if isinstance(p, dict) else None) for p in engine.get("EnginePower") or []]
    powers = [p for p in powers if p is not None]
    images = [x for x in appearance.get("ImageRef") or [] if isinstance(x, dict) and x.get("value")]
    price = _int((vendor.get("Price") or {}).get("value"))
    mileage = _int((condition.get("CurrentOdometerReading") or {}).get("value"))
    year = _int(history.get("YearOfProduction"))
    vehicle_id = identity.get("ID")
    dealer_id = vendor.get("VendorCode")

    return {
        "id": vehicle_id,
        "dealer_id": dealer_id,
        "make": make,
        "registration": reg,
        "model": model,
        "trim": trim,
        "year": year,
        # Ford search results currently provide month/year (e.g. "Sep 2025"), not an exact date.
        "first_registered": None,
        "registration_text": str(history.get("DateOfRegistration") or "").strip() or None,
        "mileage": mileage,
        "price": price,
        "previous_price": None,
        "fuel": _description(config.get("FuelType")),
        "transmission": _description(config.get("TransmissionType")),
        "body_type": body_type,
        "body_text": body_text,
        "seats": seats,
        "power": max(powers) if powers else None,
        "colour": _colour(appearance),
        "photo_count": len(images),
        "dealer": str(vendor.get("VendorName") or "").strip() or None,
        "location": town,
        "lat_lon": dealer_coords,
        "url": advert_url(vehicle_id, dealer_id),
    }


def record_wanted(c: dict[str, Any], car: dict[str, Any], models: list[str] | None = None) -> bool:
    if _plain(c.get("make")) != "ford" or not c.get("registration"):
        return False
    if models and not any(model_matches(m, c.get("model")) for m in models):
        return False
    wanted_trim = str(car.get("trim") or "").strip().lower()
    if wanted_trim and wanted_trim not in str(c.get("trim") or "").lower():
        return False
    if not fuel_matches(str(car.get("fuel") or "Any"), str(c.get("fuel") or "")):
        return False
    if not transmission_matches(str(car.get("transmission") or "Any"), str(c.get("transmission") or "")):
        return False
    price, mileage, year, power = c.get("price"), c.get("mileage"), c.get("year"), c.get("power")
    if not price:
        return False
    if car.get("price_min") is not None and price < int(car["price_min"]):
        return False
    if car.get("price_max") is not None and price > int(car["price_max"]):
        return False
    if mileage is not None and car.get("mileage_max") is not None and mileage > int(car["mileage_max"]):
        return False
    if year is not None and car.get("year_min") is not None and year < int(car["year_min"]):
        return False
    if power is not None and car.get("power_min") is not None and power < int(car["power_min"]):
        return False
    return True


def record_to_row(c: dict[str, Any], home: tuple[float, float] | None) -> dict[str, Any] | None:
    reg = c.get("registration")
    if not reg:
        return None
    title = " ".join(str(p) for p in (c.get("year"), "Ford", c.get("model"), c.get("trim"), reg) if p)
    raw = {k: c.get(k) for k in ("id", "dealer_id", "registration_text", "body_text", "power", "lat_lon", "url")}
    return standardise({
        "registration": reg,
        "make": "Ford",
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
        "url": c.get("url") or PUBLIC_PAGE,
        # The search endpoint supplies a thumbnail, not the advert's complete photo count.
        "photo_status": "unknown",
        "photo_count": c.get("photo_count") or 0,
        "photo_reason": "Ford search API supplies search thumbnails only; dealer photo count is not known",
        "title": title,
        "raw_text": json.dumps(raw, ensure_ascii=False),
        "source": SOURCE_KEY,
        "status": "active",
        "last_seen": now_iso(),
    })


def search(car: dict[str, Any], settings: dict[str, Any], timings: dict[str, Any] | None = None) -> SearchResult:
    if _plain(car.get("make")) != "ford":
        raise RuntimeError(f"Ford source does not search make '{car.get('make')}'")

    models: list[str] = []
    if car.get("model"):
        models = models_for(str(car["model"]), KNOWN_MODELS)
        try:
            live = models_for(str(car["model"]), site_models())
        except Exception:  # noqa: BLE001 - fall back to the known list if Ford's options lookup is unavailable
            live = []
        models += [m for m in live if m not in models]
        if not models:
            raise RuntimeError(f"Ford has no model matching '{car['model']}' right now")

    home = postcode_location(settings.get("home_postcode"))
    result = SearchResult(search_url=PUBLIC_PAGE)
    seen_regs: set[str] = set()
    seen_ids: set[str] = set()
    pages_log: list[dict[str, Any]] = []
    starting_record = 0

    for page in range(1, MAX_PAGES + 1):
        payload = build_search(car, models, home, settings, starting_record)
        started = time.perf_counter()
        inventory = fetch_vehicles(payload)
        fetch_seconds = time.perf_counter() - started
        if timings is not None:
            timings["search_fetch_seconds"] = round(float(timings.get("search_fetch_seconds") or 0) + fetch_seconds, 3)

        items = [x for x in inventory.get("VehicleInventoryItem") or [] if isinstance(x, dict)]
        records = [parse_vehicle(x) for x in items]
        new: list[dict[str, Any]] = []
        for c in records:
            key = str(c.get("id") or c.get("registration") or "")
            if key and key not in seen_ids:
                seen_ids.add(key)
                new.append(c)

        rows_added = 0
        for c in new:
            if c.get("registration") and _plain(c.get("make")) == "ford":
                result.raw_regs.add(c["registration"])
            if not record_wanted(c, car, models):
                continue
            row = record_to_row(c, home)
            if not row or row["registration"] in seen_regs:
                continue
            if body_and_seats_match(car, row.get("body_type"), row.get("seats")):
                seen_regs.add(row["registration"])
                result.rows.append(row)
                rows_added += 1

        total = _int(inventory.get("totalMatches"))
        pages_log.append({"search": car.get("name"), "page": page, "starting_record": starting_record,
                          "fetch_seconds": round(fetch_seconds, 3), "vehicle_objects": len(records),
                          "rows": rows_added, "total": total})

        if not new or len(items) < PAGE_SIZE or (total is not None and starting_record + len(items) >= total):
            break
        starting_record += len(items)

    if timings is not None:
        timings.setdefault("pages", []).extend(pages_log)
    result.debug_text = "\n".join(json.dumps(p) for p in pages_log)
    return result
