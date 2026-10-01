"""Mitsubishi (Mitsubishi Motors UK approved used) source module.

Searches https://usedcars.mitsubishi-motors.co.uk, Mitsubishi UK's used vehicle
locator (an Autoweb dealer site).  The results are ordinary server-rendered
pages, 100 cars each:

* the search is in the path: ``/usedcars/mitsubishi[/<model slug>]
  [/pricemin/<n>/pricemax/<n>][/page/<n>]``.  Model slugs are the search
  form's (``outlander``, ``outlander-phev``, ``shogun-sport`` ...); "Outlander"
  and "Outlander PHEV" are separate slugs, so CarFinder runs every slug the typed
  model matches.  The whole UK stock is only about 300 cars, so nothing else is
  sent and every other limit is applied here;
* each result card gives the advert id and link, number of photos, version and
  year, gearbox, body style, fuel, mileage, town and price.  Sold cars stay
  listed for a while (price shown as "Sold") and are left out, as are L200
  pick-ups and other commercials (priced "+ VAT");
* the card has no registration or dealer name, so for each car that passes
  every filter the advert page is read once: its analytics block has the
  registration, first-registration date, VIN, colour and dealer, and the dealer
  address ends with the postcode (used for the distance).  These are cached in
  ``data/cache/mitsubishi_adverts.json`` by advert id;
* the site's body style is not reliable (an Eclipse Cross can be a
  "Hatchback", an Outlander an "Estate"), so the body type comes from the model.
"""
from __future__ import annotations

import json
import random
import re
import time
import unicodedata
from pathlib import Path
from typing import Any

import requests
from bs4 import BeautifulSoup

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

SOURCE_KEY = "mitsubishi"
SOURCE_NAME = "Mitsubishi Approved Used"
MAKES = ("Mitsubishi",)

SITE_ROOT = "https://usedcars.mitsubishi-motors.co.uk"
SEARCH_ROOT = SITE_ROOT + "/usedcars/mitsubishi"
PAGE_SIZE = 100
MAX_PAGES = 10
IMPERSONATE = "chrome"
PAGE_DELAY = 0.5
PAGE_JITTER = 0.5
RETRY_WAITS = (10.0, 30.0)
CACHE_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "cache" / "mitsubishi_adverts.json"

# The search form's model slugs (Oct 2026), used when the live list cannot be read.
KNOWN_SLUGS = ("asx", "colt", "eclipse-cross", "l200", "lancer", "mirage", "outlander", "outlander-phev", "shogun",
               "shogun-sport")
MODEL_BODIES = {"asx": "SUV", "eclipsecross": "SUV", "outlander": "SUV", "shogun": "SUV", "mirage": "Hatchback",
                "colt": "Hatchback"}
COMMERCIAL_MODELS = ("l200",)
NO_IMAGE = "noimage"
PLATE_IN_IMAGE = re.compile(r"-([a-z]{2}\d{2}[a-z]{3})-(?:\d+-)?\d+\.jpe?g", re.I)

HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-GB,en;q=0.9",
    "Referer": SITE_ROOT + "/",
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
}


def _plain(text: Any) -> str:
    text = unicodedata.normalize("NFKD", str(text or "")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "", text.lower())


def _int(value: Any) -> int | None:
    if value in (None, ""):
        return None
    digits = re.sub(r"[^0-9]", "", str(value))
    return int(digits) if digits else None


def _text(node: Any) -> str:
    return re.sub(r"\s+", " ", node.get_text(" ") if node is not None else "").strip()


def is_mitsubishi(make: Any) -> bool:
    return _plain(make) in ("mitsubishi", "mitsubishimotors")


def _model_key(name: Any) -> str:
    key = _plain(name)
    return key[10:] if key.startswith("mitsubishi") and len(key) > 10 else key


def slugs_for(model: str, known: tuple[str, ...] | list[str]) -> list[str]:
    """Search slugs for what the user typed ("Outlander" -> outlander and outlander-phev)."""
    typed = _model_key(model)
    if not typed:
        return []
    return [slug for slug in known if _plain(slug).startswith(typed)]


# ---------------------------------------------------------------------------
# Car search -> page URL
# ---------------------------------------------------------------------------

def page_url(car: dict[str, Any], slug: str | None, page: int = 1) -> str:
    parts = [SEARCH_ROOT]
    if slug:
        parts.append(slug)
    low, high = car.get("price_min"), car.get("price_max")
    if low is not None or high is not None:
        parts += ["pricemin", str(int(low or 0))]
        if high is not None:
            parts += ["pricemax", str(int(high))]
    if page > 1:
        parts += ["page", str(page)]
    return "/".join(parts)


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


def _get(url: str, timeout: int = 60) -> str:
    """GET a page, with a wait and a fresh connection after a 403/429."""
    global _session
    attempts = len(RETRY_WAITS) + 1
    response = None
    for attempt in range(attempts):
        response = _http().get(url, headers=_headers(), timeout=timeout)
        if response.status_code not in (403, 429):
            response.raise_for_status()
            return response.text
        if attempt < len(RETRY_WAITS):
            _session = None
            time.sleep(RETRY_WAITS[attempt])
    status = response.status_code if response is not None else "?"
    server = (response.headers.get("Server") if response is not None else None) or "the site"
    raise RuntimeError(f"Mitsubishi blocked the request (HTTP {status} from {server}, {attempts} tries; "
                       f"fetched with {_fetcher()})")


def site_slugs() -> list[str]:
    """Model slugs from the site's search form (in case a model has been added)."""
    soup = BeautifulSoup(_get(SITE_ROOT + "/", timeout=30), "lxml")
    select = soup.find("select", attrs={"name": "model"})
    return [o.get("value") for o in (select.find_all("option") if select else []) if o.get("value")]


# ---------------------------------------------------------------------------
# Results page -> cards
# ---------------------------------------------------------------------------

def page_count(soup: BeautifulSoup) -> int | None:
    match = re.search(r"Page\s+\d+\s+of\s+(\d+)", soup.get_text(" "))
    return int(match.group(1)) if match else None


def _spec(card: Any) -> dict[str, str]:
    out = {}
    for spec in card.select(".us-result-spec"):
        name = _text(spec.select_one(".us-result-spec-name")).rstrip(":").lower()
        out[name] = _text(spec.find("strong"))
    return out


def tidy_model(name: Any) -> str | None:
    """"ECLIPSE CROSS" -> "Eclipse Cross", "ASX" and "L200" as they are."""
    words = re.sub(r"\s+", " ", str(name or "")).strip().split(" ")
    text = " ".join(w if (len(w) <= 3 and w.isalpha()) or any(ch.isdigit() for ch in w) else w.capitalize()
                    for w in words if w)
    return text or None


def body_for(model: Any, style: Any) -> str | None:
    key = _model_key(model)
    for name, body in MODEL_BODIES.items():
        if key.startswith(name):
            return body
    if re.search(r"pick\s*-?\s*up", str(style or ""), re.I):
        return None
    return classify_body_type(style)


def parse_card(card: Any) -> dict[str, Any] | None:
    """Everything on one result card (no filtering)."""
    holder = card.select_one("[data-vehicle-id]") or card
    advert_id = holder.get("data-vehicle-id")
    link = card.select_one(".us-result-name a") or card.select_one("a[href*='/used/']")
    if not advert_id or link is None:
        return None
    href = (link.get("href") or "").strip()
    name = card.select_one(".us-result-name")
    first_span = name.find("span", recursive=False) if name is not None else None
    version_text = _text(first_span)
    year = re.search(r"-\s*((?:19|20)\d{2})(?:\s*\(\d+\))?\s*$", version_text)
    version = re.sub(r"\s*-\s*(?:19|20)\d{2}(?:\s*\(\d+\))?\s*$", "", version_text).strip() or None
    title = _text(name.find("h3")) if name is not None else ""
    model = tidy_model(re.sub(r"^mitsubishi\s+", "", title, flags=re.I))
    promotion = _text(card.select_one(".us-promotion"))
    price_box = card.select_one(".Price")
    price_text = _text(price_box)
    sold = bool(re.search(r"\bsold\b", price_text, re.I)) or bool(re.search(r"\bsold\b", promotion, re.I))
    current = price_box.find("strong") if price_box is not None else None
    if current is not None and current.find("del") is not None:
        current.find("del").extract()
    picture = card.select_one(".us-result-image")
    image = re.search(r"url\(([^)]+)\)", picture.get("style", "") if picture is not None else "")
    image_url = image.group(1).strip() if image else ""
    plate = PLATE_IN_IMAGE.search(image_url)
    photos = re.search(r"x\s*(\d+)", _text(card.select_one(".photo-number")))
    spec = _spec(card)
    slug = re.match(r"/used/mitsubishi/([^/]+)/", href)
    return {
        "id": str(advert_id),
        "url": SITE_ROOT + href if href.startswith("/") else href,
        "model": model,
        "model_slug": slug.group(1) if slug else None,
        "trim": version,
        "promotion": promotion or None,
        "year": int(year.group(1)) if year else None,
        "transmission": spec.get("gearbox") or None,
        "body_text": spec.get("bodystyle") or None,
        "body_type": body_for(model, spec.get("bodystyle")),
        "fuel": spec.get("fuel type") or None,
        "mileage": _int(spec.get("mileage")),
        "location": _text(card.select_one(".us-result-location strong .hidden-xs")) or None,
        "price": None if sold else _int(_text(current)),
        "plus_vat": "vat" in price_text.lower() and "no vat" not in price_text.lower(),
        "sold": sold,
        "photo_count": 0 if NO_IMAGE in image_url else (int(photos.group(1)) if photos else 0),
        "image_plate": normalise_plate(plate.group(1)) if plate else None,
    }


def parse_page(html: str) -> tuple[list[dict[str, Any]], int | None]:
    soup = BeautifulSoup(html, "lxml")
    cards = [parse_card(c) for c in soup.select(".col-listing-grid") if c.select_one("[data-vehicle-id]")]
    return [c for c in cards if c], page_count(soup)


# ---------------------------------------------------------------------------
# Advert page -> registration, dealer (cached)
# ---------------------------------------------------------------------------

GTM_FIELD = re.compile(r"'(\w+)'\s*:\s*'([^']*)'")
POSTCODE = re.compile(r"\b([A-Z]{1,2}\d[A-Z\d]?\s*\d[A-Z]{2})\s*$", re.I)


def details_from_advert(html: str) -> dict[str, Any]:
    block = re.search(r"Variables passed to the details page(.*?)\}\s*\)", html, flags=re.S)
    fields = dict(GTM_FIELD.findall(block.group(1))) if block else {}
    soup = BeautifulSoup(html, "lxml")
    address = soup.select_one(".dealer-details-address")
    lines = [line.strip(" ,") for line in (address.get_text("\n") if address else "").split("\n") if line.strip(" ,")]
    postcode = POSTCODE.search(lines[-1]) if lines else None
    date = re.match(r"(\d{2})/(\d{2})/(\d{4})$", fields.get("registrationDate", ""))
    colour = fields.get("vehicleColour", "").strip()
    return {
        "registration": normalise_plate(fields.get("registration")),
        "first_registered": f"{date.group(3)}-{date.group(2)}-{date.group(1)}" if date else None,
        "vin": fields.get("vehicleVIN") or None,
        "colour": colour.capitalize() if colour.isupper() else (colour or None),
        "dealer": fields.get("dealerName") or _text(soup.select_one("#dealersName")) or None,
        "town": fields.get("town") or None,
        "postcode": postcode.group(1).upper() if postcode else None,
        "seller_id": fields.get("sellerId") or None,
    }


def _load_cache() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(CACHE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_cache(cache: dict[str, dict[str, Any]]) -> None:
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    CACHE_PATH.write_text(json.dumps(cache), encoding="utf-8")


def advert_details(c: dict[str, Any], cache: dict[str, dict[str, Any]],
                   timings: dict[str, Any] | None) -> dict[str, Any] | None:
    if c["id"] in cache:
        return cache[c["id"]]
    started = time.perf_counter()
    if timings is not None:
        timings["detail_fetch_count"] = int(timings.get("detail_fetch_count") or 0) + 1
    try:
        details = details_from_advert(_get(c["url"]))
    except Exception:  # noqa: BLE001 - requests or curl_cffi errors alike
        if timings is not None:
            timings["detail_fetch_errors"] = int(timings.get("detail_fetch_errors") or 0) + 1
        return None
    finally:
        if timings is not None:
            timings["detail_fetch_seconds"] = round(float(timings.get("detail_fetch_seconds") or 0)
                                                    + time.perf_counter() - started, 3)
    if details.get("registration"):
        cache[c["id"]] = details
    return details


# ---------------------------------------------------------------------------
# Filtering and rows
# ---------------------------------------------------------------------------

def is_commercial(c: dict[str, Any]) -> bool:
    if _plain(c.get("model_slug") or c.get("model")) in COMMERCIAL_MODELS or c.get("plus_vat"):
        return True
    return bool(re.search(r"pick\s*-?\s*up", str(c.get("body_text") or ""), re.I))


def record_wanted(c: dict[str, Any], car: dict[str, Any]) -> bool:
    if c.get("sold") or is_commercial(c):
        return False
    typed, model = _model_key(car.get("model")), _model_key(c.get("model"))
    if typed and model and not (typed.startswith(model) or model.startswith(typed)):
        return False  # "Outlander PHEV" adverts are titled "Outlander", so either way round
    wanted_trim = str(car.get("trim") or "").strip().lower()
    if wanted_trim and wanted_trim not in f"{c.get('trim') or ''} {c.get('promotion') or ''}".lower():
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


def record_to_row(c: dict[str, Any], details: dict[str, Any] | None,
                  home: tuple[float, float] | None) -> dict[str, Any] | None:
    details = details or {}
    reg = details.get("registration") or c.get("image_plate")
    if not reg:
        return None
    count = int(c.get("photo_count") or 0)
    if count > 1:
        photo_status, photo_reason = "photos", f"{count} dealer images"
    elif count == 1:
        photo_status, photo_reason = "awaiting", "Only one image; treating as stock/awaiting"
    else:
        photo_status, photo_reason = "awaiting", "No dealer images yet"
    town = details.get("town") or c.get("location")
    where = postcode_location(details.get("postcode")) or place_location(town)
    year = c.get("year")
    if details.get("first_registered"):
        year = int(details["first_registered"][:4])
    title = " ".join(str(p) for p in (year, "Mitsubishi", c.get("model"), c.get("trim"), reg) if p)
    raw = {"id": c.get("id"), "promotion": c.get("promotion"), "body_text": c.get("body_text"),
           "image_plate": c.get("image_plate"), **{k: details.get(k) for k in ("vin", "postcode", "seller_id")}}
    return standardise({
        "registration": reg,
        "make": "Mitsubishi",
        "model": c.get("model"),
        "trim": c.get("trim"),
        "year": year,
        "first_registered": details.get("first_registered"),
        "colour": details.get("colour"),
        "fuel": c.get("fuel"),
        "transmission": c.get("transmission"),
        "body_type": c.get("body_type"),
        "seats": None,
        "mileage": c.get("mileage"),
        "price": c.get("price"),
        "previous_price": None,
        "dealer": details.get("dealer") or (f"Mitsubishi {town}" if town else None),
        "location": town,
        "distance_miles": distance_miles(home, where) if where else None,
        "url": c.get("url") or SEARCH_ROOT,
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
    if not is_mitsubishi(car.get("make")):
        raise RuntimeError(f"Mitsubishi source does not search make '{car.get('make')}'")
    slugs: list[str | None] = [None]
    if car.get("model"):
        found = slugs_for(str(car["model"]), KNOWN_SLUGS)
        if not found:
            try:
                found = slugs_for(str(car["model"]), site_slugs())
            except Exception:  # noqa: BLE001 - the error below says what matters
                found = []
        if not found:
            raise RuntimeError(f"Mitsubishi has no model matching '{car['model']}' right now")
        slugs = list(found)

    home = postcode_location(settings.get("home_postcode"))
    result = SearchResult(search_url=page_url(car, slugs[0]))
    cache = _load_cache()
    cache_size = len(cache)
    seen: set[str] = set()
    seen_ids: set[str] = set()
    pages_log: list[dict[str, Any]] = []
    first_fetch = True
    for slug in slugs:
        page = 1
        while page <= MAX_PAGES:
            if not first_fetch:
                time.sleep(PAGE_DELAY + random.uniform(0, PAGE_JITTER))
            first_fetch = False
            started = time.perf_counter()
            try:
                cards, pages = parse_page(_get(page_url(car, slug, page)))
            except RuntimeError as exc:
                raise RuntimeError(f"{exc} on results page {page}") from exc
            fetch_seconds = time.perf_counter() - started
            if timings is not None:
                timings["search_fetch_seconds"] = round(float(timings.get("search_fetch_seconds") or 0)
                                                        + fetch_seconds, 3)
            new = [c for c in cards if c["id"] not in seen_ids]
            rows = 0
            for c in new:
                seen_ids.add(c["id"])
                if c.get("sold"):
                    continue  # left out of raw_regs too, so a car that has sold is marked missing
                known = cache.get(c["id"]) or {}
                if known.get("registration") or c.get("image_plate"):
                    result.raw_regs.add(known.get("registration") or c["image_plate"])
                if not record_wanted(c, car):
                    continue
                details = advert_details(c, cache, timings)
                if details and details.get("registration"):
                    result.raw_regs.add(details["registration"])
                row = record_to_row(c, details, home)
                if not row or row["registration"] in seen:
                    continue
                if body_and_seats_match(car, row.get("body_type"), row.get("seats")):
                    seen.add(row["registration"])
                    result.rows.append(row)
                    rows += 1
            pages_log.append({"search": car.get("name"), "model_slug": slug, "page": page,
                              "fetch_seconds": round(fetch_seconds, 3), "vehicle_objects": len(cards), "rows": rows,
                              "total_pages": pages})
            if not new or len(cards) < PAGE_SIZE or (pages is not None and page >= pages):
                break
            page += 1
    if len(cache) != cache_size:
        _save_cache(cache)
    if timings is not None:
        timings.setdefault("pages", []).extend(pages_log)
    result.debug_text = "\n".join(json.dumps(p) for p in pages_log)
    return result
