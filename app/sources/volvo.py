"""Volvo (Volvo Selekt approved used) source module.

Searches https://selekt.volvocars.co.uk, Volvo Car UK's used-car store, which is
built on the Codeweavers "digital retail store" platform:

* the page is an app that asks a JSON API for results:
  ``POST https://services.codeweavers.net/api/vehicles/search-with-facets/groups``;
* every API call needs an anonymous visitor session, which the site creates on
  page load with ``POST /api/guest/initialise/proposal`` using the public API key
  in the page's ``<meta name="cw-api-key">``; the session token then goes in the
  ``X-CW-CustomerToken`` header, with the store reference in
  ``X-CW-DigitalRetailStoreReference``.  CarFinder does the same, once per search;
* filters go in ``Filters.Vehicle.SelectedFacets``: ``Models``, ``Fuel``,
  ``Transmission``, ``BodyStyles`` (lists of names) and ``OnTheRoadPriceAbove/Below``,
  ``MileagesBelow``, ``RegistrationYearAbove`` (inclusive); reserved cars are left out;
* ``ResultsPerPage`` can be 100 and ``Page`` starts at 1;
* each vehicle carries the registration, VIN, price, mileage, DVLA registration
  date, colour, fuel, gearbox, body style, seats and the retailer's name, town,
  postcode and latitude/longitude, so distance is the straight line from home.

Every limit is applied again here, as for the other sources.
"""
from __future__ import annotations

import json
import random
import re
import time
from typing import Any

import requests

try:  # A real Chrome TLS handshake, as for Spoticar (the API has not needed it so far).
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

SOURCE_KEY = "volvo"
SOURCE_NAME = "Volvo Selekt"
MAKES = ("Volvo",)

SITE_ROOT = "https://selekt.volvocars.co.uk"
STORE_PATH = "/en-gb/store/all/vehicles"
API_ROOT = "https://services.codeweavers.net"
SESSION_URL = API_ROOT + "/api/guest/initialise/proposal"
SEARCH_URL = API_ROOT + "/api/vehicles/search-with-facets/groups"
STORE_REFERENCE = "349121c1-c059-47e2-908a-dd821b048ce8"
ORGANISATION = {"Type": "CodeweaversReference", "Value": "55577"}
PAGE_SIZE = 100
MAX_PAGES = 40
SORT_BY = "PriceAscending"
IMPERSONATE = "chrome"
PAGE_DELAY = 1.0
PAGE_JITTER = 1.0
RETRY_WAITS = (10.0, 30.0)

# The site's model names (Oct 2026).
KNOWN_MODELS = ["C40", "EC40", "ES90", "EX30", "EX30 Cross Country", "EX40", "EX90", "S60", "S90", "V40",
                "V40 Cross Country", "V60", "V60 Cross Country", "V90", "V90 Cross Country", "XC40", "XC60", "XC90"]

FUEL_VALUES = {
    "Petrol": ["Petrol", "Petrol Mild Hybrid"],
    "Diesel": ["Diesel", "Diesel Mild Hybrid"],
    "Hybrid": ["Hybrid", "Petrol Mild Hybrid", "Diesel Mild Hybrid", "Hybrid Petrol/Electric Plug-in"],
    "Electric": ["Electric"],
}
GEAR_VALUES = {"Manual": ["Manual"], "Automatic": ["Automatic"]}
BODY_VALUES = {"SUV": ["SUV"], "Estate": ["Estate"], "Saloon": ["Saloon"], "Hatchback": ["Hatchback"]}
# Images that are brochure/stock pictures rather than photos of the car itself.
STOCK_IMAGE = re.compile(r"stock|promise|placeholder|coming-soon", re.I)

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-GB,en;q=0.9",
    "Content-Type": "application/json",
    "Origin": SITE_ROOT,
    "Referer": SITE_ROOT + "/",
    "X-CW-ApplicationName": "Storefront",
    "X-CW-Accept-Language": "en-gb",
    "X-CW-DigitalRetailStoreReference": STORE_REFERENCE,
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
    text = re.sub(r"\s+", " ", str(name or "")).strip()
    return text[6:].strip() if text.lower().startswith("volvo ") else text


def model_matches(wanted: str, name: str | None) -> bool:
    """True when a model name starts with what the user typed ("V60" matches "V60 Cross Country")."""
    typed = _plain(model_name(wanted))
    return not typed or _plain(model_name(name)).startswith(typed)


def models_for(model: str, known: list[str]) -> list[str]:
    if not _plain(model):
        return []
    return [name for name in known if model_matches(model, name)]


# ---------------------------------------------------------------------------
# Car search -> API request
# ---------------------------------------------------------------------------

def build_facets(car: dict[str, Any], models: list[str]) -> dict[str, Any]:
    facets: dict[str, Any] = {}
    if models:
        facets["Models"] = list(models)
    if car.get("price_min") is not None:
        facets["OnTheRoadPriceAbove"] = int(car["price_min"])
    if car.get("price_max") is not None:
        facets["OnTheRoadPriceBelow"] = int(car["price_max"])
    if car.get("mileage_max") is not None:
        facets["MileagesBelow"] = int(car["mileage_max"])
    if car.get("year_min") is not None:
        facets["RegistrationYearAbove"] = int(car["year_min"])
    for key, values, field in (("Fuel", FUEL_VALUES, "fuel"), ("Transmission", GEAR_VALUES, "transmission"),
                               ("BodyStyles", BODY_VALUES, "body_type")):
        chosen = values.get(str(car.get(field) or "Any"))
        if chosen:
            facets[key] = list(chosen)
    return facets


def build_body(facets: dict[str, Any], page: int, count: int = PAGE_SIZE) -> dict[str, Any]:
    return {
        "SortBy": SORT_BY, "Page": page, "ResultsPerPage": count, "IncludeNoFinanceOption": True,
        "Filters": {
            "Vehicle": {"Query": None, "IncludeReservedVehicles": False, "SelectedFacets": facets},
            "DigitalRetailStore": {"Page": {"Slug": "all"}},
            "Location": {"Latitude": None, "Longitude": None, "Distance": None},
        },
        "OrganisationIdentifier": dict(ORGANISATION),
        "Intention": "PublicWebsite",
    }


_session: Any = None
_token: str | None = None


def _fetcher() -> str:
    return "curl_cffi (Chrome)" if cffi_requests is not None else "plain requests (curl_cffi not installed)"


def _make_session() -> Any:
    if cffi_requests is not None:
        return cffi_requests.Session(impersonate=IMPERSONATE)
    session = requests.Session()
    session.headers.update({"User-Agent": HEADERS["User-Agent"]})
    return session


def _http() -> Any:
    global _session
    if _session is None:
        _session = _make_session()
    return _session


def _headers(extra: dict[str, str] | None = None) -> dict[str, str]:
    headers = {k: v for k, v in HEADERS.items() if not (cffi_requests is not None and k == "User-Agent")}
    headers.update(extra or {})
    return headers


def api_key() -> str:
    """The public API key the store page gives every visitor (``<meta name="cw-api-key">``)."""
    response = _http().get(SITE_ROOT + STORE_PATH, headers={"Accept": "text/html", "Accept-Language": "en-GB"},
                           timeout=30)
    response.raise_for_status()
    match = re.search(r'<meta\s+name="cw-api-key"\s+content="([^"]+)"', response.text)
    if not match:
        raise RuntimeError(f"Volvo Selekt page has no API key ({len(response.text)} bytes), probably a bot check; "
                           f"fetched with {_fetcher()}")
    return match.group(1)


def new_token() -> str:
    """Start an anonymous visitor session, as the site does on page load."""
    body = {"ApiKey": api_key(), "OrganisationIdentifier": dict(ORGANISATION)}
    response = _http().post(SESSION_URL, data=json.dumps(body), headers=_headers(), timeout=30)
    if response.status_code >= 400:
        raise RuntimeError(f"Volvo Selekt refused a visitor session (HTTP {response.status_code}); "
                           f"fetched with {_fetcher()}")
    data = response.json()
    token = data.get("SessionToken") or data.get("UserToken")
    if not token:
        raise RuntimeError("Volvo Selekt gave no visitor session token")
    return str(token)


def _search_page(body: dict[str, Any], timeout: int = 60) -> dict[str, Any]:
    """POST one results query; a 401 gets a new visitor session, a 403/429 a wait and a fresh connection."""
    global _session, _token
    attempts = len(RETRY_WAITS) + 1
    response = None
    for attempt in range(attempts):
        if _token is None:
            _token = new_token()
        response = _http().post(SEARCH_URL, data=json.dumps(body),
                                headers=_headers({"X-CW-CustomerToken": _token}), timeout=timeout)
        if response.status_code == 401:
            _token = None
            continue
        if response.status_code not in (403, 429):
            response.raise_for_status()
            try:
                data = response.json()
            except ValueError as exc:
                raise RuntimeError(f"Volvo Selekt returned something that is not car data; fetched with "
                                   f"{_fetcher()}") from exc
            if not isinstance(data, dict) or "Groups" not in data:
                raise RuntimeError("Volvo Selekt returned an unexpected answer (no results list)")
            return data
        if attempt < len(RETRY_WAITS):
            _session, _token = None, None
            time.sleep(RETRY_WAITS[attempt])
    status = response.status_code if response is not None else "?"
    server = (response.headers.get("Server") if response is not None else None) or "the site"
    raise RuntimeError(f"Volvo Selekt blocked the request (HTTP {status} from {server}, {attempts} tries; "
                       f"fetched with {_fetcher()})")


def site_models(data: dict[str, Any]) -> list[str]:
    values = ((((data.get("FacetCategories") or {}).get("Model") or {}).get("Options") or {}).get("Values")) or []
    return [str(v.get("Value")) for v in values if v.get("Value")]


# ---------------------------------------------------------------------------
# API record -> car
# ---------------------------------------------------------------------------

def body_for(style: str | None, model: str | None) -> str | None:
    text = f" {style or ''} ".lower()
    for word, body in (("suv", "SUV"), ("estate", "Estate"), ("saloon", "Saloon"), ("hatch", "Hatchback"),
                       ("mpv", "MPV")):
        if word in text:
            return body
    return classify_body_type(style, model)


def town_name(text: Any) -> str | None:
    town = re.sub(r"\s+", " ", str(text or "")).strip()
    return (town.title() if town.isupper() else town) or None


def parse_group(group: dict[str, Any]) -> dict[str, Any]:
    """Everything CarFinder needs from one result (no filtering)."""
    headline = group.get("HeadlineVehicle") or {}
    vehicle = headline.get("Vehicle") or {}
    physical = vehicle.get("Physical") or {}
    spec = vehicle.get("Specification") or {}
    retailer = headline.get("Retailer") or {}
    address = retailer.get("Address") or {}
    location = address.get("Location") or {}
    registration = physical.get("Registration") or {}
    first_reg = str(registration.get("DateRegisteredWithDvla") or "")[:10] or None
    miles = _int(physical.get("Mileage"))
    if miles is not None and str(physical.get("MileageUnit") or "").lower().startswith("kilomet"):
        miles = int(round(miles * 0.621371))
    model = model_name(spec.get("Model"))
    trim = " ".join(p for p in (str(spec.get("Variant") or "").strip(), str(spec.get("Derivative") or "").strip())
                    if p)
    images = [str(i.get("Url") or "") for i in vehicle.get("Images") or [] if isinstance(i, dict)]
    try:
        lat_lon = (float(location["Latitude"]), float(location["Longitude"]))
    except (KeyError, TypeError, ValueError):
        lat_lon = None
    colour = (physical.get("ExteriorColour") or {})
    car_hash = vehicle.get("Hash")
    return {
        "id": group.get("Id") or car_hash,
        "make": spec.get("Manufacturer"),
        "registration": normalise_plate(registration.get("RegistrationNumber")),
        "vin": physical.get("Vin"),
        "model": model or None,
        "trim": trim or None,
        "description": spec.get("Description"),
        "year": _int(first_reg[:4]) if first_reg else None,
        "first_registered": first_reg,
        "mileage": miles,
        "price": _int(physical.get("OnTheRoadPrice")),
        "fuel": spec.get("FuelType"),
        "transmission": spec.get("Transmission"),
        "body_type": body_for(spec.get("BodyStyle"), model),
        "body_text": spec.get("BodyStyle"),
        "seats": _int(spec.get("Seats")),
        "colour": str(colour.get("Description") or colour.get("Value") or "").strip() or None,
        "photo_count": len([u for u in images if not STOCK_IMAGE.search(u)]),
        "image_count": len(images),
        "dealer": retailer.get("Name"),
        "location": town_name(address.get("TownCity")),
        "postcode": address.get("Postcode"),
        "lat_lon": lat_lon,
        "url": f"{SITE_ROOT}{STORE_PATH}/{car_hash}" if car_hash else None,
        "available": not physical.get("IsReserved") and not physical.get("NoLongerAvailable"),
        "car_type": spec.get("Type"),
    }


def record_wanted(c: dict[str, Any], car: dict[str, Any]) -> bool:
    if c.get("make") and _plain(c["make"]) != "volvo":
        return False
    if not c.get("available") or c.get("car_type") not in (None, "", "Car"):
        return False
    if car.get("model") and not model_matches(str(car["model"]), c.get("model")):
        return False
    wanted_trim = str(car.get("trim") or "").strip().lower()
    if wanted_trim and wanted_trim not in f"{c.get('trim') or ''} {c.get('description') or ''}".lower():
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
    elif c.get("image_count"):
        photo_status, photo_reason = "awaiting", "Only stock images so far"
    else:
        photo_status, photo_reason = "awaiting", "No dealer images yet"
    title = " ".join(str(p) for p in (c.get("year"), "Volvo", c.get("model"), c.get("trim"), reg) if p)
    raw = {k: c.get(k) for k in ("id", "vin", "description", "body_text", "first_registered", "mileage", "price",
                                 "dealer", "location", "postcode", "lat_lon", "url")}
    return standardise({
        "registration": reg,
        "make": "Volvo",
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
        "url": c.get("url") or SITE_ROOT + STORE_PATH,
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
    global _token
    if _plain(car.get("make")) != "volvo":
        raise RuntimeError(f"Volvo source does not search make '{car.get('make')}'")
    _token = None  # a fresh visitor session for each search
    models: list[str] = []
    if car.get("model"):
        models = models_for(str(car["model"]), KNOWN_MODELS)
        if not models:
            # The site's own model list, in case a new model has been added.
            models = models_for(str(car["model"]), site_models(_search_page(build_body({}, 1, 1))))
        if not models:
            raise RuntimeError(f"Volvo Selekt has no model matching '{car['model']}' right now")

    facets = build_facets(car, models)
    home = postcode_location(settings.get("home_postcode"))
    result = SearchResult(search_url=SITE_ROOT + STORE_PATH)
    seen: set[str] = set()
    seen_ids: set[str] = set()
    pages_log: list[dict[str, Any]] = []
    pages = 1
    page = 1
    while page <= min(pages, MAX_PAGES):
        if page > 1:
            time.sleep(PAGE_DELAY + random.uniform(0, PAGE_JITTER))
        started = time.perf_counter()
        try:
            data = _search_page(build_body(facets, page))
        except RuntimeError as exc:
            raise RuntimeError(f"{exc} on results page {page}") from exc
        fetch_seconds = time.perf_counter() - started
        if timings is not None:
            timings["search_fetch_seconds"] = round(float(timings.get("search_fetch_seconds") or 0) + fetch_seconds, 3)
        pages = max(1, _int(data.get("TotalPages")) or 1)
        total = _int(data.get("NumberOfMatchingVehiclesAcrossAllGroups"))
        records = [parse_group(g) for g in data.get("Groups") or [] if isinstance(g, dict)]
        new = [c for c in records if c.get("id") not in seen_ids]
        rows = 0
        for c in new:
            seen_ids.add(str(c.get("id")))
            if c.get("registration"):
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
        pages_log.append({"search": car.get("name"), "page": page, "fetch_seconds": round(fetch_seconds, 3),
                          "vehicle_objects": len(records), "rows": rows, "total": total})
        if not new or len(records) < PAGE_SIZE:
            break
        page += 1
    if timings is not None:
        timings.setdefault("pages", []).extend(pages_log)
    result.debug_text = "\n".join(json.dumps(p) for p in pages_log)
    return result
