"""Nissan (Nissan Intelligent Choice approved used) source module.

Searches https://usedcars.nissan.co.uk, Nissan UK's certified pre-owned site.
The results page is a Next.js app that asks Nissan's GraphQL API for cars:

* every call needs a public token, which the site gets from
  ``GET <TOKEN_URL>`` (``{"idToken", "expiresIn"}``, valid 3 hours); it goes in
  the ``Authorization`` header as it is (no "Bearer").  CarFinder gets one per
  search and a new one on a 401;
* the search is the ``getUsedCarsInventoryData`` query.  Filters go in
  ``queryFilters`` as ``{"type", "values"}`` (``make``, ``modelName`` - the
  site's model labels such as ``Qashqai``, ``LEAF``, ``fuelType``, ``gearbox``)
  and ``rangeFilters`` as ``{"key": "PRICE_RANGE_FILTER", "ranges": [{"min",
  "max"}]}`` ("any" for an open end).  The mileage and registration-year
  ranges return no cars at all, even from the site's own filters (Oct 2026), so
  they are not sent;
* 15 cars per page (the ``pagination`` input is ignored); ``pageNumber`` starts
  at 1.  ``sortingCriteria {type: PRICE, order: ASC}`` sorts cheapest first;
* with a ``location`` (home latitude/longitude, a large radius) each car's
  dealer comes back with its distance in miles; it does not change which
  dealer is shown or which cars are found;
* the site does not show number plates, but the API has them
  (``registrationPlate``), so CarFinder asks for that field; a car without one
  is kept under its VIN;
* each car also has the first-registration month and year, the number of
  photos (``mediaCount``) and the seat count when the dealer gave it.  The
  body style is "Hatchback" for most Qashqais and Jukes, so the body type comes
  from the model; vans and pick-ups (not "Passenger car") are left out.

Every limit is applied again here, as for the other sources.
"""
from __future__ import annotations

import json
import random
import re
import time
import unicodedata
from typing import Any
from urllib.parse import urlencode

import requests

try:  # A real Chrome TLS handshake, as for Spoticar (the API has not needed it so far).
    from curl_cffi import requests as cffi_requests
except ImportError:  # pragma: no cover - depends on what is installed
    cffi_requests = None

from app.db import now_iso
from app.geo import postcode_location
from app.sources import (
    SearchResult,
    body_and_seats_match,
    classify_body_type,
    fuel_matches,
    normalise_plate,
    standardise,
    transmission_matches,
)

SOURCE_KEY = "nissan"
SOURCE_NAME = "Nissan Intelligent Choice"
MAKES = ("Nissan",)

SITE_ROOT = "https://usedcars.nissan.co.uk"
SEARCH_PAGE = SITE_ROOT + "/all-vehicles/inventory"
GRAPHQL_URL = "https://gq-eu-prod.nissanpace.com/graphql"
TOKEN_URL = ("https://apigateway-eu-prod.nissanpace.com/euw1nisprod/public-access-token"
             "?brand=NISSAN&dataSourceType=live&market=GB&client=euecomm")
ADVERT_URL = "https://www.nissan.co.uk/cars-for-sale/product-details/buy.shtml"
PAGE_SIZE = 15
MAX_PAGES = 120
IMPERSONATE = "chrome"
PAGE_DELAY = 0.5
PAGE_JITTER = 0.5
RETRY_WAITS = (10.0, 30.0)
RADIUS_MILES = 1000

MARKET = {"brand": "NISSAN", "country": "GB", "language": "en",
          "metadata": {"clientApp": "[WEB]USEDCARS", "correlationId": ""}}
BASE_INPUT = {"usedCarsServletURL": "/content/nissan_prod/en_GB/index/cf-used-cars-ecom.model.json",
              "includeCentralStock": False, "minLatestAchievementPoint": 40, "withDiscounts": False,
              "vinListType": "used_cars", "dealerId": ""}

QUERY = """query GetUsedCarsInventoryData($marketConfig: MarketConfig!,
    $usedCarsInventoryInputData: UsedCarsInventoryInputData!) {
  getUsedCarsInventoryData(marketConfig: $marketConfig, usedCarsInventoryInputData: $usedCarsInventoryInputData) {
    vehicles {
      vin registrationPlate make modelName grade version mileage registrationMonth registrationYear
      color { exteriorBaseColor } fuelType transmission numberOfSeats bodystyle rrpPrice discountedPrice
      mediaCount vehicleType vehiclesku isCentralStock
      dealer { dealerId dealerName distance unit dealerStatus unusedServiceDealer }
    }
    metaData { pageIndex pageSize totalCount totalPages hasMorePages }
  }
}"""

MODELS_QUERY = """query GetUsedCarsInventoryData($marketConfig: MarketConfig!,
    $usedCarsInventoryInputData: UsedCarsInventoryInputData!) {
  getUsedCarsInventoryData(marketConfig: $marketConfig, usedCarsInventoryInputData: $usedCarsInventoryInputData) {
    facetsData { vehicleFacets { type values { label } } }
  }
}"""

# The site's model labels (Oct 2026), used before asking the site for its current list.
KNOWN_MODELS = ("Qashqai", "Juke", "X-Trail", "ARIYA", "LEAF", "MICRA", "Note", "Townstar", "e-NV200",
                "Primastar", "Interstar", "Navara")
# Body type by model: the site calls most Qashqais and Jukes hatchbacks.
MODEL_BODIES = {"qashqai": "SUV", "juke": "SUV", "xtrail": "SUV", "ariya": "SUV", "leaf": "Hatchback",
                "micra": "Hatchback", "note": "Hatchback", "pulsar": "Hatchback", "townstar": "MPV", "env200": "MPV"}
FUELS = {"Petrol": "Petrol", "Diesel": "Diesel", "Hybrid": "Hybrid", "Electric": "Electric"}
GEARBOXES = {"Automatic": "Automatic", "Manual": "Manual"}
MONTHS = {m: i for i, m in enumerate(("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov",
                                       "dec"), start=1)}

HEADERS = {
    "Accept": "*/*",
    "Accept-Language": "en-GB,en;q=0.9",
    "Content-Type": "application/json",
    "Origin": SITE_ROOT,
    "Referer": SITE_ROOT + "/",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
}


def _plain(text: Any) -> str:
    """Lower-case letters and digits only, accents removed ("X-Trail" -> "xtrail")."""
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


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def is_nissan(make: Any) -> bool:
    return _plain(make) == "nissan"


def _model_key(name: Any) -> str:
    key = _plain(name)
    return key[6:] if key.startswith("nissan") and len(key) > 6 else key


def model_matches(wanted: str, name: str | None) -> bool:
    """True when a site model label starts with what the user typed ("x trail" matches "X-Trail")."""
    typed = _model_key(wanted)
    return not typed or _model_key(name).startswith(typed)


def models_for(model: str, known: tuple[str, ...] | list[str]) -> list[str]:
    if not _plain(model):
        return []
    return [name for name in known if model_matches(model, name)]


# ---------------------------------------------------------------------------
# Car search -> API request
# ---------------------------------------------------------------------------

def build_input(car: dict[str, Any], models: list[str], page: int = 1,
                home: tuple[float, float] | None = None) -> dict[str, Any]:
    filters: list[dict[str, Any]] = [{"type": "make", "values": ["Nissan"]}]
    if models:
        filters.append({"type": "modelName", "values": list(models)})
    fuel = FUELS.get(str(car.get("fuel") or "Any"))
    if fuel:
        filters.append({"type": "fuelType", "values": [fuel]})
    gearbox = GEARBOXES.get(str(car.get("transmission") or "Any"))
    if gearbox:
        filters.append({"type": "gearbox", "values": [gearbox]})
    data: dict[str, Any] = dict(BASE_INPUT, pageNumber=page, queryFilters=filters,
                                sortingCriteria={"type": "PRICE", "order": "ASC"})
    low, high = car.get("price_min"), car.get("price_max")
    if low is not None or high is not None:
        data["rangeFilters"] = [{"key": "PRICE_RANGE_FILTER", "ranges": [{
            "min": str(int(low)) if low is not None else "any",
            "max": str(int(high)) if high is not None else "any"}]}]
    if home:
        data["location"] = {"lat": home[0], "long": home[1], "radius": RADIUS_MILES, "unit": "M"}
    return data


def build_body(data: dict[str, Any], query: str = QUERY) -> dict[str, Any]:
    return {"operationName": "GetUsedCarsInventoryData", "query": query,
            "variables": {"marketConfig": MARKET, "usedCarsInventoryInputData": data}}


def search_page_url(car: dict[str, Any], models: list[str]) -> str:
    """The same search on the site, for the "open search" link."""
    params: list[tuple[str, str]] = [("make", "nissan")]
    if len(models) == 1:
        params.append(("models", _plain(models[0])))
    if car.get("price_min") is not None or car.get("price_max") is not None:
        params += [("totalpricemin", str(car.get("price_min") if car.get("price_min") is not None else "any")),
                   ("totalpricemax", str(car.get("price_max") if car.get("price_max") is not None else "any")),
                   ("parentfilter", "rangefilters")]
    return SEARCH_PAGE + "?" + urlencode(params)


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
    """The public token the site uses for its searches."""
    response = _http().get(TOKEN_URL, headers=_headers(), timeout=30)
    if response.status_code >= 400:
        server = response.headers.get("Server") or "the site"
        raise RuntimeError(f"Nissan refused a search token (HTTP {response.status_code} from {server}); "
                           f"fetched with {_fetcher()}")
    try:
        token = (response.json() or {}).get("idToken")
    except ValueError:
        token = None
    if not token:
        raise RuntimeError(f"Nissan gave no search token ({len(response.text or '')} bytes); fetched with "
                           f"{_fetcher()}")
    return str(token)


def _post(body: dict[str, Any], timeout: int = 60) -> dict[str, Any]:
    """POST a query; a 401 gets a new token, a 403/429 a wait and a fresh connection."""
    global _session, _token
    attempts = len(RETRY_WAITS) + 1
    response = None
    for attempt in range(attempts):
        if _token is None:
            _token = new_token()
        response = _http().post(GRAPHQL_URL, data=json.dumps(body), headers=_headers({"Authorization": _token}),
                                timeout=timeout)
        if response.status_code == 401:
            _token = None
            continue
        if response.status_code not in (403, 429):
            response.raise_for_status()
            try:
                data = response.json()
            except ValueError as exc:
                raise RuntimeError(f"Nissan returned something that is not car data; fetched with "
                                   f"{_fetcher()}") from exc
            if not isinstance(data, dict):
                raise RuntimeError("Nissan returned an unexpected answer")
            if data.get("errors") and not data.get("data"):
                message = str((data["errors"][0] or {}).get("message") or data["errors"][0])[:200]
                raise RuntimeError(f"Nissan's search failed: {message}")
            return data
        if attempt < len(RETRY_WAITS):
            _session, _token = None, None
            time.sleep(RETRY_WAITS[attempt])
    status = response.status_code if response is not None else "?"
    server = (response.headers.get("Server") if response is not None else None) or "the site"
    raise RuntimeError(f"Nissan blocked the request (HTTP {status} from {server}, {attempts} tries; "
                       f"fetched with {_fetcher()})")


def _inventory(data: dict[str, Any], timeout: int = 60) -> dict[str, Any]:
    answer = _post(build_body(data), timeout)
    inventory = (answer.get("data") or {}).get("getUsedCarsInventoryData")
    if not isinstance(inventory, dict) or not isinstance(inventory.get("vehicles"), list):
        raise RuntimeError("Nissan returned an unexpected answer (no vehicles list)")
    return inventory


def site_models() -> list[str]:
    """The site's current model labels (in case a new model has been added)."""
    data = dict(BASE_INPUT, pageNumber=1, queryFilters=[{"type": "make", "values": ["Nissan"]}])
    answer = _post(build_body(data, MODELS_QUERY), 30)
    facets = (((answer.get("data") or {}).get("getUsedCarsInventoryData") or {}).get("facetsData") or {})
    for facet in facets.get("vehicleFacets") or []:
        if facet.get("type") == "modelName":
            return [str(v["label"]) for v in facet.get("values") or [] if v.get("label")]
    return []


# ---------------------------------------------------------------------------
# API record -> car
# ---------------------------------------------------------------------------

def tidy_model(name: Any) -> str | None:
    """"LEAF" -> "Leaf", "X-Trail" and "e-NV200" as they are."""
    text = re.sub(r"\s+", " ", str(name or "")).strip()
    if text.isupper() and len(text) > 2:
        return text.capitalize()
    return text or None


def body_for(model: Any, style: Any) -> str | None:
    key = _model_key(model)
    for name, body in MODEL_BODIES.items():
        if key.startswith(name):
            return body
    return classify_body_type(style)


def is_van(vehicle_type: Any, style: Any) -> bool:
    kind = str(vehicle_type or "").strip().lower()
    if kind and kind != "passenger car":
        return True
    return bool(re.search(r"\b(van|pick-?up|chassis)\b", str(style or ""), re.I))


def first_registered(month: Any, year: Any) -> str | None:
    """"Sep", "2019" -> "2019-09-01" (the site gives the month, not the day)."""
    y = _int(year)
    m = MONTHS.get(str(month or "").strip().lower()[:3])
    return f"{y:04d}-{m:02d}-01" if y and m else None


def advert_url(vehicle: dict[str, Any]) -> str | None:
    vin = vehicle.get("vin")
    dealer = vehicle.get("dealer") or {}
    if not vin:
        return None
    params = {"id": vin, "dealerId": dealer.get("dealerId") or "",
              "disabledDealer": "false" if str(dealer.get("dealerStatus") or "Enabled") == "Enabled" else "true",
              "unusedServiceDealer": str(bool(dealer.get("unusedServiceDealer"))).lower(),
              "isCentralStock": str(bool(vehicle.get("isCentralStock"))).lower(),
              "isGlobal": "true", "vinListType": "used_cars", "plpUser": "true"}
    return ADVERT_URL + "?" + urlencode(params)


def parse_vehicle(vehicle: dict[str, Any]) -> dict[str, Any]:
    """Everything CarFinder needs from one result (no filtering)."""
    dealer = vehicle.get("dealer") or {}
    vin = str(vehicle.get("vin") or "").strip().upper() or None
    plate = normalise_plate(vehicle.get("registrationPlate"))
    first_reg = first_registered(vehicle.get("registrationMonth"), vehicle.get("registrationYear"))
    distance = _float(dealer.get("distance"))
    if distance is not None and str(dealer.get("unit") or "mi").lower().startswith("k"):
        distance = distance / 1.609344
    model = tidy_model(vehicle.get("modelName"))
    seats = re.match(r"\s*(\d+)", str(vehicle.get("numberOfSeats") or ""))
    return {
        "vin": vin,
        "registration": plate or vin,
        "has_plate": bool(plate),
        "model": model,
        "site_model": vehicle.get("modelName"),
        "trim": re.sub(r"\s+", " ", str(vehicle.get("version") or "")).strip() or None,
        "grade": vehicle.get("grade") or None,
        "year": _int(vehicle.get("registrationYear")),
        "first_registered": first_reg,
        "mileage": _int(vehicle.get("mileage")),
        "price": _int(vehicle.get("discountedPrice")) or _int(vehicle.get("rrpPrice")),
        "rrp": _int(vehicle.get("rrpPrice")),
        "fuel": vehicle.get("fuelType") or None,
        "transmission": vehicle.get("transmission") or None,
        "body_type": body_for(vehicle.get("modelName"), vehicle.get("bodystyle")),
        "body_text": vehicle.get("bodystyle"),
        "seats": int(seats.group(1)) if seats else None,
        "colour": (vehicle.get("color") or {}).get("exteriorBaseColor") or None,
        "photo_count": _int(vehicle.get("mediaCount")) or 0,
        "van": is_van(vehicle.get("vehicleType"), vehicle.get("bodystyle")),
        "dealer": re.sub(r"\s+", " ", str(dealer.get("dealerName") or "")).strip() or None,
        "dealer_id": dealer.get("dealerId"),
        "distance_miles": int(round(distance)) if distance is not None else None,
        "url": advert_url(vehicle),
    }


def record_wanted(c: dict[str, Any], car: dict[str, Any], models: list[str] | None = None) -> bool:
    if c.get("van"):
        return False
    if models and c.get("site_model") not in models:
        return False
    wanted_trim = str(car.get("trim") or "").strip().lower()
    if wanted_trim and wanted_trim not in f"{c.get('trim') or ''} {c.get('grade') or ''}".lower():
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


def record_to_row(c: dict[str, Any]) -> dict[str, Any] | None:
    reg = c.get("registration")
    if not reg:
        return None
    count = int(c.get("photo_count") or 0)
    if count > 1:
        photo_status, photo_reason = "photos", f"{count} dealer images"
    elif count == 1:
        photo_status, photo_reason = "awaiting", "Only one image; treating as stock/awaiting"
    else:
        photo_status, photo_reason = "awaiting", "No dealer images yet"
    title = " ".join(str(p) for p in (c.get("year"), "Nissan", c.get("model"), c.get("trim"), reg) if p)
    raw = {k: c.get(k) for k in ("vin", "has_plate", "site_model", "grade", "body_text", "first_registered",
                                 "mileage", "price", "rrp", "dealer", "dealer_id", "distance_miles", "url")}
    return standardise({
        "registration": reg,
        "make": "Nissan",
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
        "location": None,
        "distance_miles": c.get("distance_miles"),
        "url": c.get("url") or SEARCH_PAGE,
        "photo_status": photo_status,
        "photo_count": count,
        "photo_reason": photo_reason,
        "title": title,
        "raw_text": json.dumps(raw, ensure_ascii=False),
        "source": SOURCE_KEY,
        "status": "active",
        "last_seen": now_iso(),
    })


def search(car: dict[str, Any], settings: dict[str, Any], timings: dict[str, Any] | None = None) -> SearchResult:
    global _token
    if not is_nissan(car.get("make")):
        raise RuntimeError(f"Nissan source does not search make '{car.get('make')}'")
    _token = None  # a fresh token for each search
    models: list[str] = []
    if car.get("model"):
        models = models_for(str(car["model"]), KNOWN_MODELS)
        if not models:
            models = models_for(str(car["model"]), site_models())
        if not models:
            raise RuntimeError(f"Nissan has no model matching '{car['model']}' right now")

    home = postcode_location(settings.get("home_postcode"))
    result = SearchResult(search_url=search_page_url(car, models))
    seen: set[str] = set()
    seen_ids: set[str] = set()
    pages_log: list[dict[str, Any]] = []
    page = 1
    while page <= MAX_PAGES:
        if page > 1:
            time.sleep(PAGE_DELAY + random.uniform(0, PAGE_JITTER))
        started = time.perf_counter()
        try:
            inventory = _inventory(build_input(car, models, page, home))
        except RuntimeError as exc:
            raise RuntimeError(f"{exc} on results page {page}") from exc
        fetch_seconds = time.perf_counter() - started
        if timings is not None:
            timings["search_fetch_seconds"] = round(float(timings.get("search_fetch_seconds") or 0)
                                                    + fetch_seconds, 3)
        meta = inventory.get("metaData") or {}
        records = [parse_vehicle(v) for v in inventory.get("vehicles") or [] if isinstance(v, dict)]
        new = [c for c in records if c.get("vin") not in seen_ids]
        rows = 0
        for c in new:
            seen_ids.add(c.get("vin"))
            if c.get("registration"):
                result.raw_regs.add(c["registration"])
            if not record_wanted(c, car, models):
                continue
            row = record_to_row(c)
            if not row or row["registration"] in seen:
                continue
            if body_and_seats_match(car, row.get("body_type"), row.get("seats")):
                seen.add(row["registration"])
                result.rows.append(row)
                rows += 1
        pages_log.append({"search": car.get("name"), "page": page, "fetch_seconds": round(fetch_seconds, 3),
                          "vehicle_objects": len(records), "rows": rows, "total": _int(meta.get("totalCount"))})
        if not new or not meta.get("hasMorePages") or len(records) < PAGE_SIZE:
            break
        page += 1
    if timings is not None:
        timings.setdefault("pages", []).extend(pages_log)
    result.debug_text = "\n".join(json.dumps(p) for p in pages_log)
    return result
