"""Volkswagen Approved Used source module.

Searches usedcars.volkswagen.co.uk and returns cars in the CarFinder standard
listing format (see ``app/sources/__init__.py``).

The page parsing below is the proven CarFinder VW scraper, moved here from
``scripts/fetch_vw_used.py`` unchanged apart from taking the search URL and
the wanted-car rules from a car search in Settings instead of hard-coded
Polo values.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any
from urllib.parse import quote, urlencode

import requests
from bs4 import BeautifulSoup

from app.db import now_iso
from app.sources import SearchResult, body_and_seats_match, classify_body_type, standardise

SOURCE_KEY = "vw"
SOURCE_NAME = "Volkswagen Approved Used"
MAKES = ("Volkswagen",)

SITE_ROOT = "https://usedcars.volkswagen.co.uk"
SEARCH_BASE_URL = f"{SITE_ROOT}/en/vehicle_search/all-brands/all-models"
DEFAULT_POOLS_CSV = "47-12532-217084"
MAX_PAGES = 5
# A search with no model (e.g. "any VW estate") covers far more stock.
MAX_PAGES_ANY_MODEL = 12

REG_PATTERN = re.compile(r"^[A-Z]{2}\d{2}[A-Z]{3}$")
RAW_REG_PATTERN = re.compile(r"[A-Z]{2}\s?\d{2}\s?[A-Z]{3}", re.I)
PLACEHOLDER_PHOTO_PATTERN = re.compile(r"nopic[-_\s]*coming[-_\s]*soon|coming[-_\s]+soon|placeholder", re.I)


def add_timing(timings: dict[str, Any] | None, key: str, seconds: float) -> None:
    if timings is None:
        return
    timings[key] = round(float(timings.get(key) or 0) + seconds, 3)


def normalise_reg(reg: str | None) -> str | None:
    compact = re.sub(r"\s+", "", (reg or "").upper())
    if not REG_PATTERN.match(compact):
        return None
    return f"{compact[:4]} {compact[4:]}"


def fetch_html(url: str, timeout: int = 45) -> str:
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/125.0 Safari/537.36"
        ),
        "Accept-Language": "en-GB,en;q=0.9",
    }
    response = requests.get(url, headers=headers, timeout=timeout)
    response.raise_for_status()
    return response.text



def find_matching_brace(text: str, start: int) -> int | None:
    depth = 0
    in_string = False
    escaped = False

    for idx in range(start, len(text)):
        ch = text[idx]

        if escaped:
            escaped = False
            continue

        if ch == "\\":
            escaped = True
            continue

        if ch == '"':
            in_string = not in_string
            continue

        if in_string:
            continue

        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return idx

    return None


def score_vehicle_object(obj: dict[str, Any]) -> int:
    """Prefer the full VW inventory object over a tiny nested fragment."""
    useful_keys = (
        "LICENSE_NUMBER_STR", "PRICE_RETAIL_CUR_FLT", "MILEAGE_MIL_INT",
        "MODEL_TEXT_STR", "MODEL_TYPE_STR", "SUB_MODEL_TEXT_STR", "TRIM_STR",
        "POOL_NAME1_STR", "DISTANCE_LEN_FLT", "BODY_COLOR_STR", "BODY_BASE_COLOR_LST",
        "ID", "LEGACY_CHIFFRE_STR", "VIN_STR",
    )
    return sum(1 for key in useful_keys if obj.get(key) not in (None, "")) + min(len(obj), 50)


def extract_license_regs_from_html(html: str) -> set[str]:
    """Return every UK-style registration visible in the VW page source.

    This is used as a safety net for the missing/not-seen step.  If VW changes
    the card JSON shape and we fail to build a full row for one result, we still
    avoid incorrectly marking an existing tracked car as missing.
    """
    regs: set[str] = set()

    for match in re.finditer(r'"LICENSE_NUMBER_STR"\s*:\s*"([A-Z0-9 ]+)"', html, re.I):
        reg = normalise_reg(match.group(1))
        if reg:
            regs.add(reg)

    # Some VW fragments expose the registration in text rather than the exact
    # LICENSE_NUMBER_STR field.  Keep this broad but still UK-reg shaped.
    for match in RAW_REG_PATTERN.finditer(html.upper()):
        reg = normalise_reg(match.group(0))
        if reg:
            regs.add(reg)

    return regs


def extract_vehicle_objects(html: str) -> list[dict[str, Any]]:
    vehicles: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    for match in re.finditer(r'"LICENSE_NUMBER_STR"\s*:\s*"([A-Z0-9 ]+)"', html, re.I):
        # The closest preceding "{" is not always the start of the full vehicle
        # object.  Try a set of nearby opening braces and keep the richest parsed
        # object that contains the same registration.
        window_start = max(0, match.start() - 50000)
        candidate_starts = [window_start + m.start() for m in re.finditer(r"\{", html[window_start:match.start() + 1])]
        best_obj: dict[str, Any] | None = None
        best_score = -1

        for start in reversed(candidate_starts[-80:]):
            end = find_matching_brace(html, start)
            if end is None or end < match.end():
                continue

            raw_obj = html[start : end + 1]
            try:
                obj = json.loads(raw_obj)
            except json.JSONDecodeError:
                continue

            if not isinstance(obj, dict):
                continue

            reg = normalise_reg(obj.get("LICENSE_NUMBER_STR"))
            wanted_reg = normalise_reg(match.group(1))
            if not reg or reg != wanted_reg:
                continue

            score = score_vehicle_object(obj)
            if score > best_score:
                best_obj = obj
                best_score = score

        if best_obj is None:
            continue

        reg = normalise_reg(best_obj.get("LICENSE_NUMBER_STR"))
        vehicle_id = str(best_obj.get("ID") or best_obj.get("LEGACY_CHIFFRE_STR") or best_obj.get("VIN_STR") or reg)
        if vehicle_id in seen_ids:
            continue

        seen_ids.add(vehicle_id)
        vehicles.append(best_obj)

    return vehicles


def extract_jsonld_info_map(html: str) -> dict[str, dict[str, Any]]:
    """
    VW includes exact vehicle detail URLs in JSON-LD blocks.  Some pages also
    expose image lists/counts there even when the inventory object itself does
    not, so keep both values by SKU.
    """
    soup = BeautifulSoup(html, "lxml")
    info_map: dict[str, dict[str, Any]] = {}

    for script in soup.find_all("script", attrs={"type": "application/ld+json"}):
        raw = script.string or script.get_text()
        if not raw:
            continue

        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            continue

        cars = find_jsonld_cars(data)
        for car in cars:
            sku = str_or_none(car.get("sku"))
            if not sku:
                continue

            key = sku.upper()
            entry = info_map.setdefault(key, {})

            url = str_or_none(car.get("url"))
            if url:
                entry["url"] = url

            photo_count = find_photo_count(car)
            if photo_count is not None:
                entry["photo_count"] = max(int(entry.get("photo_count") or 0), int(photo_count))

    return info_map


# Backwards-compatible wrapper for any older imports/tests.
def extract_jsonld_url_map(html: str) -> dict[str, str]:
    return {key: value["url"] for key, value in extract_jsonld_info_map(html).items() if value.get("url")}

def find_jsonld_cars(value: Any) -> list[dict[str, Any]]:
    cars: list[dict[str, Any]] = []

    if isinstance(value, dict):
        if value.get("@type") == "Car":
            cars.append(value)

        for child in value.values():
            cars.extend(find_jsonld_cars(child))

    elif isinstance(value, list):
        for item in value:
            cars.extend(find_jsonld_cars(item))

    return cars


def int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def str_or_none(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def find_explicit_photo_count(value: Any) -> int | None:
    """Find explicit UI photo counts such as 1/1, 1/34, or "34 photos".

    These are more reliable than counting image-like URLs because VW sometimes
    includes several stock/placeholder image URLs for a car with no dealer
    photos.  A visible 1/1 therefore means awaiting photos even if other media
    fragments exist in the JSON.
    """
    text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    counts: list[int] = []

    for match in re.finditer(r"\b1\s*(?:/|of)\s*(\d{1,3})\b", text, re.I):
        counts.append(int(match.group(1)))

    for match in re.finditer(r"\b(\d{1,3})\s*(?:photo|photos|image|images|picture|pictures)\b", text, re.I):
        counts.append(int(match.group(1)))

    if not counts:
        return None

    return max(counts)


def find_photo_count(value: Any, key_hint: str = "") -> int | None:
    """
    Try to infer the VW photo count.

    VW has changed the field names several times.  This therefore looks for:
    - text such as 1/40, 1 of 40, "40 photos"
    - likely numeric count fields
    - image/media/gallery lists
    - lists of image URL strings, even when the parent key is not obviously named
    """
    hint = key_hint.lower()
    photo_words = ("photo", "photos", "image", "images", "picture", "pictures", "media", "gallery", "slider")
    count_words = ("count", "number", "total")

    if isinstance(value, dict):
        best: int | None = None
        for key, child in value.items():
            combined_hint = f"{hint} {key}".strip()
            child_count = find_photo_count(child, combined_hint)
            if child_count is not None:
                best = max(best or 0, child_count)
        return best

    if isinstance(value, list):
        # A plain list of image URLs is common in JSON-LD: "image": ["...", "..."]
        if value and all(isinstance(item, str) for item in value):
            image_like = [item for item in value if re.search(r"\.(jpg|jpeg|png|webp)(\?|$)|/image|/images|/photo|/photos|/media", item, re.I)]
            if image_like:
                return len(image_like)

        if any(word in hint for word in photo_words):
            return len(value) if value else None

        best: int | None = None
        for child in value:
            child_count = find_photo_count(child, hint)
            if child_count is not None:
                best = max(best or 0, child_count)
        return best

    if isinstance(value, str):
        # Search-card text often contains "1/40" or similar.
        match = re.search(r"\b1\s*(?:/|of)\s*(\d{1,3})\b", value, re.I)
        if match:
            return int(match.group(1))

        # JSON/labels sometimes contain "40 photos".
        match = re.search(r"\b(\d{1,3})\s*(?:photo|photos|image|images|picture|pictures)\b", value, re.I)
        if match:
            return int(match.group(1))

        # A single image URL counts as one only when the key says image/photo.
        if any(word in hint for word in photo_words) and re.search(r"\.(jpg|jpeg|png|webp)(\?|$)|/image|/images|/photo|/photos|/media", value, re.I):
            return 1

        # Do not lift arbitrary numbers from keys such as INTEREST_AMOUNT,
        # NUMBER_OF_DOORS or TOTAL_WIDTH; they are not photo counts.
        return None

    if isinstance(value, (int, float)):
        has_photo_word = any(word in hint for word in photo_words)
        has_count_word = any(word in hint for word in count_words)
        if not (has_photo_word and has_count_word):
            return None
        try:
            number = int(value)
        except (TypeError, ValueError):
            return None
        return number if number >= 0 else None

    return None


def has_coming_soon_photo_marker(value: Any) -> bool:
    """Return True when VW explicitly shows a stock/placeholder photo marker.

    The search result and detail page can show a generic VW image labelled
    "Coming Soon".  That is not a dealer photo, even though it is technically
    an image in the page data.
    """
    text = json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value
    return bool(PLACEHOLDER_PHOTO_PATTERN.search(text))


def photo_status_from_detail_html(html: str) -> str | None:
    """Classify photos from the vehicle detail page when possible.

    VW's detail page exposes the main image in meta tags.  For cars awaiting
    dealer photos that image is currently nopic-coming-soon.jpg.  Treat that as
    awaiting photos even if the search-card JSON contains other image-like URLs.
    """
    if not html:
        return None

    soup = BeautifulSoup(html, "lxml")
    image_values: list[str] = []
    for tag in soup.find_all("meta"):
        key = (tag.get("itemprop") or tag.get("property") or tag.get("name") or "").lower()
        if "image" not in key:
            continue
        content = str_or_none(tag.get("content"))
        if content:
            image_values.append(content)

    if any(PLACEHOLDER_PHOTO_PATTERN.search(value) for value in image_values):
        return "awaiting"

    explicit_count = find_explicit_photo_count(html)
    if explicit_count is not None:
        return photo_status_from_count(explicit_count)

    if image_values:
        return "photos"

    return None


def refine_photo_status_from_detail_page(row: dict[str, Any], timings: dict[str, Any] | None = None) -> None:
    """Use the detail page as a final override for stock/coming-soon photos.

    Search results can include cached or stock media URLs.  The detail page meta
    image is a clearer signal: nopic-coming-soon.jpg means no dealer photos.
    If the detail page cannot be fetched or classified, leave the search result
    classification unchanged.
    """
    url = str_or_none(row.get("url"))
    if not url or "/vehicle_search/" not in url:
        return

    timings = timings if timings is not None else None
    if timings is not None:
        timings["detail_fetch_count"] = int(timings.get("detail_fetch_count") or 0) + 1

    started = time.perf_counter()
    try:
        detail_html = fetch_html(url, timeout=12)
    except requests.RequestException:
        add_timing(timings, "detail_fetch_seconds", time.perf_counter() - started)
        if timings is not None:
            timings["detail_fetch_errors"] = int(timings.get("detail_fetch_errors") or 0) + 1
        return

    add_timing(timings, "detail_fetch_seconds", time.perf_counter() - started)
    detail_status = photo_status_from_detail_html(detail_html)
    if detail_status in {"photos", "awaiting"}:
        row["photo_status"] = detail_status
        if detail_status == "awaiting":
            row["photo_reason"] = "Detail page meta image is VW nopic-coming-soon placeholder"
        else:
            row["photo_reason"] = "Detail page image metadata indicates dealer photos"


def photo_status_from_count(photo_count: int | None, has_placeholder: bool = False) -> str:
    if has_placeholder:
        return "awaiting"
    if photo_count is None:
        return "unknown"
    if photo_count > 1:
        return "photos"
    return "awaiting"



# ---------------------------------------------------------------------------
# Car search -> VW search URL
# ---------------------------------------------------------------------------

def _model_code(model: str) -> str:
    code = re.sub(r"\s+", "_", (model or "").strip().upper())
    return f"VOLKSWAGEN_{code}" if code else ""


def build_search_url(car: dict[str, Any], settings: dict[str, Any]) -> str:
    """Return the VW Approved Used search URL for a car search.

    A URL pasted into the car search (``search_url``) always wins, so any VW
    filter that CarFinder does not know about can still be used.
    """
    pasted = str(car.get("search_url") or "").strip()
    if pasted:
        return pasted

    params: list[tuple[str, str]] = [
        ("POOLS_CSV", DEFAULT_POOLS_CSV),
        ("RADIUS_LEN_FLT", str(int(settings.get("search_radius_miles") or 900))),
    ]
    if car.get("price_min") is not None:
        params.append(("PRICE_RETAIL_CUR_FLT_FROM", str(int(car["price_min"]))))
    if car.get("price_max") is not None:
        params.append(("PRICE_RETAIL_CUR_FLT_TO", str(int(car["price_max"]))))
    if car.get("mileage_max") is not None:
        params.append(("MILEAGE_MIL_INT_TO", str(int(car["mileage_max"]))))
    if car.get("power_min") is not None:
        params.append(("ENGINE_PWR_FLT_FROM", str(int(car["power_min"]))))
    params.extend([("search", "passenger"), ("MANUFACTURER_LST", "VOLKSWAGEN"), ("priceSwitch", "on")])

    fuel = car.get("fuel") or "Any"
    if fuel == "Petrol":
        params.append(("FUEL_TYPE_LST", "PETROL||SUPER"))
    elif fuel == "Diesel":
        params.append(("FUEL_TYPE_LST", "DIESEL"))
    if (car.get("transmission") or "Any") == "Manual":
        params.append(("TRANSMISSION_LST", "MANUAL"))
    if car.get("trim"):
        params.append(("TRIM_STR", str(car["trim"])))
    model_code = _model_code(str(car.get("model") or ""))
    if model_code:
        params.append(("MODEL_TYPE_LST", model_code))
    postcode = str(settings.get("home_postcode") or "").strip()
    if postcode:
        params.append(("ZIP_LOC", postcode))
    params.append(("sort", "PRICE_RETAIL_CUR_FLT:ASC"))

    return f"{SEARCH_BASE_URL}?{urlencode(params, safe='|:', quote_via=quote)}"


def page_url(search_url: str, page_number: int) -> str:
    if page_number <= 1:
        return search_url

    if re.search(r"/page\d+(?=[/?#]|$)", search_url):
        return re.sub(r"/page\d+(?=[/?#]|$)", f"/page{page_number}", search_url, count=1)

    if "?" in search_url:
        base, query = search_url.split("?", 1)
        return f"{base}/page{page_number}?{query}"

    return f"{search_url.rstrip('/')}/page{page_number}"


# ---------------------------------------------------------------------------
# Wanted-car rules (from the car search in Settings)
# ---------------------------------------------------------------------------

def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (text or "").lower())


def fuel_matches(wanted: str, fuel_text: str) -> bool:
    fuel = (fuel_text or "").lower()
    if not fuel or wanted in ("", "Any"):
        return True
    if wanted == "Petrol":
        return "petrol" in fuel or "super" in fuel
    if wanted == "Diesel":
        return "diesel" in fuel
    if wanted == "Hybrid":
        return "hybrid" in fuel or ("electric" in fuel and ("petrol" in fuel or "diesel" in fuel))
    if wanted == "Electric":
        return "electric" in fuel and "petrol" not in fuel and "diesel" not in fuel
    return True


def transmission_matches(wanted: str, transmission_text: str) -> bool:
    transmission = (transmission_text or "").lower()
    if not transmission or wanted in ("", "Any"):
        return True
    if wanted == "Manual":
        return "manual" in transmission
    if wanted == "Automatic":
        return "manual" not in transmission
    return True


def vw_body_and_seats(obj: dict[str, Any]) -> tuple[str | None, int | None]:
    """Body type and seat count from a VW record.

    VW's field names for these are not confirmed yet, so look for any field
    whose name mentions BODY (but not colour) or SEAT, and fall back to the
    model name (e.g. "Golf Estate", "Tiguan Allspace").
    """
    body_texts: list[str] = []
    seats: int | None = None
    for key, value in obj.items():
        name = str(key).upper()
        if "BODY" in name and "COLOR" not in name and "COLOUR" not in name and isinstance(value, (str, list)):
            body_texts.append(" ".join(str(v) for v in value) if isinstance(value, list) else value)
        elif "SEAT" in name and seats is None:
            number = int_or_none(value)
            if number is not None and 1 < number < 10:
                seats = number
    body = classify_body_type(*body_texts) or classify_body_type(
        obj.get("MODEL_TEXT_STR"), obj.get("SUB_MODEL_TEXT_STR"), obj.get("MODEL_TYPE_STR")
    )
    model_text = " ".join(str(obj.get(k) or "") for k in ("MODEL_TEXT_STR", "SUB_MODEL_TEXT_STR")).lower()
    if seats is None and "allspace" in model_text:
        seats = 7
    return body, seats


def is_wanted_vehicle(obj: dict[str, Any], car: dict[str, Any]) -> bool:
    reg = normalise_reg(obj.get("LICENSE_NUMBER_STR"))
    if not reg:
        return False

    trim = str(obj.get("TRIM_STR") or "").lower()
    transmission = str(obj.get("TRANSMISSION_LST") or "").lower()
    fuel = str(obj.get("FUEL_TYPE_LST") or obj.get("FUEL_TYPE_COMBINED_LST") or "").lower()

    price = int_or_none(obj.get("PRICE_RETAIL_CUR_FLT"))
    mileage = int_or_none(obj.get("MILEAGE_MIL_INT"))
    power = int_or_none(obj.get("ENGINE_PWR_FLT"))
    year = int_or_none(obj.get("YEAR_OF_MODEL_INT"))

    # The VW page URL is the main filter.  Only reject a result when VW
    # provides a field that clearly contradicts the car search.  Missing or
    # renamed fields must not make genuine results vanish and then get
    # marked as missing.
    searchable_text = " ".join(str(obj.get(k) or "") for k in (
        "MODEL_TEXT_STR", "MODEL_TYPE_STR", "SUB_MODEL_TEXT_STR", "TRIM_STR",
        "TRANSMISSION_LST", "FUEL_TYPE_LST", "FUEL_TYPE_COMBINED_LST"
    )).lower().strip()

    wanted_model = _squash(str(car.get("model") or ""))
    if wanted_model and searchable_text and wanted_model not in _squash(searchable_text):
        return False
    wanted_trim = str(car.get("trim") or "").strip().lower()
    if wanted_trim and trim and trim != wanted_trim:
        return False
    if not transmission_matches(str(car.get("transmission") or "Any"), transmission):
        return False
    if not fuel_matches(str(car.get("fuel") or "Any"), fuel):
        return False
    if price is not None:
        if car.get("price_min") is not None and price < int(car["price_min"]):
            return False
        if car.get("price_max") is not None and price > int(car["price_max"]):
            return False
    if mileage is not None and car.get("mileage_max") is not None and mileage > int(car["mileage_max"]):
        return False
    if power is not None and car.get("power_min") is not None and power < int(car["power_min"]):
        return False
    if year is not None and car.get("year_min") is not None and year < int(car["year_min"]):
        return False
    body_type, seats = vw_body_and_seats(obj)
    if not body_and_seats_match(car, body_type, seats):
        return False

    return True


# ---------------------------------------------------------------------------
# VW record -> standard listing
# ---------------------------------------------------------------------------

def absolute_url(value: str | None) -> str | None:
    text = str_or_none(value)
    if not text:
        return None
    if text.startswith("//"):
        return "https:" + text
    if text.startswith("/"):
        return SITE_ROOT + text
    return text


def build_vehicle_url(obj: dict[str, Any], info_map: dict[str, dict[str, Any]], search_url: str) -> str | None:
    possible_keys = [
        str_or_none(obj.get("ID")),
        str_or_none(obj.get("LEGACY_CHIFFRE_STR")),
        str_or_none(obj.get("VIN_STR")),
    ]

    for key in possible_keys:
        if not key:
            continue
        entry = info_map.get(key.upper())
        if entry and entry.get("url"):
            return absolute_url(str(entry["url"]))

    # Fallback: registration-specific VW search, better than the dealer homepage.
    reg = normalise_reg(obj.get("LICENSE_NUMBER_STR"))
    if reg:
        compact_reg = reg.replace(" ", "")
        return f"{SEARCH_BASE_URL}/page1?search={compact_reg}"

    return search_url


def object_to_row(obj: dict[str, Any], info_map: dict[str, dict[str, Any]], car: dict[str, Any], search_url: str) -> dict[str, Any]:
    reg = normalise_reg(obj.get("LICENSE_NUMBER_STR"))
    assert reg is not None

    year = int_or_none(obj.get("YEAR_OF_MODEL_INT"))
    price = int_or_none(obj.get("PRICE_RETAIL_CUR_FLT"))
    previous_price = int_or_none(obj.get("PRICE_RETAIL_PREVIOUS_CUR_FLT"))
    mileage = int_or_none(obj.get("MILEAGE_MIL_INT"))
    distance = int_or_none(obj.get("DISTANCE_LEN_FLT"))
    explicit_photo_count = find_explicit_photo_count(obj)
    photo_count = explicit_photo_count if explicit_photo_count is not None else find_photo_count(obj)
    photo_count_source = "search page explicit count" if explicit_photo_count is not None else "search page inferred count"

    for key in (str_or_none(obj.get("ID")), str_or_none(obj.get("LEGACY_CHIFFRE_STR")), str_or_none(obj.get("VIN_STR"))):
        if not key:
            continue
        jsonld_count = info_map.get(key.upper(), {}).get("photo_count")
        if jsonld_count is not None and explicit_photo_count is None:
            photo_count = max(photo_count or 0, int(jsonld_count))
            photo_count_source = "search page JSON-LD image count"

    has_placeholder = has_coming_soon_photo_marker(obj)
    photo_status = photo_status_from_count(photo_count, has_placeholder)
    if has_placeholder:
        photo_reason = "Coming Soon / placeholder marker found in search-card data"
    elif photo_count is None:
        photo_reason = "No clear photo count found on search card"
    elif int(photo_count) <= 1:
        photo_reason = f"{photo_count_source} = {int(photo_count)}; treating 1/1 as stock/awaiting"
    else:
        photo_reason = f"{photo_count_source} = {int(photo_count)}; more than one image means dealer photos"

    dealer = str_or_none(obj.get("POOL_NAME1_STR"))
    location = str_or_none(obj.get("POOL_CITY_STR"))
    colour = str_or_none(obj.get("BODY_COLOR_STR") or obj.get("BODY_BASE_COLOR_LST"))
    trim = str_or_none(obj.get("TRIM_STR"))
    model = str_or_none(obj.get("MODEL_TEXT_STR")) or str_or_none(car.get("model"))
    fuel = str_or_none(obj.get("FUEL_TYPE_LST") or obj.get("FUEL_TYPE_COMBINED_LST"))
    transmission = str_or_none(obj.get("TRANSMISSION_LST"))
    body_type, seats = vw_body_and_seats(obj)

    title = f"{year or ''} Volkswagen {model or ''} {obj.get('SUB_MODEL_TEXT_STR') or trim or ''} {reg}"
    title = " ".join(title.split())

    return standardise({
        "registration": reg,
        "make": "Volkswagen",
        "model": model,
        "trim": trim,
        "year": year,
        "colour": colour,
        "fuel": fuel,
        "transmission": transmission,
        "body_type": body_type,
        "seats": seats,
        "mileage": mileage,
        "price": price,
        "previous_price": previous_price,
        "dealer": dealer,
        "location": location,
        "distance_miles": distance,
        "photo_status": photo_status,
        "photo_reason": photo_reason,
        "photo_count": photo_count,
        "url": build_vehicle_url(obj, info_map, search_url),
        "raw_text": json.dumps(obj, ensure_ascii=False, sort_keys=True),
        "title": title,
        "source": SOURCE_KEY,
        "status": "active",
        "last_seen": now_iso(),
    })


def extract_rows_from_html(html: str, car: dict[str, Any], search_url: str, timings: dict[str, Any] | None = None) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    seen_regs: set[str] = set()
    info_map = extract_jsonld_info_map(html)

    for obj in extract_vehicle_objects(html):
        if not is_wanted_vehicle(obj, car):
            continue

        row = object_to_row(obj, info_map, car, search_url)
        reg = row["registration"]

        if reg in seen_regs:
            continue

        # Detail-page requests are slow.  Only use the detail page when the
        # search-card data cannot classify the photo state.  Clear search-card
        # counts are authoritative:
        #   1/1 or 1 / 1  -> awaiting photos / stock image only
        #   1/2 or higher -> dealer photos
        if row.get("photo_status") == "unknown":
            refine_photo_status_from_detail_page(row, timings)

        seen_regs.add(reg)
        rows.append(row)

    return rows


def search(car: dict[str, Any], settings: dict[str, Any], timings: dict[str, Any] | None = None) -> SearchResult:
    """Run one car search against VW Approved Used."""
    search_url = build_search_url(car, settings)
    result = SearchResult(search_url=search_url)
    seen_regs: set[str] = set()
    html_parts: list[str] = []
    text_parts: list[str] = []
    page_timings: list[dict[str, Any]] = []

    max_pages = MAX_PAGES if (car.get("model") or car.get("search_url")) else MAX_PAGES_ANY_MODEL
    for page_number in range(1, max_pages + 1):
        url = page_url(search_url, page_number)

        page_started = time.perf_counter()
        fetch_started = time.perf_counter()
        html = fetch_html(url)
        fetch_seconds = time.perf_counter() - fetch_started
        add_timing(timings, "search_fetch_seconds", fetch_seconds)
        html_parts.append(f"\n\n<!-- PAGE {page_number}: {url} -->\n\n{html}")

        parse_started = time.perf_counter()
        soup = BeautifulSoup(html, "lxml")
        text = soup.get_text("\n", strip=True)
        text_parts.append(f"\n\n===== PAGE {page_number}: {url} =====\n\n{text}")

        page_raw_regs = extract_license_regs_from_html(html)
        result.raw_regs.update(page_raw_regs)
        vehicle_object_count = len(extract_vehicle_objects(html))
        rows = extract_rows_from_html(html, car, search_url, timings)
        parse_seconds = time.perf_counter() - parse_started
        add_timing(timings, "parsing_seconds", parse_seconds)

        page_timings.append({
            "search": car.get("name"),
            "page": page_number,
            "fetch_seconds": round(fetch_seconds, 3),
            "parse_and_detail_seconds": round(parse_seconds, 3),
            "total_seconds": round(time.perf_counter() - page_started, 3),
            "vehicle_objects": vehicle_object_count,
            "rows": len(rows),
            "raw_regs": len(page_raw_regs),
        })

        new_count = 0
        for row in rows:
            if row["registration"] in seen_regs:
                continue
            seen_regs.add(row["registration"])
            result.rows.append(row)
            new_count += 1

        if page_number > 1 and new_count == 0 and vehicle_object_count == 0:
            break
        if vehicle_object_count < 20:
            break

    if timings is not None:
        timings.setdefault("pages", []).extend(page_timings)

    result.debug_html = "\n".join(html_parts)
    result.debug_text = "\n".join(text_parts)
    return result
