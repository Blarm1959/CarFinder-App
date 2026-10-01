"""Renault, Dacia and Alpine (Renew approved used) source module.

Searches https://uk.renew.auto, Renault Group UK's approved-used site (the
"see used vehicles" link on renault.co.uk and dacia.co.uk).  It runs on the
NetDirector dealer platform: the results page asks a JSON endpoint for cars,
24 at a time:

* ``GET /ajax/stock-listing/get-items/pageId/<page id>/...?<filters>`` returns
  ``{"count", "hasMoreResults", "vehicles": [...]}``.  No token, cookie or
  special header is needed;
* filters are the search form's fields, repeated as ``name[]=value``:
  ``section[]`` (used cars), ``make[]``, ``model[<Make>][]`` (exact site model
  names, e.g. ``Clio``, ``5 E-Tech electric``), ``budget-program[]=pay`` with
  ``budget-price-min[]`` / ``budget-price-max[]`` (cash price, either end
  optional), ``mileage[range][]=-<max>``, ``registered-at[min][]=<N> year``
  (registered within the last N years) and ``transmission[]``;
* paging is ``page=N`` (from 1), 24 cars per page; the page size cannot be
  changed.  ``order=price`` sorts cheapest first;
* the dealers also list part-exchanged cars of other makes, so the make is
  always sent; the "Used Cars" section also holds some vans, which are left out;
* fuel names are not consistent ("Electric", "E-Tech Electric", "Petrol/Electric
  Hybrid", "PETROL/MHEV" ...), so fuel is only checked here, not sent.  Nor is
  the body style: the site labels the same model several ways (a Duster can be
  "SUV", "4x4", "Estate" or "Hatchback"), so the body type comes from the model;
* each car carries the registration, VIN, first-registration date, price and
  reduced-from price, mileage, colour, body style, the dealer with its
  latitude/longitude, and the number of photos (``images_count``; a car with no
  photos yet shows one placeholder image from another host).

Every limit is applied again here, as for the other sources.
"""
from __future__ import annotations

import datetime as dt
import html
import json
import random
import re
import time
import unicodedata
from typing import Any
from urllib.parse import urlencode

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

SOURCE_KEY = "renew"
SOURCE_NAME = "Renew (Renault, Dacia, Alpine approved used)"
MAKES = ("Renault", "Dacia", "Alpine")

SITE_ROOT = "https://uk.renew.auto"
SEARCH_PAGE = SITE_ROOT + "/used-cars/search/"
SECTION = "194695"  # "Used Cars"
PAGE_ID = "1712848"  # the search page
ITEMS_URL = (f"{SITE_ROOT}/ajax/stock-listing/get-items/pageId/{PAGE_ID}/ratio/4_3/taxBandImageLink//"
             f"taxBandImageHyperlink//imgWidth/400/")
PAGE_SIZE = 24
MAX_PAGES = 150
IMPERSONATE = "chrome"
PAGE_DELAY = 0.5
PAGE_JITTER = 0.5
RETRY_WAITS = (10.0, 30.0)
PHOTO_HOST = "images.netdirector.auto"

# The site's model names (Oct 2026), used when the live list cannot be read.
KNOWN_MODELS: dict[str, tuple[str, ...]] = {
    "Renault": ("4", "4 E-Tech electric", "5", "5 E-Tech electric", "Arkana", "Austral", "Captur", "Clio",
                "Kadjar", "Kangoo", "Kangoo Maxi", "Koleos", "Megane", "Megane E-Tech",
                "MEGANE E-TECH 100% ELECTRIC", "Megane R.s.", "New Austral", "Rafale", "Renault 4 E-Tech electric",
                "Renault 5", "Scenic", "Scenic E-Tech", "SCENIC E-TECH 100% ELECTRIC", "Symbioz", "Twizy", "Zoe"),
    "Dacia": ("Bigster", "Duster", "Jogger", "Logan MCV", "Logan Stepway", "Sandero", "Sandero Stepway", "Spring"),
    "Alpine": ("A110", "A290"),
}
# Van-only models, and words that mark a van in the body style or description.
VAN_MODELS = ("trafic", "master")
VAN_WORDS = re.compile(r"\b(van|chassis|tipper|dropside|luton|pick-?up)\b", re.I)

# Body type by model.  The site's own body style is unreliable (Dusters are
# listed as SUV, 4x4, Estate and Hatchback), so, as for Skoda, the model decides
# and the site's label is used only for a model not listed here.  Longest name
# first wins ("Scenic E-Tech" before "Scenic"); "Sport Tourer" versions are estates
# (other words are no guide: some Arkana adverts end "5dr Auto Estate").
MODEL_BODIES: dict[str, str] = {
    "duster": "SUV", "bigster": "SUV", "arkana": "SUV", "austral": "SUV", "newaustral": "SUV", "captur": "SUV",
    "kadjar": "SUV", "koleos": "SUV", "rafale": "SUV", "symbioz": "SUV", "scenicetech": "SUV", "4": "SUV",
    "clio": "Hatchback", "5": "Hatchback", "megane": "Hatchback", "zoe": "Hatchback", "sandero": "Hatchback",
    "spring": "Hatchback", "twizy": "Hatchback", "a290": "Hatchback",
    "jogger": "MPV", "scenic": "MPV", "grandscenic": "MPV", "kangoo": "MPV",
    "logan": "Estate", "loganmcv": "Estate",
}
ESTATE_WORDS = re.compile(r"\bsports? ?tourer\b", re.I)
OTHER_BODIES = ("coupe", "convertible", "cabrio", "roadster")
# Town names in dealer links that take two words ("milton-keynes", "bristol-east").
TOWN_LEADS = {"milton", "hemel", "high", "kings", "west", "east", "north", "south", "great", "little", "st", "new",
              "upper", "lower", "royal"}
TOWN_TAILS = {"east", "west", "north", "south", "central", "city"}
NOT_TOWN = {"renault", "dacia", "alpine", "motors", "motor", "group", "renew", "cars"}

HEADERS = {
    "Accept": "application/json, text/javascript, */*; q=0.01",
    "Accept-Language": "en-GB,en;q=0.9",
    "Referer": SEARCH_PAGE,
    "X-Requested-With": "XMLHttpRequest",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
}


def _plain(text: Any) -> str:
    """Lower-case letters and digits only, accents removed ("Mégane" -> "megane")."""
    text = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        return int(round(value))
    digits = re.sub(r"[^0-9]", "", str(value))
    return int(digits) if digits else None


def site_make(make: Any) -> str | None:
    """The site's spelling of a make CarFinder searches here, or None."""
    wanted = _plain(make)
    for name in MAKES:
        if wanted in (_plain(name), _plain(name) + "group"):
            return name
    return None


def _name_key(name: Any, make: str) -> str:
    """A site model name without the make in front ("Renault 5" -> "5")."""
    key = _plain(name)
    prefix = _plain(make)
    return key[len(prefix):] if key.startswith(prefix) and len(key) > len(prefix) else key


def model_matches(wanted: str, name: str | None, make: str) -> bool:
    """True when a site model name starts with what the user typed ("Megane" matches "Megane E-Tech")."""
    typed = _name_key(wanted, make)
    return not typed or _name_key(name, make).startswith(typed)


def models_for(model: str, make: str, known: dict[str, tuple[str, ...]]) -> list[str]:
    if not _plain(model):
        return []
    return [name for name in known.get(make, ()) if model_matches(model, name, make)]


MODEL_INPUT = re.compile(r"<input\b[^>]*>", re.I)


def parse_site_models(page: str) -> dict[str, tuple[str, ...]]:
    """Model names per make from the search page's model check boxes."""
    models: dict[str, list[str]] = {}
    for tag in MODEL_INPUT.findall(page or ""):
        name = re.search(r'\bname="model\[([^\]"]+)\]\[\]"', tag)
        value = re.search(r'\bvalue="([^"]*)"', tag)
        if name and value and html.unescape(name.group(1)) in MAKES and value.group(1).strip():
            make = html.unescape(name.group(1))
            text = html.unescape(value.group(1)).strip()
            if text not in models.setdefault(make, []):
                models[make].append(text)
    return {make: tuple(names) for make, names in models.items()}


# ---------------------------------------------------------------------------
# Car search -> request
# ---------------------------------------------------------------------------

def years_back(year_min: Any, today: dt.date | None = None) -> int | None:
    """How many years back the site must look to include cars first registered in ``year_min``."""
    if year_min in (None, ""):
        return None
    today = today or dt.date.today()
    return max(1, today.year - int(year_min) + 1)


def build_params(car: dict[str, Any], make: str, models: list[str], page: int = 1,
                 today: dt.date | None = None) -> list[tuple[str, str]]:
    params: list[tuple[str, str]] = [("section[]", SECTION), ("pageId", PAGE_ID), ("order", "price"),
                                     ("make[]", make)]
    params += [(f"model[{make}][]", name) for name in models]
    low, high = car.get("price_min"), car.get("price_max")
    if low is not None or high is not None:
        params.append(("budget-program[]", "pay"))
        if low is not None:
            params.append(("budget-price-min[]", str(int(low))))
        if high is not None:
            params.append(("budget-price-max[]", str(int(high))))
    if car.get("mileage_max") is not None:
        params.append(("mileage[range][]", f"-{int(car['mileage_max'])}"))
    years = years_back(car.get("year_min"), today)
    if years:
        params.append(("registered-at[min][]", f"{years} year"))
    transmission = str(car.get("transmission") or "Any")
    if transmission in ("Automatic", "Manual"):
        params.append(("transmission[]", transmission))
    if page > 1:
        params.append(("page", str(page)))
    return params


def items_url(params: list[tuple[str, str]]) -> str:
    return ITEMS_URL + "?" + urlencode(params)


def search_page_url(params: list[tuple[str, str]]) -> str:
    """The same search on the site, for the "open search" link."""
    return SEARCH_PAGE + "?" + urlencode([(k, v) for k, v in params if k not in ("pageId", "page")])


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


def _get(url: str, timeout: int = 60) -> Any:
    """GET with a wait and a fresh connection after a 403/429."""
    global _session
    attempts = len(RETRY_WAITS) + 1
    response = None
    for attempt in range(attempts):
        response = _http().get(url, headers=_headers(), timeout=timeout)
        if response.status_code not in (403, 429):
            response.raise_for_status()
            return response
        if attempt < len(RETRY_WAITS):
            _session = None
            time.sleep(RETRY_WAITS[attempt])
    status = response.status_code if response is not None else "?"
    server = (response.headers.get("Server") if response is not None else None) or "the site"
    raise RuntimeError(f"Renew blocked the request (HTTP {status} from {server}, {attempts} tries; "
                       f"fetched with {_fetcher()})")


def _fetch_items(params: list[tuple[str, str]], timeout: int = 60) -> dict[str, Any]:
    response = _get(items_url(params), timeout)
    try:
        data = response.json()
    except ValueError as exc:
        raise RuntimeError(f"Renew returned something that is not car data; fetched with {_fetcher()}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("vehicles"), list):
        raise RuntimeError("Renew returned an unexpected answer (no vehicles list)")
    return data


def site_models() -> dict[str, tuple[str, ...]]:
    """The site's current model names (in case a new model has been added)."""
    return parse_site_models(_get(SEARCH_PAGE, timeout=30).text)


# ---------------------------------------------------------------------------
# Site record -> car
# ---------------------------------------------------------------------------

def model_body(model: Any, make: Any) -> str | None:
    key = _name_key(model, str(make or ""))
    for name in sorted(MODEL_BODIES, key=len, reverse=True):
        if key == name or (key.startswith(name) and not name.isdigit()) or (
                name.isdigit() and re.fullmatch(rf"{name}(etech.*)?", key)):
            return MODEL_BODIES[name]
    return None


def body_for(style: Any, description: Any, model: Any = None, make: Any = None) -> str | None:
    if ESTATE_WORDS.search(str(description or "")):
        return "Estate"
    by_model = model_body(model, make)
    if by_model:
        return by_model
    text = _plain(style)
    for word, body in (("suv", "SUV"), ("4x4", "SUV"), ("estate", "Estate"), ("saloon", "Saloon"),
                       ("hatch", "Hatchback"), ("citycar", "Hatchback"), ("mpv", "MPV")):
        if word in text:
            return body
    if any(word in text for word in OTHER_BODIES):
        return None
    return classify_body_type(style, description)


def other_body(style: Any) -> bool:
    """Coupe, convertible or roadster: never what a body-type search asks for."""
    return any(word in _plain(style) for word in OTHER_BODIES)


def is_van(c: dict[str, Any]) -> bool:
    if _plain(c.get("model")) in VAN_MODELS:
        return True
    return bool(VAN_WORDS.search(f"{c.get('body_text') or ''} {c.get('description') or ''}"))


def tidy_colour(*texts: Any) -> str | None:
    for text in texts:
        colour = re.sub(r"\s+", " ", str(text or "")).strip()
        colour = re.sub(r"^((special\s+)?(metallic|solid|pearlescent|pearl)|special)\s*-\s*", "", colour, flags=re.I)
        if colour:
            return colour[:1].upper() + colour[1:].lower() if colour.isupper() else colour[:1].upper() + colour[1:]
    return None


def trim_from(variant: Any, model: Any, make: Any = None) -> str | None:
    """The variant without the make and model in front ("RENAULT ARKANA 1.6 ..." -> "1.6 ...")."""
    text = re.sub(r"\s+", " ", str(variant or "")).strip()
    for name in (str(make or "").strip(), str(model or "").strip()):
        if name and text.lower().startswith(name.lower() + " "):
            text = text[len(name) + 1:]
        elif name and text.lower() == name.lower():
            text = ""
    return text or None


def tidy_fuel(text: Any) -> str | None:
    """One spelling per fuel, so the usual fuel matching works ("Petrol Parallel PHEV" -> "Petrol Plug-in Hybrid")."""
    raw = re.sub(r"\s+", " ", str(text or "")).strip()
    fuel = raw.lower()
    if not fuel:
        return None
    base = "Diesel" if "diesel" in fuel else "Petrol"
    if "phev" in fuel or "plug-in" in fuel or "plug in" in fuel:
        return f"{base} Plug-in Hybrid"
    if "hybrid" in fuel or "mhev" in fuel or ("electric" in fuel and ("petrol" in fuel or "diesel" in fuel)):
        return f"{base} Hybrid"
    if "electric" in fuel:
        return "Electric"
    if "lpg" in fuel or "bi fuel" in fuel or "bi-fuel" in fuel or "gas" in fuel:
        return "Petrol/LPG"
    if fuel in ("petrol", "diesel"):
        return fuel.capitalize()
    return raw


def town_from(dealer_link: Any) -> str | None:
    """The dealer's town from its link ("/dealer-locator/jaybee-motors-renault-banbury/" -> "Banbury")."""
    slug = str(dealer_link or "").rstrip("/").rsplit("/", 1)[-1]
    words = [w for w in slug.lower().split("-") if w and w not in NOT_TOWN]
    if not words:
        return None
    town = words[-1:]
    if len(words) > 1 and (words[-2] in TOWN_LEADS or words[-1] in TOWN_TAILS):
        town = words[-2:]
    return " ".join(w.capitalize() for w in town)


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse_vehicle(vehicle: dict[str, Any]) -> dict[str, Any]:
    """Everything CarFinder needs from one result (no filtering)."""
    first_reg = str(vehicle.get("reg_year_full") or "")[:10] or None
    year = (_int(first_reg[:4]) if first_reg else None) or _int(vehicle.get("year")) \
        or _int(str(vehicle.get("model_year") or "")[:4])
    lat, lon = _float(vehicle.get("latitude")), _float(vehicle.get("longitude"))
    lat_lon = (lat, lon) if lat is not None and lon is not None and (lat, lon) != (0.0, 0.0) else None
    price = _int(vehicle.get("price_now_raw")) or _int(vehicle.get("price_now"))
    was = _int(vehicle.get("price_was"))
    model = str(vehicle.get("model") or "").strip() or None
    images = _int(vehicle.get("images_count")) or 0
    if PHOTO_HOST not in str(vehicle.get("image") or ""):
        images = 0  # the placeholder picture
    url = str(vehicle.get("url") or "")
    return {
        "id": vehicle.get("id"),
        "make": str(vehicle.get("make") or "").strip() or None,
        "registration": normalise_plate(vehicle.get("registration")),
        "vin": vehicle.get("vin"),
        "model": model,
        "trim": trim_from(vehicle.get("variant"), model, vehicle.get("make")),
        "description": vehicle.get("link_title"),
        "year": year,
        "first_registered": first_reg,
        "mileage": _int(vehicle.get("mileage")),
        "price": price,
        "previous_price": was if was and price and was > price else None,
        "fuel": tidy_fuel(vehicle.get("fuel")),
        "transmission": vehicle.get("transmission"),
        "body_type": body_for(vehicle.get("bodystyle"), vehicle.get("link_title"), model, vehicle.get("make")),
        "body_text": vehicle.get("bodystyle"),
        "colour": tidy_colour(vehicle.get("exterior_colour"), vehicle.get("colour")),
        "photo_count": images,
        "dealer": html.unescape(str(vehicle.get("location_name") or "")).strip() or None,
        "location": town_from(vehicle.get("location_url")),
        "lat_lon": lat_lon,
        "url": (SITE_ROOT + url) if url.startswith("/") else (url or None),
    }


def record_wanted(c: dict[str, Any], car: dict[str, Any], make: str, models: list[str] | None = None) -> bool:
    if _plain(c.get("make")) != _plain(make) or is_van(c):
        return False
    if models and c.get("model") not in models:
        return False
    wanted_trim = str(car.get("trim") or "").strip().lower()
    if wanted_trim and wanted_trim not in f"{c.get('trim') or ''} {c.get('description') or ''}".lower():
        return False
    if str(car.get("body_type") or "Any") != "Any" and c.get("body_type") is None and other_body(c.get("body_text")):
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
    """Dealer lat/lon from the site, else its town."""
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
    raw = {k: c.get(k) for k in ("id", "vin", "description", "body_text", "first_registered", "mileage", "price",
                                 "previous_price", "dealer", "location", "lat_lon", "url")}
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
        "previous_price": c.get("previous_price"),
        "dealer": c.get("dealer"),
        "location": c.get("location"),
        "distance_miles": distance_miles(home, where) if where else None,
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
    make = site_make(car.get("make"))
    if not make:
        raise RuntimeError(f"Renew source does not search make '{car.get('make')}'")
    models: list[str] = []
    if car.get("model"):
        models = models_for(str(car["model"]), make, KNOWN_MODELS)
        try:
            live = models_for(str(car["model"]), make, site_models())
        except Exception:  # noqa: BLE001 - fall back to the known list
            live = []
        models += [m for m in live if m not in models]
        if not models:
            raise RuntimeError(f"Renew has no {make} model matching '{car['model']}' right now")

    home = postcode_location(settings.get("home_postcode"))
    result = SearchResult(search_url=search_page_url(build_params(car, make, models)))
    seen: set[str] = set()
    seen_ids: set[str] = set()
    pages_log: list[dict[str, Any]] = []
    page = 1
    while page <= MAX_PAGES:
        if page > 1:
            time.sleep(PAGE_DELAY + random.uniform(0, PAGE_JITTER))
        started = time.perf_counter()
        try:
            data = _fetch_items(build_params(car, make, models, page))
        except RuntimeError as exc:
            raise RuntimeError(f"{exc} on results page {page}") from exc
        fetch_seconds = time.perf_counter() - started
        if timings is not None:
            timings["search_fetch_seconds"] = round(float(timings.get("search_fetch_seconds") or 0)
                                                    + fetch_seconds, 3)
        records = [parse_vehicle(v) for v in data.get("vehicles") or [] if isinstance(v, dict)]
        new = [c for c in records if str(c.get("id")) not in seen_ids]
        rows = 0
        for c in new:
            seen_ids.add(str(c.get("id")))
            if c.get("registration") and _plain(c.get("make")) == _plain(make):
                result.raw_regs.add(c["registration"])
            if not record_wanted(c, car, make, models):
                continue
            row = record_to_row(c, home)
            if not row or row["registration"] in seen:
                continue
            if body_and_seats_match(car, row.get("body_type"), row.get("seats")):
                seen.add(row["registration"])
                result.rows.append(row)
                rows += 1
        pages_log.append({"search": car.get("name"), "page": page, "fetch_seconds": round(fetch_seconds, 3),
                          "vehicle_objects": len(records), "rows": rows, "total": _int(data.get("count"))})
        if not new or not data.get("hasMoreResults") or len(records) < PAGE_SIZE:
            break
        page += 1
    if timings is not None:
        timings.setdefault("pages", []).extend(pages_log)
    result.debug_text = "\n".join(json.dumps(p) for p in pages_log)
    return result
