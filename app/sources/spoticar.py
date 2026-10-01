"""Spoticar (Stellantis approved used) source module.

Searches https://www.spoticar.co.uk, the approved-used site for Peugeot,
Citroën, Vauxhall, Fiat and DS (and other Stellantis makes):

* results are ordinary server-rendered HTML pages, 12 cars per page, at
  ``/second-hand-cars?page=N&filters[0][brand]=peugeot&filters[1][model]=5008...``;
  repeating a filter (two models, two body categories) means "either";
* the site filters on make, model, price, mileage, year, fuel, gearbox and
  body category; CarFinder applies the exact limits again itself;
* each car card carries a JSON block (``multiple_teaser_data``) with the
  registration, VIN, price, mileage and first-registration date, plus the
  dealer's name, town and latitude/longitude, so distance is the straight
  line from the person's postcode to the dealer;
* the colour is read from the photo description ("Used Car - Mpv Petrol Black");
* the site sits behind Akamai, which refuses plain Python/curl requests (HTTP 403)
  because of their TLS handshake, so pages are fetched with ``curl_cffi``
  impersonating Chrome. If ``curl_cffi`` is not installed, plain ``requests`` is
  used and a block is reported as an error rather than as "no cars found";
* Akamai also refuses requests that arrive too quickly, so result pages are
  fetched a few seconds apart and a 403 is retried with a fresh session after
  a longer wait.
"""
from __future__ import annotations

import base64
import json
import math
import random
import re
import time
import unicodedata
from typing import Any
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

try:  # Akamai blocks plain requests; curl_cffi sends a real Chrome TLS handshake.
    from curl_cffi import requests as cffi_requests
except ImportError:  # pragma: no cover - depends on what is installed
    cffi_requests = None

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

SOURCE_KEY = "spoticar"
SOURCE_NAME = "Spoticar (Stellantis Approved Used)"
MAKES = ("Peugeot", "Citroën", "Vauxhall", "Fiat", "DS")

SITE_ROOT = "https://www.spoticar.co.uk"
LIST_PATH = "/second-hand-cars"
PAGE_SIZE = 12
MAX_PAGES = 40

# CarFinder make -> the site's brand filter value and the brand code in the card JSON.
BRANDS: dict[str, tuple[str, tuple[str, ...]]] = {
    "Peugeot": ("peugeot", ("PEU",)),
    "Citroën": ("citroën", ("CIT",)),
    "Vauxhall": ("vauxhall", ("VAU", "OPV")),
    "Fiat": ("fiat", ("FIA", "FIAT")),
    "DS": ("ds", ("DS",)),
}

# Model names offered by the site's model filter (Oct 2026), lower case as the site uses them.
KNOWN_MODELS: dict[str, list[str]] = {
    "Peugeot": ["108", "2008", "208", "3008", "308", "308 sw", "408", "5008", "508", "508 sw", "boxer", "e-3008",
                "e-408", "expert", "partner", "partner tepee", "rifter", "traveller"],
    "Citroën": ["ami", "berlingo", "c1", "c3", "c3 aircross", "c3 picasso", "c4", "c4 cactus", "c4 grand picasso",
                "c4 grand spacetourer", "c4 picasso", "c4 spacetourer", "c4 x", "c5 aircross", "c5 x", "dispatch",
                "holidays", "new e-c5 aircross", "relay", "spacetourer"],
    "Vauxhall": ["adam", "astra", "combo", "combo life", "corsa", "crossland", "frontera", "grandland", "insignia",
                 "meriva", "mokka", "movano", "movano electric", "viva", "vivaro", "vivaro life", "zafira tourer"],
    "Fiat": ["124", "500", "500c", "500l", "500x", "600", "600e", "doblo", "ducato", "grande panda", "panda", "qubo",
             "scudo", "tipo"],
    "DS": ["ds 3", "ds 3 crossback", "ds 4", "ds 7 crossback", "no4", "no8"],
}

FUEL_VALUES = {"Petrol": ["petrol"], "Diesel": ["diesel"], "Hybrid": ["hybrid", "plug-in hybrid"],
               "Electric": ["electric"]}
GEAR_VALUES = {"Manual": "manual", "Automatic": "automatic"}
BODY_VALUES = {"Hatchback": ["hatchback", "city car"], "Estate": ["estate"], "SUV": ["suv"], "MPV": ["mpv"],
               "Saloon": ["saloon"]}
SITE_BODY = {"hatchback": "Hatchback", "city car": "Hatchback", "estate": "Estate", "suv": "SUV", "mpv": "MPV",
             "saloon": "Saloon"}
BODY_WORDS = ("commercial vehicle", "city car", "hatchback", "estate", "saloon", "mpv", "suv", "coupe", "convertible",
              "cabriolet", "van", "pick-up", "pickup", "combi")
IMPERSONATE = "chrome"
# A real results page is several hundred KB; a bot-check page is a few KB.
SMALL_PAGE_BYTES = 100_000
# Pause between result pages (seconds, plus up to PAGE_JITTER more) and the waits before retrying a 403.
PAGE_DELAY = 3.0
PAGE_JITTER = 2.0
RETRY_WAITS = (10.0, 30.0)

SEVEN_SEATERS = ("5008", "grand picasso", "grand spacetourer", "zafira", "berlingo xl", "rifter long", "combo life xl")

HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
}


def _squash(text: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(text or "").lower())


def _int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        digits = re.sub(r"[^0-9]", "", str(value))
        return int(digits) if digits else None


def _text(node: Any) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip() if node is not None else ""


def _plain(text: Any) -> str:
    """Lower-case letters/digits with accents removed ("Citroën" -> "citroen")."""
    return _squash(unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode())


def make_key(make: str | None) -> str | None:
    """CarFinder make as written in MAKES ("citroen" -> "Citroën")."""
    wanted = _plain(make)
    for name in BRANDS:
        if _plain(name) == wanted:
            return name
    return None


# ---------------------------------------------------------------------------
# Car search -> site filters
# ---------------------------------------------------------------------------

def model_matches(wanted: str, name: str | None, make: str = "") -> bool:
    """True when a model name starts with what the user typed ("308" matches "308 SW").

    For DS the "DS" is part of the model name, so "7" and "DS 7" both match "DS 7 Crossback".
    """
    typed, have = _plain(wanted), _plain(name)
    if not typed:
        return True
    if have.startswith(typed):
        return True
    return make == "DS" and have.startswith("ds" + typed)


def models_for(model: str, known: list[str], make: str = "") -> list[str]:
    """Site model names starting with what the user typed ("308" -> 308, 308 sw)."""
    if not _plain(model):
        return []
    return [name for name in known if model_matches(model, name, make)]


def build_filters(car: dict[str, Any], brand_value: str, model_names: list[str]) -> list[tuple[str, str]]:
    filters: list[tuple[str, str]] = [("brand", brand_value)]
    filters.extend(("model", name) for name in model_names)
    if car.get("price_min") is not None:
        filters.append(("min_price", str(int(car["price_min"]))))
    if car.get("price_max") is not None:
        filters.append(("max_price", str(int(car["price_max"]))))
    if car.get("mileage_max") is not None:
        filters.append(("max_km", str(int(car["mileage_max"]))))
    if car.get("year_min") is not None:
        filters.append(("min_year", str(int(car["year_min"]))))
    filters.extend(("energy", value) for value in FUEL_VALUES.get(str(car.get("fuel") or "Any"), []))
    gear = GEAR_VALUES.get(str(car.get("transmission") or "Any"))
    if gear:
        filters.append(("gearbox", gear))
    filters.extend(("category", value) for value in BODY_VALUES.get(str(car.get("body_type") or "Any"), []))
    return filters


def build_url(filters: list[tuple[str, str]], page: int) -> str:
    parts = [f"page={page}"]
    parts.extend(f"filters%5B{i}%5D%5B{key}%5D={quote(value)}" for i, (key, value) in enumerate(filters))
    return f"{SITE_ROOT}{LIST_PATH}?" + "&".join(parts)


_session: Any = None


def _fetcher() -> str:
    return "curl_cffi (Chrome)" if cffi_requests is not None else "plain requests (curl_cffi not installed)"


def _make_session() -> Any:
    if cffi_requests is not None:
        # Let curl_cffi send Chrome's own User-Agent so it matches the TLS handshake.
        return cffi_requests.Session(impersonate=IMPERSONATE)
    session = requests.Session()
    session.headers.update(HEADERS)
    return session


def _http() -> Any:
    """One shared session (keeps the site's cookies between pages)."""
    global _session
    if _session is None:
        _session = _make_session()
    return _session


def _get(url: str, timeout: int = 30) -> str:
    """Fetch a page; on a 403 wait, start a fresh session and try again (RETRY_WAITS)."""
    global _session
    headers = {"Accept-Language": HEADERS["Accept-Language"]} if cffi_requests is not None else None
    attempts = len(RETRY_WAITS) + 1
    for attempt in range(attempts):
        response = _http().get(url, headers=headers, timeout=timeout)
        if response.status_code != 403:
            response.raise_for_status()
            return response.text
        if attempt < len(RETRY_WAITS):
            _session = None
            time.sleep(RETRY_WAITS[attempt])
    server = response.headers.get("Server") or "the site"
    raise RuntimeError(f"Spoticar blocked the request (HTTP 403 from {server}, {attempts} tries; "
                       f"fetched with {_fetcher()})")


def looks_blocked(html: str, cards: list[dict[str, Any]], total: int | None) -> bool:
    """A small page with no car list and no result count is a bot check, not "no cars"."""
    return not cards and total is None and len(html) < SMALL_PAGE_BYTES


# ---------------------------------------------------------------------------
# Results page -> cards
# ---------------------------------------------------------------------------

def total_count(soup: BeautifulSoup) -> int | None:
    node = soup.select_one(".teaser-count")
    match = re.search(r"([\d,]+)\s*Used vehicles", _text(node) if node else soup.get_text(" "), flags=re.I)
    return int(match.group(1).replace(",", "")) if match else None


def card_vehicle(card: Any) -> dict[str, Any]:
    """The card's ``multiple_teaser_data`` vehicle record (its top key varies: AP, AC, OV...)."""
    node = card.select_one("input.multiple_teaser_data")
    try:
        data = json.loads(node.get("value") or "{}") if node else {}
    except json.JSONDecodeError:
        return {}
    for value in data.values():
        if isinstance(value, dict) and isinstance(value.get("vehicles"), dict):
            return value["vehicles"]
    return {}


def long_label(vehicle: dict[str, Any]) -> str:
    presentation = vehicle.get("presentation") or {}
    if presentation.get("longLabel"):
        return str(presentation["longLabel"])
    encoded = presentation.get("longLabelBase64")
    if encoded:
        try:
            return base64.b64decode(encoded).decode("utf-8", "ignore")
        except (ValueError, TypeError):
            return ""
    return ""


def colour_from_alt(alt: str, fuel: str | None) -> str | None:
    """"... Used Car - Mpv Petrol Black - Crawley - 1200639708_1" -> "Black"."""
    match = re.search(r"Used Car - (.+?) - ", alt or "")
    if not match:
        return None
    words = match.group(1).strip()
    low = words.lower()
    for body in BODY_WORDS:
        if low.startswith(body + " "):
            words, low = words[len(body):].strip(), low[len(body):].strip()
            break
    fuel_low = (fuel or "").strip().lower()
    if fuel_low and low.startswith(fuel_low + " "):
        words = words[len(fuel_low):].strip()
    elif fuel_low and low == fuel_low:
        words = ""
    return words.title() or None


def body_for(label: str, model: str | None) -> str | None:
    """Body type from the compare label ("Peugeot 5008 MPV", "Peugeot 108 City car")."""
    low = (label or "").lower()
    for word, body in SITE_BODY.items():
        if low.endswith(" " + word):
            return body
    name = (model or "").lower()
    if name.endswith(" sw") or "tourer" in name:
        return "Estate"
    return None


def parse_card(card: Any, make: str) -> dict[str, Any]:
    """Everything CarFinder needs from one car card (no filtering)."""
    vehicle = card_vehicle(card)
    state = vehicle.get("state") or {}
    title_node = card.select_one(".vehicle-card-title h3")
    version_node = card.select_one(".vehicle-card-title .car-version")
    version = _text(version_node)
    heading = _text(title_node)
    if version and heading.endswith(version):
        heading = heading[: -len(version)].strip()
    model = heading
    first_word = model.split(" ", 1)
    # DS headings are already "DS 4", "DS 7 Crossback", "No8" (no make prefix).
    if make != "DS" and len(first_word) == 2 and _plain(first_word[0]) == _plain(make):
        model = first_word[1].strip()
    tags = [_text(t) for t in card.select(".characteristics-tags .tag")]
    mileage_tag = next((t for t in tags if "mile" in t.lower()), None)
    year_tag = next((t for t in tags if re.fullmatch(r"\d{4}-\d{2}", t)), None)
    gear_tag = next((t for t in tags if t.lower() in ("manual", "automatic")), None)
    fuel_tag = next((t for t in tags if t not in (mileage_tag, year_tag, gear_tag)), None)
    compare = card.select_one("[data-vehicle-model-body-style-label-compare]")
    images = card.select("img.car-image")
    dealer_node = card.select_one(".dealer-address")
    dealer_name = None
    town = None
    lat = lon = None
    if dealer_node is not None:
        name_node = dealer_node.select_one(".address-name")
        if name_node is not None:
            tooltip = name_node.select_one(".pdv-tooltip")
            dealer_name = _text(tooltip) if tooltip is not None else _text(name_node)
        spans = [s for s in dealer_node.find_all("span", recursive=False)
                 if "address-name" not in (s.get("class") or []) and "distance" not in (s.get("class") or [])]
        town = _text(spans[0]) if spans else None
        dist = dealer_node.select_one(".distance")
        if dist is not None:
            try:
                lat, lon = float(dist.get("data-lat")), float(dist.get("data-long"))
            except (TypeError, ValueError):
                lat = lon = None
    link = card.select_one("a.vehicle-card-link") or card.select_one("a[href*='/second-hand-cars/']")
    price = _int((vehicle.get("pricing") or {}).get("netPriceInclTax"))
    if price is None:
        price = _int(_text(card.select_one(".cash .price-value")))
    first_reg = str(state.get("firstRegistrationDate") or "")[:10] or None
    year = _int(first_reg[:4]) if first_reg else (_int(year_tag[:4]) if year_tag else None)
    return {
        "id": card.get("data-vo-id"),
        "brand_code": vehicle.get("brand"),
        "registration": normalise_plate(vehicle.get("registrationNumber")),
        "vin": vehicle.get("vin"),
        "model": model or None,
        "trim": version or None,
        "label": long_label(vehicle),
        "year": year,
        "first_registered": first_reg,
        "mileage": _int(state.get("mileage")) if state.get("mileage") not in (None, "") else _int(mileage_tag),
        "price": price,
        "fuel": fuel_tag,
        "transmission": gear_tag,
        "body_type": body_for(compare.get("data-vehicle-model-body-style-label-compare") if compare else "", model),
        "colour": colour_from_alt(images[0].get("alt") if images else "", fuel_tag),
        "photo_count": len(images),
        "dealer": dealer_name,
        "location": town,
        "lat_lon": (lat, lon) if lat is not None and lon is not None else None,
        "url": SITE_ROOT + link["href"] if link is not None and str(link.get("href", "")).startswith("/") else (
            link.get("href") if link is not None else None),
        "used": state.get("stateType") in (None, "", "VO"),
        # "VP" = passenger car; "VU" = commercial vehicle (vans), which CarFinder skips.
        "kind": (vehicle.get("features") or {}).get("kind"),
    }


def parse_page(html: str, make: str) -> tuple[list[dict[str, Any]], int | None]:
    soup = BeautifulSoup(html, "lxml")
    return [parse_card(card, make) for card in soup.select(".vehicle-card")], total_count(soup)


# ---------------------------------------------------------------------------
# Card -> standard listing
# ---------------------------------------------------------------------------

def card_wanted(c: dict[str, Any], car: dict[str, Any], make: str) -> bool:
    codes = BRANDS[make][1]
    if c.get("brand_code") and str(c["brand_code"]).upper() not in codes:
        return False
    if not c.get("used") or c.get("kind") not in (None, "", "VP"):
        return False
    if car.get("model") and not model_matches(str(car["model"]), c.get("model"), make):
        return False
    wanted_trim = str(car.get("trim") or "").strip().lower()
    if wanted_trim and wanted_trim not in f"{c.get('trim') or ''} {c.get('label') or ''}".lower():
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


def card_to_row(c: dict[str, Any], make: str, home: tuple[float, float] | None) -> dict[str, Any] | None:
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
    model_low = f"{c.get('model') or ''} {c.get('trim') or ''}".lower()
    seats = 7 if any(name in model_low for name in SEVEN_SEATERS) else None
    title = " ".join(str(p) for p in (c.get("year"), make, c.get("model"), c.get("trim"), reg) if p)
    raw = {k: c.get(k) for k in ("id", "brand_code", "vin", "label", "first_registered", "mileage", "price", "dealer",
                                 "location", "lat_lon", "url")}
    return standardise({
        "registration": reg,
        "make": make,
        "model": c.get("model"),
        "trim": c.get("trim"),
        "year": c.get("year"),
        "first_registered": c.get("first_registered"),
        "colour": c.get("colour"),
        "fuel": c.get("fuel"),
        "transmission": c.get("transmission"),
        "body_type": c.get("body_type"),
        "seats": seats,
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
    make = make_key(car.get("make"))
    if not make:
        raise RuntimeError(f"Spoticar does not search make '{car.get('make')}'")
    brand_value = BRANDS[make][0]
    model_names: list[str] = []
    if car.get("model"):
        model_names = models_for(str(car["model"]), KNOWN_MODELS[make], make)
        if not model_names:
            # The site's own model list, in case a new model has been added.
            first_soup = BeautifulSoup(_get(build_url([("brand", brand_value)], 1)), "lxml")
            site_models = [str(i.get("value")) for i in first_soup.select('input[name="model"]') if i.get("value")]
            model_names = models_for(str(car["model"]), site_models, make)
        if not model_names:
            raise RuntimeError(f"Spoticar has no {make} model matching '{car['model']}' right now")

    filters = build_filters(car, brand_value, model_names)
    home = postcode_location(settings.get("home_postcode"))
    result = SearchResult(search_url=build_url(filters, 1))
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
            html = _get(build_url(filters, page))
        except RuntimeError as exc:
            raise RuntimeError(f"{exc} on results page {page}") from exc
        fetch_seconds = time.perf_counter() - started
        if timings is not None:
            timings["search_fetch_seconds"] = round(float(timings.get("search_fetch_seconds") or 0) + fetch_seconds, 3)
        cards, total = parse_page(html, make)
        if page == 1 and looks_blocked(html, cards, total):
            raise RuntimeError(f"Spoticar returned a page with no car list ({len(html)} bytes), probably a bot "
                               f"check; fetched with {_fetcher()}")
        if total is not None:
            pages = max(1, math.ceil(total / PAGE_SIZE))
        new_cards = [c for c in cards if c.get("id") not in seen_ids]
        rows = 0
        for c in new_cards:
            seen_ids.add(str(c.get("id")))
            if c.get("registration"):
                result.raw_regs.add(c["registration"])
            if not card_wanted(c, car, make):
                continue
            row = card_to_row(c, make, home)
            if not row or row["registration"] in seen:
                continue
            if body_and_seats_match(car, row.get("body_type"), row.get("seats")):
                seen.add(row["registration"])
                result.rows.append(row)
                rows += 1
        pages_log.append({"search": car.get("name"), "page": page, "fetch_seconds": round(fetch_seconds, 3),
                          "vehicle_objects": len(cards), "rows": rows, "total": total})
        # A page past the end repeats the last page, so stop when nothing new arrives.
        if not new_cards or len(cards) < PAGE_SIZE:
            break
        page += 1
    if timings is not None:
        timings.setdefault("pages", []).extend(pages_log)
    result.debug_text = "\n".join(json.dumps(p) for p in pages_log)
    return result
