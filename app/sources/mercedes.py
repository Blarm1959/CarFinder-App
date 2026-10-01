"""Mercedes-Benz (Mercedes-Benz Approved Used) source module.

Searches https://shop.mercedes-benz.co.uk/used, Mercedes-Benz UK's online
showroom.  The results page is an app that asks a JSON API for cars:

* every API call needs a guest token, which the site gets on page load with
  ``POST /api/v3/profiles {"SessionId": <random id>}``; the answer's
  ``authToken`` goes in an ``Authorization: Bearer`` header.  CarFinder does
  the same, once per search, and gets a new token on a 401;
* the search is ``POST /api/v4/vehicles/search/used``.  Filters go in
  ``Criteria``: ``ModelId`` (list of the site's model ids), ``RetailPrice``,
  ``Mileage`` and ``Age`` (registration years) as ``{"Min", "Max"}`` (either end
  can be left out), and ``FuelId`` / ``TransmissionId`` / ``BodyStyleId`` as
  ``{"Values": [...]}``.  The search fails (HTTP 500) without the site's
  default ``Finance`` block, so that is always sent;
* ``ResultsPerPage`` can be 100 and ``PageIndex`` starts at 0;
* each car carries the registration, VIN, commission number, price, mileage,
  registration date, colour, fuel, gearbox, body style and two dealers:
  ``OwningRetailer`` is where the car is, ``Retailer`` the one selling it.  They
  are the same unless the search has a location (Mercedes sells as an agent, so
  with a location the nearest showroom is shown as the seller); CarFinder never
  sends a location and measures distance to the owning dealer;
* the search only says whether a car has a photo (``Media.NoImage``).  The real
  number of photos comes from ``POST /api/v3/vehicles/details`` with up to 50
  ``CommissionNumber`` values, so that is asked only for cars that pass every
  filter and have a photo;
* the site also lists smart cars (brand id 3); they are left out.

Every limit is applied again here, as for the other sources.
"""
from __future__ import annotations

import json
import random
import re
import time
import unicodedata
import uuid
from typing import Any

import requests

try:  # A real Chrome TLS handshake, as for Spoticar (the API has not needed it so far).
    from curl_cffi import requests as cffi_requests
except ImportError:  # pragma: no cover - depends on what is installed
    cffi_requests = None

from app.db import now_iso
from app.geo import distance_miles, place_location, postcode_location
from app.sources import (
    SearchResult,
    body_and_seats_match,
    classify_body_type,
    fuel_matches,
    normalise_plate,
    standardise,
    transmission_matches,
)

SOURCE_KEY = "mercedes"
SOURCE_NAME = "Mercedes-Benz Approved Used"
MAKES = ("Mercedes-Benz",)

SITE_ROOT = "https://shop.mercedes-benz.co.uk"
SEARCH_PAGE = SITE_ROOT + "/used"
PROFILE_URL = SITE_ROOT + "/api/v3/profiles"
SEARCH_URL = SITE_ROOT + "/api/v4/vehicles/search/used"
DETAILS_URL = SITE_ROOT + "/api/v3/vehicles/details"
FILTERS_URL = SITE_ROOT + "/api/v3/filters/used"
PAGE_SIZE = 100
MAX_PAGES = 40
DETAILS_BATCH = 50
SORT_PRICE_ASCENDING = 1
USED = 2
SMART_BRAND = 3
IMPERSONATE = "chrome"
PAGE_DELAY = 1.0
PAGE_JITTER = 1.0
RETRY_WAITS = (10.0, 30.0)

# The site's default finance quote; the search fails without a Finance block.
FINANCE = {"Criteria": {
    "Key": "PCP", "Name": "Agility (Personal Contract Plan)", "Type": "PCP", "IsDefault": True,
    "Term": {"Options": [{"IsDefault": True, "Value": 48}]}, "Deposit": {"Default": "17.5%"},
    "Mileage": {"Options": [{"IsDefault": True, "Value": 10000}]}, "MonthlyPrice": {"Min": 50, "Max": 4000},
    "IsPersonalised": False, "CustomerType": "Personal", "VehicleType": "UNASSIGNED",
    "AdvanceRentals": None, "RegularPayment": None,
}}

# The site's used-car models (Oct 2026): id -> name.  Brand 1 is Mercedes-Benz,
# 2 Mercedes-AMG, 5 Mercedes-Maybach; smart (brand 3) is not included.
KNOWN_MODELS: dict[int, str] = {
    1: "A-Class Hatchback", 2: "B-Class Hatchback", 96: "A-Class Saloon", 122: "Electric CLA", 8: "C-Class Saloon",
    15: "E-Class Saloon", 108: "EQE", 25: "S-Class Saloon", 106: "EQS", 28: "Mercedes-Maybach",
    126: "Electric CLA Shooting Brake", 6: "CLA Shooting Brake", 9: "C-Class Estate", 16: "E-Class Estate",
    7: "GLA SUV", 105: "EQA", 125: "Electric GLB", 98: "GLB SUV", 107: "EQB", 124: "Electric GLC", 13: "GLC SUV",
    14: "GLC Coupé", 103: "EQC", 20: "GLE SUV", 21: "GLE Coupé", 111: "EQE SUV", 30: "GLS SUV", 112: "EQS SUV",
    31: "G-Class", 5: "CLA Coupé", 10: "C-Class Coupé", 114: "CLE Coupé", 17: "E-Class Coupé", 23: "CLS Coupé",
    26: "S-Class Coupé", 11: "C-Class Cabriolet", 116: "CLE Cabriolet", 18: "E-Class Cabriolet",
    27: "S-Class Cabriolet", 12: "SLC Roadster", 29: "SL Roadster", 89: "V-Class", 95: "Marco Polo", 104: "EQV",
    32: "A-Class Hatchback AMG", 97: "A-Class Saloon AMG", 39: "C-Class Saloon AMG", 50: "E-Class Saloon AMG",
    110: "EQE Saloon AMG", 64: "S-Class Saloon AMG", 109: "EQS Saloon AMG", 37: "CLA Shooting Brake AMG",
    41: "C-Class Estate AMG", 52: "E-Class Estate AMG", 38: "GLA SUV AMG", 99: "GLB SUV AMG", 48: "GLC SUV AMG",
    49: "GLC Coupé AMG", 57: "GLE SUV AMG", 59: "GLE Coupé AMG", 113: "EQE SUV AMG", 73: "GLS SUV AMG",
    74: "G-Class AMG", 36: "CLA Coupé AMG", 43: "C-Class Coupé AMG", 115: "CLE Coupé AMG", 54: "E-Class Coupé AMG",
    62: "CLS Coupé AMG", 66: "S-Class Coupé", 83: "AMG GT Coupé", 94: "AMG GT Coupé (4 door)",
    45: "C-Class Cabriolet AMG", 117: "CLE Cabriolet AMG", 55: "E-Class Cabriolet AMG", 68: "S-Class Cabriolet",
    47: "SLC Roadster", 71: "SL Roadster AMG", 82: "AMG-GT Roadster", 118: "Mercedes-Maybach",
    119: "GLS SUV Maybach", 120: "EQS SUV Maybach", 121: "SL Roadster Maybach",
}

FUEL_IDS = {"Petrol": [1], "Diesel": [2], "Hybrid": [3], "Electric": [4]}
GEAR_IDS = {"Automatic": [1], "Manual": [2]}
BODY_IDS = {"Hatchback": [1], "Saloon": [2], "Estate": [3], "SUV": [4], "MPV": [19]}
# The site's body styles that are not one of CarFinder's body types.
OTHER_BODIES = ("coupe", "cabrio", "roadster")
NO_IMAGE = re.compile(r"no_image", re.I)

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-GB,en;q=0.9",
    "Content-Type": "application/json",
    "Origin": SITE_ROOT,
    "Referer": SEARCH_PAGE,
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
}


def _plain(text: Any) -> str:
    """Lower-case letters and digits only, accents removed ("Coupé" -> "coupe")."""
    text = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        digits = re.sub(r"[^0-9]", "", str(value))
        return int(digits) if digits else None


def is_mercedes(make: Any) -> bool:
    return _plain(make) in ("mercedes", "mercedesbenz", "mercedesamg", "mercedesmaybach")


def _name_keys(name: str) -> list[str]:
    """The ways a site model name can be typed: "Mercedes-Maybach" also as "Maybach"."""
    plain = _plain(name)
    keys = [plain]
    for prefix in ("mercedesbenz", "mercedes", "electric"):
        if plain.startswith(prefix) and len(plain) > len(prefix):
            keys.append(plain[len(prefix):])
    return keys


def model_matches(wanted: str, name: str | None) -> bool:
    """True when a site model name starts with what the user typed ("GLC" matches "GLC Coupé AMG")."""
    typed = _plain(wanted)
    for prefix in ("mercedesbenz", "mercedes"):
        if typed.startswith(prefix) and len(typed) > len(prefix):
            typed = typed[len(prefix):]
    return not typed or any(key.startswith(typed) for key in _name_keys(str(name or "")))


def model_ids_for(model: str, known: dict[int, str]) -> list[int]:
    if not _plain(model):
        return []
    return [model_id for model_id, name in known.items() if model_matches(model, name)]


# ---------------------------------------------------------------------------
# Car search -> API request
# ---------------------------------------------------------------------------

def _range(low: Any, high: Any) -> dict[str, int] | None:
    out = {}
    if low is not None:
        out["Min"] = int(low)
    if high is not None:
        out["Max"] = int(high)
    return out or None


def build_criteria(car: dict[str, Any], model_ids: list[int]) -> dict[str, Any]:
    criteria: dict[str, Any] = {"VehicleType": USED, "LimitToMotability": False, "LatestModel": False,
                                "PreviousModel": False, "PromotionalOfferVehiclesOnly": False,
                                "ModelId": list(model_ids)}
    for key, value in (("RetailPrice", _range(car.get("price_min"), car.get("price_max"))),
                       ("Mileage", _range(None, car.get("mileage_max"))),
                       ("Age", _range(car.get("year_min"), None))):
        if value:
            criteria[key] = value
    for key, values, field in (("FuelId", FUEL_IDS, "fuel"), ("TransmissionId", GEAR_IDS, "transmission"),
                               ("BodyStyleId", BODY_IDS, "body_type")):
        chosen = values.get(str(car.get(field) or "Any"))
        if chosen:
            criteria[key] = {"Values": list(chosen)}
    return criteria


def build_body(criteria: dict[str, Any], page_index: int, count: int = PAGE_SIZE) -> dict[str, Any]:
    return {
        "Criteria": criteria,
        "Sort": {"Id": SORT_PRICE_ASCENDING},
        "Finance": FINANCE,
        "Paging": {"ResultsPerPage": count, "PageIndex": page_index},
        "DisableBestMatch": True,
        "IncludeOffers": False, "IncludeReservations": False, "IncludeQuotes": False,
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


def new_token() -> str:
    """Start a guest profile, as the site does on page load, and return its token."""
    body = {"SessionId": uuid.uuid4().hex}
    response = _http().post(PROFILE_URL, data=json.dumps(body), headers=_headers(), timeout=30)
    if response.status_code >= 400:
        server = response.headers.get("Server") or "the site"
        raise RuntimeError(f"Mercedes-Benz refused a guest session (HTTP {response.status_code} from {server}); "
                           f"fetched with {_fetcher()}")
    try:
        token = (response.json() or {}).get("authToken")
    except ValueError:
        token = None
    if not token:
        raise RuntimeError(f"Mercedes-Benz gave no guest token ({len(response.text or '')} bytes), probably a "
                           f"bot check; fetched with {_fetcher()}")
    return str(token)


def _post(url: str, body: dict[str, Any], timeout: int = 60) -> Any:
    """POST to the API; a 401 gets a new guest token, a 403/429 a wait and a fresh connection."""
    global _session, _token
    attempts = len(RETRY_WAITS) + 1
    response = None
    for attempt in range(attempts):
        if _token is None:
            _token = new_token()
        response = _http().post(url, data=json.dumps(body),
                                headers=_headers({"Authorization": f"Bearer {_token}"}), timeout=timeout)
        if response.status_code == 401:
            _token = None
            continue
        if response.status_code not in (403, 429):
            response.raise_for_status()
            try:
                return response.json()
            except ValueError as exc:
                raise RuntimeError(f"Mercedes-Benz returned something that is not car data; fetched with "
                                   f"{_fetcher()}") from exc
        if attempt < len(RETRY_WAITS):
            _session, _token = None, None
            time.sleep(RETRY_WAITS[attempt])
    status = response.status_code if response is not None else "?"
    server = (response.headers.get("Server") if response is not None else None) or "the site"
    raise RuntimeError(f"Mercedes-Benz blocked the request (HTTP {status} from {server}, {attempts} tries; "
                       f"fetched with {_fetcher()})")


def _search_page(body: dict[str, Any], timeout: int = 60) -> dict[str, Any]:
    data = _post(SEARCH_URL, body, timeout)
    results = data.get("SearchResults") if isinstance(data, dict) else None
    if not isinstance(results, dict) or not isinstance(results.get("Vehicles"), list):
        raise RuntimeError("Mercedes-Benz returned an unexpected answer (no results list)")
    return data


def _details(commission_numbers: list[str], timeout: int = 60) -> dict[str, Any]:
    body = {"IncludeOffers": False, "IncludeReservations": False, "IncludeEquipment": False,
            "IncludeQuotes": False, "CommissionNumber": list(commission_numbers)}
    data = _post(DETAILS_URL, body, timeout)
    if not isinstance(data, dict) or not isinstance(data.get("Vehicles"), list):
        raise RuntimeError("Mercedes-Benz returned an unexpected answer (no vehicle details)")
    return data


def photo_counts(details: dict[str, Any]) -> dict[str, int]:
    """Commission number -> number of photos of the car itself."""
    counts: dict[str, int] = {}
    for vehicle in details.get("Vehicles") or []:
        media = vehicle.get("Media") or {}
        urls = [u for u in media.get("VehicleImageUrls") or [] if u and not NO_IMAGE.search(str(u))]
        count = max(len(urls), _int(media.get("ImageCount")) or 0)
        if media.get("NoImage"):
            count = 0
        if vehicle.get("CommissionNumber"):
            counts[str(vehicle["CommissionNumber"])] = count
    return counts


def fetch_photo_counts(commission_numbers: list[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for start in range(0, len(commission_numbers), DETAILS_BATCH):
        counts.update(photo_counts(_details(commission_numbers[start:start + DETAILS_BATCH])))
    return counts


def site_models() -> dict[int, str]:
    """The site's current model list (in case a new model has been added), smart left out."""
    data = _http().get(FILTERS_URL, headers=_headers({"Authorization": f"Bearer {_token or new_token()}"}),
                       timeout=30)
    data.raise_for_status()
    models: dict[int, str] = {}
    for brand in data.json().get("Brands") or []:
        if _int(brand.get("Id")) == SMART_BRAND:
            continue
        for model in brand.get("Models") or []:
            if _int(model.get("Id")) is not None and model.get("Description"):
                models[_int(model["Id"])] = str(model["Description"])
    return models


# ---------------------------------------------------------------------------
# API record -> car
# ---------------------------------------------------------------------------

def body_for(style: str | None, model: str | None) -> str | None:
    text = f" {_plain(style)} "
    for word, body in (("suv", "SUV"), ("estate", "Estate"), ("shootingbrake", "Estate"), ("saloon", "Saloon"),
                       ("hatch", "Hatchback"), ("mpv", "MPV")):
        if word in text:
            return body
    if any(word in text for word in OTHER_BODIES):
        return None
    return classify_body_type(style, model)


def other_body(style: str | None) -> bool:
    """Coupé, cabriolet or roadster: never what a body-type search asks for."""
    return any(word in _plain(style) for word in OTHER_BODIES)


def tidy_colour(text: Any) -> str | None:
    colour = re.sub(r"\s+", " ", str(text or "")).strip()
    return (colour[:1].upper() + colour[1:]) if colour else None


def trim_from(description: Any, model: Any) -> str | None:
    text = re.sub(r"\s+", " ", str(description or "")).strip()
    if text.lower().startswith("mercedes-benz "):
        text = text[14:]
    elif text.lower().startswith("mercedes-"):
        text = text[9:]
    name = str(model or "").strip()
    if name and text.lower().startswith(name.lower() + " "):
        text = text[len(name) + 1:]
    return text or None


def detail_url(vehicle: dict[str, Any], retailer_id: Any) -> str | None:
    number = vehicle.get("CommissionNumber")
    if not number or retailer_id in (None, ""):
        return None
    slug = re.sub(r"[^a-z0-9]+", "-", _plain_spaced(vehicle.get("Description"))).strip("-") or "car"
    return f"{SITE_ROOT}/used/vehicle-detail/{slug}/{number}/{retailer_id}"


def _plain_spaced(text: Any) -> str:
    return unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode().lower()


def town_name(text: Any) -> str | None:
    town = re.sub(r"\s+", " ", str(text or "")).strip()
    return (town.title() if town.isupper() else town) or None


def parse_vehicle(vehicle: dict[str, Any]) -> dict[str, Any]:
    """Everything CarFinder needs from one result (no filtering)."""
    dealer = vehicle.get("OwningRetailer") or vehicle.get("Retailer") or {}
    seller = vehicle.get("Retailer") or dealer
    media = vehicle.get("Media") or {}
    brand = vehicle.get("Brand") or {}
    first_reg = str(vehicle.get("RegistrationDate") or "")[:10] or None
    try:
        lat_lon = (float(dealer["Latitude"]), float(dealer["Longitude"]))
        if lat_lon == (0.0, 0.0):
            lat_lon = None
    except (KeyError, TypeError, ValueError):
        lat_lon = None
    model = str(vehicle.get("Model") or "").strip() or None
    has_photo = not media.get("NoImage") and not NO_IMAGE.search(str(media.get("MainImageUrl") or ""))
    return {
        "id": vehicle.get("Id"),
        "commission_number": str(vehicle.get("CommissionNumber") or "") or None,
        "brand_id": _int(brand.get("Id")),
        "registration": normalise_plate(vehicle.get("RegistrationNumber")),
        "vin": vehicle.get("Vin"),
        "model": model,
        "model_id": _int(vehicle.get("ModelId")),
        "trim": trim_from(vehicle.get("Description"), model),
        "description": vehicle.get("Description"),
        "year": _int(first_reg[:4]) if first_reg else None,
        "first_registered": first_reg,
        "mileage": _int(vehicle.get("Mileage")),
        "price": _int(vehicle.get("ActualPrice")) or _int(vehicle.get("RetailPrice")),
        "fuel": vehicle.get("FuelType"),
        "transmission": vehicle.get("TransmissionType"),
        "body_type": body_for(vehicle.get("BodyStyle"), model),
        "body_text": vehicle.get("BodyStyle"),
        "colour": tidy_colour(vehicle.get("Colour")),
        "has_photo": has_photo,
        "source_type": (vehicle.get("VehicleSource") or {}).get("Description"),
        "dealer": dealer.get("Description"),
        "location": town_name(dealer.get("City")),
        "postcode": dealer.get("Postcode"),
        "lat_lon": lat_lon,
        "url": detail_url(vehicle, seller.get("Id")),
        "available": vehicle.get("IsSellable") is not False,
        "vehicle_type": vehicle.get("VehicleType"),
    }


def record_wanted(c: dict[str, Any], car: dict[str, Any], model_ids: list[int] | None = None) -> bool:
    if c.get("brand_id") == SMART_BRAND:
        return False
    if not c.get("available") or str(c.get("vehicle_type") or "USED").upper() != "USED":
        return False
    if model_ids and c.get("model_id") not in model_ids:
        return False
    wanted_trim = str(car.get("trim") or "").strip().lower()
    if wanted_trim and wanted_trim not in f"{c.get('trim') or ''} {c.get('description') or ''}".lower():
        return False
    if str(car.get("body_type") or "Any") != "Any" and other_body(c.get("body_text")):
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


def dealer_location(c: dict[str, Any]) -> tuple[float, float] | None:
    """Dealer lat/lon from the site, else its postcode, else its town."""
    return c.get("lat_lon") or postcode_location(c.get("postcode")) or place_location(c.get("location"))


def record_to_row(c: dict[str, Any], home: tuple[float, float] | None,
                  counts: dict[str, int] | None = None) -> dict[str, Any] | None:
    reg = c.get("registration")
    if not reg:
        return None
    count = (counts or {}).get(str(c.get("commission_number"))) if c.get("has_photo") else 0
    if count is None:
        photo_count, photo_status = 0, "unknown"
        photo_reason = "Has a photo, but the number of photos could not be checked"
    elif count > 1:
        photo_count, photo_status, photo_reason = count, "photos", f"{count} dealer images"
    elif count == 1:
        photo_count, photo_status, photo_reason = 1, "awaiting", "Only one image; treating as stock/awaiting"
    else:
        photo_count, photo_status, photo_reason = 0, "awaiting", "No dealer images yet"
    where = dealer_location(c)
    title = " ".join(str(p) for p in (c.get("year"), "Mercedes-Benz", c.get("model"), c.get("trim"), reg) if p)
    raw = {k: c.get(k) for k in ("id", "commission_number", "vin", "description", "body_text", "first_registered",
                                 "mileage", "price", "source_type", "dealer", "location", "postcode", "lat_lon",
                                 "url")}
    return standardise({
        "registration": reg,
        "make": "Mercedes-Benz",
        "model": c.get("model"),
        "trim": c.get("trim"),
        "year": c.get("year"),
        "first_registered": c.get("first_registered"),
        "colour": c.get("colour"),
        "fuel": c.get("fuel"),
        "transmission": c.get("transmission"),
        "body_type": c.get("body_type"),
        "seats": None,
        "mileage": c.get("mileage"),
        "price": c.get("price"),
        "previous_price": None,
        "dealer": c.get("dealer"),
        "location": c.get("location"),
        "distance_miles": distance_miles(home, where) if where else None,
        "url": c.get("url") or SEARCH_PAGE,
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
    if not is_mercedes(car.get("make")):
        raise RuntimeError(f"Mercedes-Benz source does not search make '{car.get('make')}'")
    _token = None  # a fresh guest session for each search
    if car.get("model"):
        model_ids = model_ids_for(str(car["model"]), KNOWN_MODELS)
        if not model_ids:
            model_ids = model_ids_for(str(car["model"]), site_models())
        if not model_ids:
            raise RuntimeError(f"Mercedes-Benz has no model matching '{car['model']}' right now")
    else:
        model_ids = list(KNOWN_MODELS)  # every Mercedes-Benz model, so smart cars stay out

    criteria = build_criteria(car, model_ids)
    home = postcode_location(settings.get("home_postcode"))
    result = SearchResult(search_url=SEARCH_PAGE)
    seen: set[str] = set()
    seen_ids: set[str] = set()
    pages_log: list[dict[str, Any]] = []
    pages = 1
    page = 0
    while page < min(pages, MAX_PAGES):
        if page > 0:
            time.sleep(PAGE_DELAY + random.uniform(0, PAGE_JITTER))
        started = time.perf_counter()
        try:
            data = _search_page(build_body(criteria, page))
        except RuntimeError as exc:
            raise RuntimeError(f"{exc} on results page {page + 1}") from exc
        fetch_seconds = time.perf_counter() - started
        results = data["SearchResults"]
        pages = max(1, _int(results.get("TotalPages")) or 1)
        total = _int(results.get("TotalResults"))
        records = [parse_vehicle(v) for v in results.get("Vehicles") or [] if isinstance(v, dict)]
        new = [c for c in records if str(c.get("id")) not in seen_ids]
        wanted = []
        for c in new:
            seen_ids.add(str(c.get("id")))
            if c.get("registration"):
                result.raw_regs.add(c["registration"])
            if record_wanted(c, car, model_ids):
                wanted.append(c)
        # Photo counts only for cars that will be shown and have a photo at all.
        details_seconds = 0.0
        to_check = [c["commission_number"] for c in wanted if c.get("has_photo") and c.get("commission_number")]
        counts: dict[str, int] | None = {}
        if to_check:
            started = time.perf_counter()
            try:
                counts = fetch_photo_counts(to_check)
            except Exception:  # noqa: BLE001 - requests or curl_cffi errors alike
                counts = None  # photo status "unknown" rather than failing the whole search
            details_seconds = time.perf_counter() - started
        if timings is not None:
            timings["search_fetch_seconds"] = round(
                float(timings.get("search_fetch_seconds") or 0) + fetch_seconds + details_seconds, 3)
        rows = 0
        for c in wanted:
            row = record_to_row(c, home, counts)
            if not row or row["registration"] in seen:
                continue
            if body_and_seats_match(car, row.get("body_type"), row.get("seats")):
                seen.add(row["registration"])
                result.rows.append(row)
                rows += 1
        pages_log.append({"search": car.get("name"), "page": page + 1, "fetch_seconds": round(fetch_seconds, 3),
                          "details_seconds": round(details_seconds, 3), "vehicle_objects": len(records),
                          "rows": rows, "total": total})
        if not new or len(records) < PAGE_SIZE:
            break
        page += 1
    if timings is not None:
        timings.setdefault("pages", []).extend(pages_log)
    result.debug_text = "\n".join(json.dumps(p) for p in pages_log)
    return result
