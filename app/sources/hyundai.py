"""Hyundai (Hyundai Promise / Approved Used) source module.

Searches https://used.hyundai.co.uk, Hyundai UK's approved-used site (a Modix
site):

* results are ordinary server-rendered HTML pages, always 10 cars per page, at
  ``/en/used-car-search/pageN?sort=price:ASC&manufacturer=22&model=...``;
* the site filters on model, fuel (``gas``), gearbox (``gears``), body style
  (``build``), price (``price_from``/``price_to``), mileage in miles
  (``km_from``/``km_to`` despite the name) and first-registration year
  (``reg_date_from``); a model is a list of ids joined with ``||``;
* each car card shows the registration, price, mileage, first-registration date,
  fuel, gearbox and the dealer's name, postcode and town; the page's schema.org
  JSON-LD adds the VIN and colour (matched to the card by its link);
* distance is the straight line from the home postcode to the dealer's postcode;
* the site sits behind a CDN with Akamai-style headers, so it is fetched like
  Spoticar: curl_cffi impersonating Chrome when installed, a few seconds between
  pages, and a 403 retried with a fresh session after a longer wait.

Unknown filter values may be ignored by the site, so every limit is applied
again here.
"""
from __future__ import annotations

import json
import math
import random
import re
import time
from typing import Any
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

try:  # A real Chrome TLS handshake, as for Spoticar.
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

SOURCE_KEY = "hyundai"
SOURCE_NAME = "Hyundai Approved Used"
MAKES = ("Hyundai",)

SITE_ROOT = "https://used.hyundai.co.uk"
LIST_PATH = "/en/used-car-search"
MANUFACTURER = "22"
PAGE_SIZE = 10
MAX_PAGES = 80
IMPERSONATE = "chrome"
SMALL_PAGE_BYTES = 50_000
PAGE_DELAY = 3.0
PAGE_JITTER = 2.0
RETRY_WAITS = (10.0, 30.0)

# The site's model filter (Oct 2026): name -> its id list.
KNOWN_MODELS: dict[str, str] = {
    "i10": "1421", "i20": "2177||1446||2187||3483", "i20 N": "3483", "ix20": "1551", "BAYON": "3563",
    "i30": "2176||1405||2171||2172||2943||2948", "i30 N": "2943", "Veloster": "1580", "i800": "1536",
    "INSTER": "4190", "IONIQ": "2758||2291||2759||2760||3265", "IONIQ 5": "4299||3562||4184", "IONIQ 5 N": "4184",
    "IONIQ 6": "4300||3908||4206", "IONIQ 6 N": "4206", "IONIQ 9": "4205", "KONA": "3168||2765||3169||3264||3601",
    "KONA N": "3601", "TUCSON": "3781||488||3748", "SANTA FE": "3782||483||3747",
}

FUEL_VALUES = {"Petrol": "1||7", "Diesel": "2", "Hybrid": "9", "Electric": "3"}
GEAR_VALUES = {"Manual": "3||4||5", "Automatic": "1||7"}
BODY_VALUES = {"Estate": "4", "Hatchback": "5", "MPV": "9", "SUV": "3", "Saloon": "6"}
SEVEN_SEATERS = ("santa fe", "ioniq 9", "i800")

HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
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


def _text(node: Any) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ", strip=True)).strip() if node is not None else ""


def model_name(name: Any) -> str:
    """"Hyundai TUCSON" -> "TUCSON"."""
    text = re.sub(r"\s+", " ", str(name or "")).strip()
    return text[8:].strip() if text.lower().startswith("hyundai ") else text


def model_matches(wanted: str, name: str | None) -> bool:
    """True when a model name starts with what the user typed ("IONIQ 5" matches "IONIQ 5 N")."""
    typed = _plain(model_name(wanted))
    return not typed or _plain(model_name(name)).startswith(typed)


def model_value_for(model: str, known: dict[str, str]) -> str:
    """The site's model filter value for what the user typed (ids of every match, joined with ||)."""
    if not _plain(model):
        return ""
    ids: list[str] = []
    for name, value in known.items():
        if model_matches(model, name):
            ids.extend(i for i in value.split("||") if i not in ids)
    return "||".join(ids)


# ---------------------------------------------------------------------------
# Car search -> URL
# ---------------------------------------------------------------------------

def build_params(car: dict[str, Any], model_value: str) -> list[tuple[str, str]]:
    params: list[tuple[str, str]] = [("sort", "price:ASC"), ("manufacturer", MANUFACTURER)]
    if model_value:
        params.append(("model", model_value))
    if car.get("price_min") is not None:
        params.append(("price_from", str(int(car["price_min"]))))
    if car.get("price_max") is not None:
        params.append(("price_to", str(int(car["price_max"]))))
    if car.get("mileage_max") is not None:
        params.append(("km_to", str(int(car["mileage_max"]))))
    if car.get("year_min") is not None:
        params.append(("reg_date_from", str(int(car["year_min"]))))
    for key, values in (("gas", FUEL_VALUES), ("gears", GEAR_VALUES), ("build", BODY_VALUES)):
        field = {"gas": "fuel", "gears": "transmission", "build": "body_type"}[key]
        value = values.get(str(car.get(field) or "Any"))
        if value:
            params.append((key, value))
    return params


def build_url(params: list[tuple[str, str]], page: int) -> str:
    query = "&".join(f"{k}={quote(v, safe=':|')}" for k, v in params)
    return f"{SITE_ROOT}{LIST_PATH}/page{page}?{query}"


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
    raise RuntimeError(f"Hyundai blocked the request (HTTP 403 from {server}, {attempts} tries; "
                       f"fetched with {_fetcher()})")


def looks_blocked(html: str, cards: list[dict[str, Any]], total: int | None) -> bool:
    """A small page with no car list and no result count is a bot check, not "no cars"."""
    return not cards and total is None and len(html) < SMALL_PAGE_BYTES


# ---------------------------------------------------------------------------
# Results page -> cards
# ---------------------------------------------------------------------------

def total_count(soup: BeautifulSoup) -> int | None:
    node = soup.select_one("div.mdx-match-plural span") or soup.select_one("div.mdx-match-singular span")
    if node is not None:
        return _int(_text(node))
    match = re.search(r"([\d,]+)\s+vehicles?\s+match", soup.get_text(" "))
    return int(match.group(1).replace(",", "")) if match else None


def ld_offers(soup: BeautifulSoup) -> dict[str, dict[str, Any]]:
    """schema.org Car records from the page's JSON-LD, keyed by the advert path."""
    out: dict[str, dict[str, Any]] = {}
    for script in soup.select('script[type="application/ld+json"]'):
        text = script.string or script.get_text() or ""
        if "itemOffered" not in text:
            continue
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            continue
        items = (((data.get("mainEntity") or {}).get("offers") or {}).get("itemOffered")) or []
        for item in items if isinstance(items, list) else [items]:
            url = str((item or {}).get("url") or "")
            path = re.sub(r"^https?://[^/]+", "", url).split("?")[0]
            if path:
                out[path] = item
    return out


def card_fields(card: Any) -> dict[str, str]:
    """{"Mileage": "50,089 miles*", "Registration": "DL22OVO", ...} from the card's data list."""
    fields: dict[str, str] = {}
    for li in card.select("ul.vehicle-data__list li"):
        title_node = li.select_one(".vehicle-data__title")
        key = (li.get("title") or _text(title_node)).strip().rstrip(":").strip()
        spans = li.select("span")
        value = _text(spans[-1]) if spans else ""
        if key:
            fields[key] = value
    return fields


def uk_date(text: str) -> str | None:
    """"22/08/2022" -> "2022-08-22"."""
    match = re.search(r"(\d{1,2})/(\d{1,2})/(\d{4})", text or "")
    return f"{match.group(3)}-{int(match.group(2)):02d}-{int(match.group(1)):02d}" if match else None


def body_for(title: str | None) -> str | None:
    """Body type from the advert title ("... N Line SUV 5dr ...", "i30 Tourer", "5dr")."""
    text = f" {title or ''} ".lower()
    if any(w in text for w in ("tourer", "estate", "wagon")):
        return "Estate"
    if any(w in text for w in (" suv", "tucson", "santa fe", "kona", "bayon", "ix35", "inster", "ioniq 5",
                                  "ioniq 9")):
        return "SUV"
    if any(w in text for w in (" mpv", "ix20", "i800")):
        return "MPV"
    if any(w in text for w in ("saloon", "sedan", "ioniq 6", "i40 ")):
        return "Saloon"
    if "hatch" in text or "fastback" in text or re.search(r"\b[35] ?d(oo)?r\b", text):
        return "Hatchback"
    return classify_body_type(title)


def parse_card(card: Any, ld: dict[str, dict[str, Any]]) -> dict[str, Any]:
    """Everything CarFinder needs from one car card (no filtering)."""
    path = str(card.get("data-link") or "").split("?")[0]
    item = ld.get(path) or {}
    offer = ((item.get("offers") or {}).get("offers")) or {}
    seller = offer.get("offeredBy") or {}
    address = seller.get("address") or {}
    fields = card_fields(card)
    title = _text(card.select_one(".vehicle-data h3.color-black:not(.trim-text)")) or str(item.get("name") or "")
    model = model_name(_text(card.select_one("h3.trim-text")) or item.get("model"))
    trim = title
    for prefix in (f"Hyundai {model}", "Hyundai"):
        if trim.lower().startswith(prefix.lower()):
            trim = trim[len(prefix):].strip()
            break
    if model and trim.lower().startswith(model.lower() + " "):
        trim = trim[len(model):].strip()
    dealer_name = _text(card.select_one("address .dealer-name")) or seller.get("name")
    place = _text(card.select_one("address .s-hide"))
    postcode_match = re.match(r"^([A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2})\s+(.*)$", place, flags=re.I)
    postcode = postcode_match.group(1).upper() if postcode_match else address.get("postalCode")
    town = postcode_match.group(2).strip() if postcode_match else (address.get("addressLocality") or place or None)
    price = _int(offer.get("price")) or _int(_text(card.select_one(".vehicle-price")))
    first_reg = uk_date(fields.get("Registered", ""))
    return {
        "id": (card.get("id") or "").replace("vehicle", "", 1) or None,
        "brand": ((item.get("brand") or {}).get("name")) or "Hyundai",
        "registration": normalise_plate(fields.get("Registration")),
        "vin": item.get("vehicleIdentificationNumber") or None,
        "model": model or None,
        "trim": trim or None,
        "title": title,
        "year": _int(first_reg[:4]) if first_reg else None,
        "first_registered": first_reg,
        "mileage": _int(fields.get("Mileage")),
        "price": price,
        "fuel": fields.get("Fuel") or None,
        "transmission": fields.get("Transmission") or None,
        "body_type": body_for(f"{title} {model}"),
        "colour": (item.get("color") or "").strip() or None,
        "photo_count": len(card.select(".results-slide picture")),
        "dealer": dealer_name or None,
        "location": town or None,
        "postcode": postcode or None,
        "url": SITE_ROOT + path if path else None,
    }


def parse_page(html: str) -> tuple[list[dict[str, Any]], int | None]:
    soup = BeautifulSoup(html, "lxml")
    ld = ld_offers(soup)
    cards = [parse_card(card, ld) for card in soup.select('article.vehicle[id^="vehicle"]')]
    return cards, total_count(soup)


# ---------------------------------------------------------------------------
# Card -> standard listing
# ---------------------------------------------------------------------------

def card_wanted(c: dict[str, Any], car: dict[str, Any]) -> bool:
    if c.get("brand") and _plain(c["brand"]) != "hyundai":
        return False
    if car.get("model") and not model_matches(str(car["model"]), c.get("model")):
        return False
    wanted_trim = str(car.get("trim") or "").strip().lower()
    if wanted_trim and wanted_trim not in str(c.get("title") or "").lower():
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


def card_to_row(c: dict[str, Any], home: tuple[float, float] | None,
                dealer_lat_lon: tuple[float, float] | None = None) -> dict[str, Any] | None:
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
    title = " ".join(str(p) for p in (c.get("year"), "Hyundai", c.get("model"), c.get("trim"), reg) if p)
    raw = {k: c.get(k) for k in ("id", "vin", "title", "first_registered", "mileage", "price", "dealer", "location",
                                 "postcode", "url")}
    return standardise({
        "registration": reg,
        "make": "Hyundai",
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
        "distance_miles": distance_miles(home, dealer_lat_lon) if dealer_lat_lon else None,
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
    if _plain(car.get("make")) != "hyundai":
        raise RuntimeError(f"Hyundai source does not search make '{car.get('make')}'")
    model_value = ""
    if car.get("model"):
        model_value = model_value_for(str(car["model"]), KNOWN_MODELS)
        if not model_value:
            # The site's own model list, in case a new model has been added.
            soup = BeautifulSoup(_get(build_url([("manufacturer", MANUFACTURER)], 1)), "lxml")
            site_models = {_text(o): str(o.get("value")) for o in soup.select('select[name="model"] option')
                           if o.get("value")}
            model_value = model_value_for(str(car["model"]), site_models)
        if not model_value:
            raise RuntimeError(f"Hyundai has no model matching '{car['model']}' right now")

    params = build_params(car, model_value)
    home = postcode_location(settings.get("home_postcode"))
    result = SearchResult(search_url=build_url(params, 1))
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
            html = _get(build_url(params, page))
        except RuntimeError as exc:
            raise RuntimeError(f"{exc} on results page {page}") from exc
        fetch_seconds = time.perf_counter() - started
        if timings is not None:
            timings["search_fetch_seconds"] = round(float(timings.get("search_fetch_seconds") or 0) + fetch_seconds, 3)
        cards, total = parse_page(html)
        if page == 1 and looks_blocked(html, cards, total):
            raise RuntimeError(f"Hyundai returned a page with no car list ({len(html)} bytes), probably a bot "
                               f"check; fetched with {_fetcher()}")
        if total is not None:
            pages = max(1, math.ceil(total / PAGE_SIZE))
        new_cards = [c for c in cards if c.get("id") not in seen_ids]
        rows = 0
        for c in new_cards:
            seen_ids.add(str(c.get("id")))
            if c.get("registration"):
                result.raw_regs.add(c["registration"])
            if not card_wanted(c, car):
                continue
            body = c.get("body_type")
            if body is None and str(car.get("body_type") or "Any") != "Any":
                c["body_type"] = body = str(car["body_type"])  # the site's own body filter chose it
            row = card_to_row(c, home, postcode_location(c.get("postcode")) if home else None)
            if not row or row["registration"] in seen:
                continue
            if body_and_seats_match(car, row.get("body_type"), row.get("seats")):
                seen.add(row["registration"])
                result.rows.append(row)
                rows += 1
        pages_log.append({"search": car.get("name"), "page": page, "fetch_seconds": round(fetch_seconds, 3),
                          "vehicle_objects": len(cards), "rows": rows, "total": total})
        if not new_cards or len(cards) < PAGE_SIZE:
            break
        page += 1
    if timings is not None:
        timings.setdefault("pages", []).extend(pages_log)
    result.debug_text = "\n".join(json.dumps(p) for p in pages_log)
    return result
