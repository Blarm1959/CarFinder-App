"""Land Rover and Jaguar (JLR approved used) source module.

Searches https://buy.landrover.co.uk and https://buy.jaguar.co.uk, the approved
used sites linked from landrover.co.uk (via used.landrover.co.uk) and from
jaguar.com's UK Approved Used page.  Both run on the Rockar storefront (a Next.js
app) and ask the same tRPC call for cars, each on its own host:

* ``GET /api/trpc/product.getProducts?input=<JSON>`` with
  ``{"json": {"category": "used", "handle": "jlr_used", "products": {...}}}``.
  ``activeDistanceFilter`` must be left out (null is refused).  The call needs
  the site's session cookie (HttpOnly, set when the results page is opened);
  without it the answer is HTTP 500 "Couldn't authenticate client".  So the
  results page is opened first in the same session, and opened again (fresh
  session) if the API still says it cannot authenticate;
* ``products.first`` is the page size (250 works) and ``products.after`` the
  number of cars to skip; the answer has ``totalCount``;
* ``activeSorting`` ``{"method": "price", "order": "ASC"}`` sorts cheapest
  first, so the search stops as soon as prices pass the maximum;
* filters are ``activeFilters`` ``{"code": ..., "values": [...]}``; several values
  are OR'd.  Only ``range`` (exact site model names, e.g. ``Range Rover Sport``,
  ``Defender 110``, ``XF Sportbrake``) and ``transmission`` are sent.  The
  ``price``, ``mileage`` and ``modelYear`` filters are not ranges: two values
  match those two exact prices (the site's own price filter link finds 0 cars),
  and anything else is a server error, so every limit is applied here.  Fuel
  names vary ("Plug In Hybrid Petrol", "Petrol Plug-in Hybrid", "Hybrid
  Electric"), so fuel is only checked here;
* each car carries the registration, first-registration date, price, mileage,
  colour, photos and the selling retailer with its town and latitude/longitude
  (distance is the straight line from the home postcode);
* commercials (Defender Hard Top, Discovery Commercial) are marked
  ``vehicleType`` "commercial" and/or ``commandsVat`` and are left out;
* the site's body style is not reliable (an Evoque can be "Hatchback" or
  "Estate", a Range Rover Sport "Estate"), so the body type comes from the model.
"""
from __future__ import annotations

import json
import random
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import quote, urlencode

import requests

try:  # A real Chrome TLS handshake, as for Spoticar (the site has not needed it so far).
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

SOURCE_KEY = "jlr"
SOURCE_NAME = "Land Rover & Jaguar Approved Used"
MAKES = ("Land Rover", "Jaguar")


@dataclass(frozen=True)
class Brand:
    make: str
    site_root: str
    known_models: tuple[str, ...]


BRANDS: dict[str, Brand] = {
    "landrover": Brand("Land Rover", "https://buy.landrover.co.uk",
                       ("Defender", "Defender 90", "Defender 110", "Defender 130", "Discovery", "Discovery Sport",
                        "Range Rover", "Range Rover Evoque", "Range Rover Sport", "Range Rover Velar")),
    "jaguar": Brand("Jaguar", "https://buy.jaguar.co.uk",
                    ("E-PACE", "F-PACE", "F-TYPE", "I-PACE", "XE", "XF", "XF Sportbrake")),
}

API_PATH = "/api/trpc/product.getProducts"
LIST_PATH = "/approved-used/vehicles"
PAGE_SIZE = 250
MAX_PAGES = 40
IMPERSONATE = "chrome"
PAGE_DELAY = 0.5
PAGE_JITTER = 0.5
RETRY_WAITS = (10.0, 30.0)
GEAR_VALUES = {"Automatic": ["Automatic", "Semi Automatic"], "Manual": ["Manual"]}

# Body type by model (site labels are unreliable).  Longest key first wins.
MODEL_BODIES: dict[str, str] = {
    "defender": "SUV", "discovery": "SUV", "freelander": "SUV", "rangerover": "SUV",
    "epace": "SUV", "fpace": "SUV", "ipace": "SUV",
    "xe": "Saloon", "xf": "Saloon", "xj": "Saloon", "xfsportbrake": "Estate",
}
OTHER_MODELS = ("ftype", "xk")  # coupes and convertibles
COMMERCIAL_WORDS = re.compile(r"\b(hard ?top|commercial|van)\b", re.I)

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-GB,en;q=0.9",
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


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def brand_for(make: Any) -> Brand | None:
    wanted = _plain(make)
    if wanted in ("landrover", "rangerover", "jlr"):
        return BRANDS["landrover"]
    if wanted == "jaguar":
        return BRANDS["jaguar"]
    return None


def _model_key(name: Any, make: str = "") -> str:
    key = _plain(name)
    prefix = _plain(make)
    return key[len(prefix):] if prefix and key.startswith(prefix) and len(key) > len(prefix) else key


def model_matches(wanted: str, name: str | None, make: str = "") -> bool:
    """True when a site model name starts with what the user typed ("Defender" matches "Defender 110")."""
    typed = _model_key(wanted, make)
    return not typed or _model_key(name, make).startswith(typed)


def models_for(model: str, known: tuple[str, ...] | list[str], make: str = "") -> list[str]:
    if not _plain(model):
        return []
    return [name for name in known if model_matches(model, name, make)]


# ---------------------------------------------------------------------------
# Car search -> request
# ---------------------------------------------------------------------------

def build_input(car: dict[str, Any], models: list[str], after: int = 0, first: int = PAGE_SIZE) -> dict[str, Any]:
    filters: list[dict[str, Any]] = [{"code": "relatedLocalStore", "values": []}]
    if models:
        filters.append({"code": "range", "values": list(models)})
    gear = GEAR_VALUES.get(str(car.get("transmission") or "Any"))
    if gear:
        filters.append({"code": "transmission", "values": list(gear)})
    products = {"first": first, "after": after, "finance": {"financeType": "cash"}, "activeFilters": filters,
                "productTaxonomyFilters": [], "activeSorting": {"method": "price", "order": "ASC"}}
    return {"json": {"category": "used", "handle": "jlr_used", "totalCountOnly": False, "filterCodes": [],
                     "products": products}}


def api_url(brand: Brand, payload: dict[str, Any]) -> str:
    return brand.site_root + API_PATH + "?input=" + quote(json.dumps(payload, separators=(",", ":")), safe="")


def search_page_url(brand: Brand, models: list[str]) -> str:
    """The same search on the site, for the "open search" link."""
    query = [("radius", "nationwide"), ("financeMethod", "cash"),
             ("categorySorting", json.dumps({"method": "price", "order": "ASC"}, separators=(",", ":")))]
    if models:
        query.append(("categoryFilters", json.dumps({"range": list(models)}, separators=(",", ":"))))
    return brand.site_root + LIST_PATH + "?" + urlencode(query)


_session: Any = None
_warm: set[str] = set()  # site roots whose results page this session has opened


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
        _warm.clear()
    return _session


def _headers(brand: Brand, page: bool = False) -> dict[str, str]:
    headers = {k: v for k, v in HEADERS.items() if not (cffi_requests is not None and k == "User-Agent")}
    if page:
        headers["Accept"] = "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"
    else:
        headers["Referer"] = brand.site_root + LIST_PATH
    return headers


def _warm_up(brand: Brand, timeout: int = 60) -> None:
    """Open the results page once per session, so the site sets its session cookie."""
    session = _http()  # a fresh session forgets which pages it has opened
    if brand.site_root in _warm:
        return
    response = session.get(brand.site_root + LIST_PATH + "?radius=nationwide&financeMethod=cash",
                           headers=_headers(brand, page=True), timeout=timeout)
    if response.status_code in (403, 429):
        return  # _fetch's retries deal with blocks
    response.raise_for_status()
    _warm.add(brand.site_root)


def _no_client_auth(response: Any) -> bool:
    return response.status_code == 500 and "authenticate client" in str(getattr(response, "text", "") or "")


def _fetch(brand: Brand, payload: dict[str, Any], timeout: int = 60) -> dict[str, Any]:
    """One getProducts call; on a 403/429 (or a lost session) wait, start a fresh session and try again."""
    global _session
    attempts = len(RETRY_WAITS) + 1
    response = None
    for attempt in range(attempts):
        _warm_up(brand, timeout)
        response = _http().get(api_url(brand, payload), headers=_headers(brand), timeout=timeout)
        if response.status_code not in (403, 429) and not _no_client_auth(response):
            response.raise_for_status()
            try:
                data = response.json()
            except ValueError as exc:
                raise RuntimeError(f"{brand.make} returned something that is not car data; "
                                   f"fetched with {_fetcher()}") from exc
            category = (((data or {}).get("result") or {}).get("data") or {}).get("json") or {}
            category = category.get("productCategory") if isinstance(category, dict) else None
            if not isinstance(category, dict) or not isinstance((category.get("products") or {}).get("edges"), list):
                raise RuntimeError(f"{brand.make} returned an unexpected answer (no products list)")
            return category
        if attempt < len(RETRY_WAITS):
            _session = None
            time.sleep(RETRY_WAITS[attempt])
    status = response.status_code if response is not None else "?"
    if response is not None and _no_client_auth(response):
        raise RuntimeError(f"{brand.make} would not give this session car data (HTTP 500 \"Couldn't authenticate "
                           f"client\" after opening the results page, {attempts} tries; fetched with {_fetcher()})")
    server = (response.headers.get("Server") if response is not None else None) or "the site"
    raise RuntimeError(f"{brand.make} blocked the request (HTTP {status} from {server}, {attempts} tries; "
                       f"fetched with {_fetcher()})")


def parse_site_models(category: dict[str, Any]) -> tuple[str, ...]:
    for f in (category or {}).get("productFilters") or []:
        if isinstance(f, dict) and f.get("code") == "range":
            return tuple(str(o.get("value")) for o in f.get("options") or [] if isinstance(o, dict) and o.get("value"))
    return ()


def site_models(brand: Brand) -> tuple[str, ...]:
    """The site's current model names (in case a new model has been added)."""
    return parse_site_models(_fetch(brand, build_input({}, [], 0, 1), timeout=30))


# ---------------------------------------------------------------------------
# Site record -> car
# ---------------------------------------------------------------------------

def body_for(model: Any, site_body: Any = None) -> str | None:
    key = _plain(model)
    if any(key.startswith(m) for m in OTHER_MODELS):
        return None
    for name in sorted(MODEL_BODIES, key=len, reverse=True):
        if key.startswith(name):
            return MODEL_BODIES[name]
    site = _plain(site_body)
    if any(w in site for w in ("coupe", "convertible", "roadster")):
        return None
    return classify_body_type(site_body)


def tidy_fuel(text: Any) -> str | None:
    raw = re.sub(r"\s+", " ", str(text or "")).strip()
    fuel = raw.lower()
    if not fuel:
        return None
    base = "Diesel" if "diesel" in fuel else "Petrol"
    if "plug" in fuel or "phev" in fuel:
        return f"{base} Plug-in Hybrid"
    if "hybrid" in fuel or "mhev" in fuel:
        return f"{base} Hybrid"
    if "electric" in fuel:
        return "Electric"
    if fuel in ("petrol", "diesel"):
        return fuel.capitalize()
    return raw


def tidy_colour(text: Any) -> str | None:
    colour = re.sub(r"\s+", " ", str(text or "")).strip()
    if not colour:
        return None
    return colour.title() if colour.isupper() or colour.islower() else colour


def is_commercial(c: dict[str, Any]) -> bool:
    if str(c.get("vehicle_type") or "").lower() == "commercial" or c.get("commands_vat"):
        return True
    return bool(COMMERCIAL_WORDS.search(f"{c.get('title') or ''} {c.get('trim') or ''}"))


def parse_node(node: dict[str, Any], brand: Brand) -> dict[str, Any]:
    """Everything CarFinder needs from one product (no filtering)."""
    stores = node.get("relatedLocalStore") or []
    store = stores[0] if stores and isinstance(stores[0], dict) else {}
    lat, lon = _float(store.get("latitude")), _float(store.get("longitude"))
    first_reg = str(node.get("registrationDate") or "")[:10] or None
    model = str(node.get("model") or "").strip() or None
    slug = str(node.get("urlSlug") or "").strip()
    images = node.get("images") or []
    return {
        "id": node.get("id"),
        "make": brand.make,
        "registration": normalise_plate(node.get("registrationNumber")),
        "model": model,
        "trim": str(node.get("modelVariant") or "").strip() or None,
        "title": node.get("title"),
        "subtitle": node.get("subtitle"),
        "year": (_int(first_reg[:4]) if first_reg else None) or _int(node.get("modelYear")),
        "first_registered": first_reg,
        "mileage": _int(node.get("mileage")),
        "price": _int(node.get("price")),
        "fuel": tidy_fuel(node.get("fuelType")),
        "transmission": node.get("transmission"),
        "body_type": body_for(model, node.get("bodyType")),
        "body_text": node.get("bodyType"),
        "colour": tidy_colour(node.get("exteriorColour")),
        "photo_count": len([i for i in images if isinstance(i, dict) and i.get("imageUrl")]),
        "dealer": str(store.get("name") or "").strip() or None,
        "location": str(store.get("city") or "").strip() or None,
        "postcode": store.get("postcode"),
        "lat_lon": (lat, lon) if lat is not None and lon is not None else None,
        "vehicle_type": node.get("vehicleType"),
        "commands_vat": node.get("commandsVat"),
        "condition": node.get("productCondition"),
        "url": f"{brand.site_root}/approved-used/vehicle/{slug}" if slug else None,
    }


def record_wanted(c: dict[str, Any], car: dict[str, Any], models: list[str] | None = None) -> bool:
    if is_commercial(c) or str(c.get("condition") or "used").lower() != "used":
        return False
    if models and c.get("model") not in models:
        return False
    wanted_trim = str(car.get("trim") or "").strip().lower()
    if wanted_trim and wanted_trim not in f"{c.get('trim') or ''} {c.get('subtitle') or ''}".lower():
        return False
    if str(car.get("body_type") or "Any") != "Any" and c.get("body_type") is None:
        return False  # F-TYPE and other coupes/convertibles
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


def dealer_location(c: dict[str, Any]) -> tuple[float, float] | None:
    return c.get("lat_lon") or place_location(c.get("location"))


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
    where = dealer_location(c)
    title = " ".join(str(p) for p in (c.get("year"), c.get("make"), c.get("model"), c.get("trim"), reg) if p)
    raw = {k: c.get(k) for k in ("id", "title", "subtitle", "body_text", "first_registered", "mileage", "price",
                                 "dealer", "postcode", "lat_lon", "url")}
    return standardise({
        "registration": reg,
        "make": c.get("make"),
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
        "url": c.get("url"),
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
    brand = brand_for(car.get("make"))
    if not brand:
        raise RuntimeError(f"JLR source does not search make '{car.get('make')}'")
    models: list[str] = []
    if car.get("model"):
        models = models_for(str(car["model"]), brand.known_models, brand.make)
        try:
            live = models_for(str(car["model"]), site_models(brand), brand.make)
        except Exception:  # noqa: BLE001 - fall back to the known list
            live = []
        models += [m for m in live if m not in models]
        if not models:
            raise RuntimeError(f"{brand.make} has no model matching '{car['model']}' right now")

    home = postcode_location(settings.get("home_postcode"))
    result = SearchResult(search_url=search_page_url(brand, models))
    price_max = _int(car.get("price_max"))
    seen: set[str] = set()
    seen_ids: set[str] = set()
    pages_log: list[dict[str, Any]] = []
    page = 1
    while page <= MAX_PAGES:
        if page > 1:
            time.sleep(PAGE_DELAY + random.uniform(0, PAGE_JITTER))
        started = time.perf_counter()
        try:
            category = _fetch(brand, build_input(car, models, (page - 1) * PAGE_SIZE))
        except RuntimeError as exc:
            raise RuntimeError(f"{exc} on results page {page}") from exc
        fetch_seconds = time.perf_counter() - started
        if timings is not None:
            timings["search_fetch_seconds"] = round(float(timings.get("search_fetch_seconds") or 0)
                                                    + fetch_seconds, 3)
        products = category.get("products") or {}
        records = [parse_node(e.get("node") or {}, brand) for e in products.get("edges") or []
                   if isinstance(e, dict) and isinstance(e.get("node"), dict)]
        new = [c for c in records if str(c.get("id")) not in seen_ids]
        rows = 0
        for c in new:
            seen_ids.add(str(c.get("id")))
            if c.get("registration") and not is_commercial(c):
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
        total = _int(products.get("totalCount"))
        pages_log.append({"search": car.get("name"), "page": page, "fetch_seconds": round(fetch_seconds, 3),
                          "vehicle_objects": len(records), "rows": rows, "total": total})
        cheapest_left = max((c.get("price") or 0 for c in records), default=0)
        if not new or len(records) < PAGE_SIZE or (total is not None and page * PAGE_SIZE >= total):
            break
        if price_max is not None and cheapest_left > price_max:
            break  # sorted cheapest first: everything after this page costs more
        page += 1
    if timings is not None:
        timings.setdefault("pages", []).extend(pages_log)
    result.debug_text = "\n".join(json.dumps(p) for p in pages_log)
    return result
