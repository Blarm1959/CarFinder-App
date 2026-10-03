"""Mazda (Mazda Selected approved used) source module.

Searches https://www.mazdausedcarlocator.co.uk, the "see used car stock" link on
mazda.co.uk's Mazda Selected page (shop.mazda.co.uk is new cars only).  The site
is a NetDirector Vue app that asks NetDirector's vehicle-search GraphQL API for
cars:

* ``POST https://production-api.search-api.netdirector.auto/api/vehicle-search
  ?uuid=<site uuid>`` with ``{"query": "query { getAll(...) { ... } }"}`` and the
  site's fixed public key in the ``Authorization`` header (no "Bearer", no
  cookie, no token exchange).  Introspection is allowed;
* ``getAll(searchParams, pagination: {currentPage, pageSize}, sortParams)`` gives
  the cars and ``getCount(searchParams)`` how many there are.  ``pageSize`` can
  be 100.  Without a full sort order the pages overlap (2899 rows held only 2667
  different cars), so the sort is always price then id;
* filters that really work and are sent: ``manufacturer``, ``model`` (exact site
  names, e.g. ``CX-30``, ``2 Hybrid``, ``MX-5 RF``), ``status`` (available and
  reserved; sold cars stay listed for a while), ``type`` (Car),
  ``currentPrice``/``odometerMiles``/``registrationYear`` ranges
  (``{from, to}``) and ``transmissionType`` (Automatic also covers "CVT").  Fuel
  names are not consistent ("Hybrid", "HYBRID ELECTRIC", "PETROL/MHEV"), so fuel
  is only checked here;
* each car carries the registration, first-registration date, price and
  reduced-from price, mileage, colour, seats, number of photos and the dealer's
  name and latitude/longitude (distance is the straight line from the home
  postcode).  A few dealers list unregistered stock (10 miles, "Choice of
  Colours") with a stock number or a placeholder such as ``NEW4321`` in place of
  the plate: these have no real registration and are left out;
* the site's body style is not reliable (CX-5s and CX-60s are "Estate", MX-5s
  "Sports"), so the body type comes from the model.
"""
from __future__ import annotations

import json
import random
import re
import time
from typing import Any
from urllib.parse import quote, urlencode

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

SOURCE_KEY = "mazda"
SOURCE_NAME = "Mazda Selected (approved used)"
MAKES = ("Mazda",)

SITE_ROOT = "https://www.mazdausedcarlocator.co.uk"
SEARCH_PAGE = SITE_ROOT + "/used-cars/"
API_URL = ("https://production-api.search-api.netdirector.auto/api/vehicle-search"
           "?uuid=f3925000-fd4f-11ee-8567-dd720d9a728f")
API_KEY = "115afa10-fd50-11ee-a1c8-2753cc7fbaac"  # the site's public key, sent by every visitor's browser
PAGE_SIZE = 100
MAX_PAGES = 60
IMPERSONATE = "chrome"
PAGE_DELAY = 0.5
PAGE_JITTER = 0.5
RETRY_WAITS = (10.0, 30.0)

# The site's model names (Oct 2026), used when the live list cannot be read.
KNOWN_MODELS: tuple[str, ...] = ("2", "2 Hybrid", "3", "6", "CX-3", "CX-30", "CX-5", "CX-60", "CX-80", "Cx 80",
                                 "MX-30", "MX-5", "MX-5 RF")
STATUSES = ("available", "reserved")
GEAR_VALUES = {"Automatic": ["Automatic", "CVT"], "Manual": ["Manual"]}

# Body type by model (site labels are unreliable).  Longest key first wins.
MODEL_BODIES: dict[str, str] = {
    "cx3": "SUV", "cx30": "SUV", "cx5": "SUV", "cx60": "SUV", "cx7": "SUV", "cx80": "SUV", "cx9": "SUV",
    "mx30": "SUV",
    "2": "Hatchback", "2hybrid": "Hatchback", "3": "Hatchback",
    "5": "MPV", "premacy": "MPV",
    "6": "Saloon",
}
# Coupes and convertibles: never what a body-type search asks for.
OTHER_MODELS = ("mx5", "rx8", "rx7")
SALOON_WORDS = re.compile(r"\b(saloon|fastback|4dr|4 door)\b", re.I)
ESTATE_WORDS = re.compile(r"\b(estate|tourer|wagon)\b", re.I)
PLACEHOLDER_PLATE = re.compile(r"^(NEW|TBC|TBA|REG)\d*$")

FIELDS = ("id status type manufacturer model variant trim bodyStyle numSeats numDoors numImages "
          "registration { number date year } odometer { value unit } fuel { type } transmission { type } "
          "colour { exterior exteriorGeneric } price { current previous vatIncluded } "
          "location { name town coordinates { lat lon } }")

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-GB,en;q=0.9",
    "Authorization": API_KEY,
    "Content-Type": "application/json",
    "Origin": SITE_ROOT,
    "Referer": SEARCH_PAGE,
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


def _model_key(name: Any) -> str:
    """"Mazda CX-30" -> "cx30" (some names carry the make)."""
    key = _plain(name)
    return key[5:] if key.startswith("mazda") and len(key) > 5 else key


def model_matches(wanted: str, name: str | None) -> bool:
    """True when a site model name starts with what the user typed ("2" matches "2 Hybrid")."""
    typed = _model_key(wanted)
    return not typed or _model_key(name).startswith(typed)


def models_for(model: str, known: tuple[str, ...] | list[str]) -> list[str]:
    if not _plain(model):
        return []
    return [name for name in known if model_matches(model, name)]


# ---------------------------------------------------------------------------
# Car search -> GraphQL query
# ---------------------------------------------------------------------------

def _gql(value: Any) -> str:
    """A Python value as a GraphQL literal (strings quoted, lists bracketed, dict keys bare)."""
    if isinstance(value, dict):
        return "{" + ", ".join(f"{k}: {_gql(v)}" for k, v in value.items()) + "}"
    if isinstance(value, (list, tuple)):
        return "[" + ", ".join(_gql(v) for v in value) + "]"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(int(value)) if float(value).is_integer() else str(value)
    return json.dumps(str(value))


def build_search(car: dict[str, Any], models: list[str]) -> dict[str, Any]:
    params: dict[str, Any] = {"condition": ["Used"], "manufacturer": ["Mazda"], "type": ["Car"],
                              "status": list(STATUSES)}
    if models:
        params["model"] = list(models)
    low, high = _int(car.get("price_min")), _int(car.get("price_max"))
    if low is not None or high is not None:
        params["currentPrice"] = [{k: v for k, v in (("from", low), ("to", high)) if v is not None}]
    if car.get("mileage_max") is not None:
        params["odometerMiles"] = [{"to": int(car["mileage_max"])}]
    if car.get("year_min") is not None:
        params["registrationYear"] = [{"from": int(car["year_min"])}]
    gear = GEAR_VALUES.get(str(car.get("transmission") or "Any"))
    if gear:
        params["transmissionType"] = list(gear)
    return params


def build_query(params: dict[str, Any], page: int) -> str:
    sort = "[{fieldName: currentPrice, direction: asc}, {fieldName: id, direction: asc}]"
    return (f"query {{ total: getCount(searchParams: {_gql(params)}) "
            f"cars: getAll(searchParams: {_gql(params)}, pagination: {{currentPage: {page}, pageSize: {PAGE_SIZE}}}, "
            f"sortParams: {sort}) {{ {FIELDS} }} }}")


def search_page_url(car: dict[str, Any], models: list[str]) -> str:
    """The same search on the site, for the "open search" link."""
    query: list[tuple[str, str]] = [("manufacturer[0]", "Mazda")]
    query += [(f"model[{i}]", name) for i, name in enumerate(models)]
    if car.get("price_min") is not None:
        query.append(("currentPrice[from]", str(int(car["price_min"]))))
    if car.get("price_max") is not None:
        query.append(("currentPrice[to]", str(int(car["price_max"]))))
    query.append(("nfcSearchVersion", "1.0.0"))
    return SEARCH_PAGE + "?" + urlencode(query)


_session: Any = None


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


def _headers() -> dict[str, str]:
    return {k: v for k, v in HEADERS.items() if not (cffi_requests is not None and k == "User-Agent")}


def _post(query: str, timeout: int = 60) -> dict[str, Any]:
    """POST one GraphQL query; on a 403/429 wait, start a fresh session and try again."""
    global _session
    attempts = len(RETRY_WAITS) + 1
    response = None
    for attempt in range(attempts):
        response = _http().post(API_URL, data=json.dumps({"query": query}), headers=_headers(), timeout=timeout)
        if response.status_code not in (403, 429):
            response.raise_for_status()
            try:
                data = response.json()
            except ValueError as exc:
                raise RuntimeError(f"Mazda returned something that is not car data; fetched with {_fetcher()}") from exc
            if not isinstance(data, dict) or data.get("errors") or not isinstance(data.get("data"), dict):
                message = "; ".join(str(e.get("message")) for e in (data or {}).get("errors") or []
                                    if isinstance(e, dict)) or "no data"
                raise RuntimeError(f"Mazda search API refused the query ({message})")
            return data["data"]
        if attempt < len(RETRY_WAITS):
            _session = None
            time.sleep(RETRY_WAITS[attempt])
    status = response.status_code if response is not None else "?"
    server = (response.headers.get("Server") if response is not None else None) or "the site"
    raise RuntimeError(f"Mazda blocked the request (HTTP {status} from {server}, {attempts} tries; "
                       f"fetched with {_fetcher()})")


def parse_site_models(data: dict[str, Any]) -> tuple[str, ...]:
    attrs = (data or {}).get("getFilterAttributes") or {}
    return tuple(str(i.get("label")) for i in ((attrs.get("model") or {}).get("items") or [])
                 if isinstance(i, dict) and i.get("label"))


def site_models() -> tuple[str, ...]:
    """The site's current model names (in case a new model has been added)."""
    data = _post('query { getFilterAttributes(searchParams: {condition: ["Used"], manufacturer: ["Mazda"]}) '
                 '{ model { items { label count } } } }', timeout=30)
    return parse_site_models(data)


# ---------------------------------------------------------------------------
# API record -> car
# ---------------------------------------------------------------------------

def body_for(model: Any, variant: Any = None, site_body: Any = None) -> str | None:
    key = _model_key(model)
    if any(key.startswith(m) for m in OTHER_MODELS):
        return None
    text = f"{variant or ''} {site_body or ''}"
    by_model = None
    for name in sorted(MODEL_BODIES, key=len, reverse=True):
        if key == name or (key.startswith(name) and not name.isdigit()):
            by_model = MODEL_BODIES[name]
            break
    if by_model in ("Hatchback", "Saloon"):
        # The 3 also came as a saloon ("Fastback") and the 6 as an estate ("Tourer").
        if ESTATE_WORDS.search(text):
            return "Estate"
        if SALOON_WORDS.search(text):
            return "Saloon"
        if by_model == "Saloon" and "hatch" in text.lower():
            return "Hatchback"
        return by_model
    if by_model:
        return by_model
    site = _plain(site_body)
    if any(w in site for w in ("convertible", "coupe", "sports", "roadster")):
        return None
    return classify_body_type(site_body, variant)


def other_body(c: dict[str, Any]) -> bool:
    return c.get("body_type") is None and (any(_model_key(c.get("model")).startswith(m) for m in OTHER_MODELS)
                                           or any(w in _plain(c.get("body_text")) for w in
                                                  ("convertible", "coupe", "sports", "roadster")))


def tidy_fuel(text: Any) -> str | None:
    raw = re.sub(r"\s+", " ", str(text or "")).strip()
    fuel = raw.lower()
    if not fuel or fuel in ("n/a", "na"):
        return None
    base = "Diesel" if "diesel" in fuel else "Petrol"
    if "phev" in fuel or "plug-in" in fuel or "plug in" in fuel:
        return f"{base} Plug-in Hybrid"
    if "hybrid" in fuel or "mhev" in fuel:
        return f"{base} Hybrid"
    if "electric" in fuel:
        return "Electric"
    if fuel in ("petrol", "diesel"):
        return fuel.capitalize()
    return raw


def tidy_colour(colour: dict[str, Any] | None) -> str | None:
    colour = colour or {}
    for text in (colour.get("exterior"), colour.get("exteriorGeneric")):
        text = re.sub(r"\s+", " ", str(text or "")).strip()
        if text and text.lower() not in ("choice of colours", "n/a"):
            return text.title() if text.isupper() or text.islower() else text
    return None


def plate_for(number: Any) -> str | None:
    """A real UK/NI registration, or None for stock numbers and placeholders ("526844", "NEW4321")."""
    compact = re.sub(r"[^A-Z0-9]", "", str(number or "").upper())
    if PLACEHOLDER_PLATE.match(compact):
        return None
    return normalise_plate(compact)


def advert_url(vehicle_id: Any, model: Any, variant: Any) -> str:
    """The locator's advert page ("/used-cars/21739024-Mazda-CX-30-2.0 SKYACTIV-G .../")."""
    words = [str(vehicle_id), "Mazda", str(model or "").strip(), str(variant or "").replace("/", "").strip()]
    slug = "-".join(w for w in words if w)
    return f"{SITE_ROOT}/used-cars/{quote(slug, safe='-()[].,+')}/"


def parse_vehicle(v: dict[str, Any]) -> dict[str, Any]:
    """Everything CarFinder needs from one API record (no filtering)."""
    reg = v.get("registration") or {}
    price = v.get("price") or {}
    coords = (v.get("location") or {}).get("coordinates") or {}
    lat, lon = coords.get("lat"), coords.get("lon")
    current, previous = _int(price.get("current")), _int(price.get("previous"))
    first_reg = str(reg.get("date") or "")[:10] or None
    year = _int(reg.get("year"))
    if year is not None and year < 1950:
        year = None
    model = str(v.get("model") or "").strip() or None
    variant = re.sub(r"\s+", " ", str(v.get("variant") or "")).strip() or None
    return {
        "id": v.get("id"),
        "status": str(v.get("status") or "").lower(),
        "type": v.get("type"),
        "make": str(v.get("manufacturer") or "").strip() or None,
        "registration": plate_for(reg.get("number")),
        "model": model,
        "trim": variant,
        "year": year or (_int(first_reg[:4]) if first_reg else None),
        "first_registered": first_reg,
        "mileage": _int((v.get("odometer") or {}).get("value")),
        "price": current,
        "previous_price": previous if previous and current and previous > current else None,
        "vat_included": price.get("vatIncluded"),
        "fuel": tidy_fuel((v.get("fuel") or {}).get("type")),
        "transmission": (v.get("transmission") or {}).get("type"),
        "body_type": body_for(model, variant, v.get("bodyStyle")),
        "body_text": v.get("bodyStyle"),
        "seats": _int(v.get("numSeats")),
        "colour": tidy_colour(v.get("colour")),
        "photo_count": _int(v.get("numImages")) or 0,
        "dealer": str((v.get("location") or {}).get("name") or "").strip() or None,
        "location": str((v.get("location") or {}).get("town") or "").strip() or None,
        "lat_lon": (float(lat), float(lon)) if lat is not None and lon is not None else None,
        "url": advert_url(v.get("id"), model, variant),
    }


def is_live(c: dict[str, Any]) -> bool:
    return c.get("status") in STATUSES


def record_wanted(c: dict[str, Any], car: dict[str, Any], models: list[str] | None = None) -> bool:
    if _plain(c.get("make")) != "mazda" or not is_live(c):
        return False
    if c.get("type") not in (None, "Car") or c.get("vat_included") is False:
        return False  # commercials priced "+ VAT"
    if models and c.get("model") not in models:
        return False
    wanted_trim = str(car.get("trim") or "").strip().lower()
    if wanted_trim and wanted_trim not in str(c.get("trim") or "").lower():
        return False
    if str(car.get("body_type") or "Any") != "Any" and other_body(c):
        return False
    if not fuel_matches(str(car.get("fuel") or "Any"), str(c.get("fuel") or "")):
        return False
    if not transmission_matches(str(car.get("transmission") or "Any"), str(c.get("transmission") or "")):
        return False
    price, mileage, year = c.get("price"), c.get("mileage"), c.get("year")
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
    return True


def record_to_row(c: dict[str, Any], home: tuple[float, float] | None) -> dict[str, Any] | None:
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
    title = " ".join(str(p) for p in (c.get("year"), "Mazda", c.get("model"), c.get("trim"), reg) if p)
    raw = {k: c.get(k) for k in ("id", "status", "body_text", "first_registered", "mileage", "price",
                                 "previous_price", "dealer", "lat_lon", "url")}
    return standardise({
        "registration": reg,
        "make": "Mazda",
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
        "previous_price": c.get("previous_price"),
        "dealer": c.get("dealer"),
        "location": c.get("location"),
        "distance_miles": distance_miles(home, c.get("lat_lon")) if c.get("lat_lon") else None,
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
    if _plain(car.get("make")) != "mazda":
        raise RuntimeError(f"Mazda source does not search make '{car.get('make')}'")
    models: list[str] = []
    if car.get("model"):
        models = models_for(str(car["model"]), KNOWN_MODELS)
        try:
            live = models_for(str(car["model"]), site_models())
        except Exception:  # noqa: BLE001 - fall back to the known list
            live = []
        models += [m for m in live if m not in models]
        if not models:
            raise RuntimeError(f"Mazda has no model matching '{car['model']}' right now")

    home = postcode_location(settings.get("home_postcode"))
    params = build_search(car, models)
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
            data = _post(build_query(params, page))
        except RuntimeError as exc:
            raise RuntimeError(f"{exc} on results page {page}") from exc
        fetch_seconds = time.perf_counter() - started
        if timings is not None:
            timings["search_fetch_seconds"] = round(float(timings.get("search_fetch_seconds") or 0)
                                                    + fetch_seconds, 3)
        vehicles = [v for v in data.get("cars") or [] if isinstance(v, dict)]
        records = [parse_vehicle(v) for v in vehicles]
        new = [c for c in records if str(c.get("id")) not in seen_ids]
        rows = 0
        for c in new:
            seen_ids.add(str(c.get("id")))
            if c.get("registration") and is_live(c) and _plain(c.get("make")) == "mazda":
                result.raw_regs.add(c["registration"])
            if not record_wanted(c, car, models):
                continue
            row = record_to_row(c, home)
            if not row or row["registration"] in seen:
                continue
            if body_and_seats_match(car, row.get("body_type"), row.get("seats")):
                seen.add(row["registration"])
                result.rows.append(row)
                rows += 1
        total = _int(data.get("total"))
        pages_log.append({"search": car.get("name"), "page": page, "fetch_seconds": round(fetch_seconds, 3),
                          "vehicle_objects": len(records), "rows": rows, "total": total})
        if not new or len(records) < PAGE_SIZE or (total is not None and page * PAGE_SIZE >= total):
            break
        page += 1
    if timings is not None:
        timings.setdefault("pages", []).extend(pages_log)
    result.debug_text = "\n".join(json.dumps(p) for p in pages_log)
    return result
