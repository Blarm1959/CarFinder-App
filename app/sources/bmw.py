"""BMW approved-used source module.

Searches https://usedcars.bmw.co.uk through the JSON behind its results page,
``/vehicle/api/list/``:

* the site needs its own CSRF cookie: CarFinder opens the results page once to
  get it and sends it back as the ``X-CSRFToken`` header (as the page does);
* filters are ordinary parameters: series=3 Series, body_type=Estate,
  fuel_type=Diesel, transmission=Manual, max_mileage, min_year,
  max_supplied_price; ``size`` and ``page`` do the paging;
* each car has the plate, cash price, mileage and real first-registration
  date; the dealer is given by name only (e.g. "Sytner Leicester"), so
  distance is the straight line from the person's postcode to the dealer's
  town.
"""
from __future__ import annotations

import json
import re
import time
from typing import Any

import requests

from app.db import now_iso
from app.geo import distance_miles, place_location, postcode_location
from app.sources import (
    SearchResult,
    body_and_seats_match,
    fuel_matches,
    normalise_plate,
    standardise,
    transmission_matches,
)

SOURCE_KEY = "bmw"
SOURCE_NAME = "BMW Approved Used"
MAKES = ("BMW",)

SITE_ROOT = "https://usedcars.bmw.co.uk"
LIST_URL = f"{SITE_ROOT}/vehicle/api/list/"
PAGE_SIZE = 100
MAX_PAGES = 15

BODY_VALUES = {"Estate": "Estate", "SUV": "SUV", "Saloon": "Saloon", "Hatchback": "Hatch"}
FUEL_VALUES = {"Petrol": "Petrol", "Diesel": "Diesel", "Hybrid": "Hybrid", "Electric": "Electric"}
GEAR_VALUES = {"Manual": "Manual", "Automatic": "Automatic"}


def _int(value: Any) -> int | None:
    try:
        return int(round(float(value))) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


def _words(text: Any) -> list[str]:
    return re.findall(r"[a-z0-9]+", str(text or "").lower())


# ---------------------------------------------------------------------------
# Model text -> BMW "series"
# ---------------------------------------------------------------------------

def series_for(model: str | None) -> str | None:
    """"3 Series Touring" -> "3 Series", "X5" -> "X Series", "iX3" -> "BMW i", "M3" -> "M"."""
    text = (model or "").strip().lower()
    if not text:
        return None
    match = re.match(r"^(\d)\s*(series)?\b", text)
    if match:
        return f"{match.group(1)} Series"
    if re.match(r"^i(x|\d|\s)", text):
        return "BMW i"
    if re.match(r"^x\d", text):
        return "X Series"
    if re.match(r"^m\d", text):
        return "M"
    if re.match(r"^z\d", text):
        return "Z Series"
    return None


def model_matches(wanted: str | None, title: str | None, derivative: str | None) -> bool:
    """Every word the user typed must appear in the title + derivative."""
    words = _words(wanted)
    if not words:
        return True
    have = " ".join(_words(title) + _words(derivative))
    return all(re.search(rf"(^|\s){re.escape(w)}", have) or w in have.replace(" ", "") for w in words)


def body_for(title: str | None, derivative: str | None, site_body: str | None = None) -> str | None:
    text = f"{title or ''} {derivative or ''}".lower()
    if "tourer" in text and "touring" not in text:
        return "MPV"  # 2 Series Active Tourer / Gran Tourer
    if "touring" in text:
        return "Estate"
    if re.search(r"\b(bmw\s+)?i?x\d", text):
        return "SUV"
    if "saloon" in text:
        return "Saloon"
    if site_body in {"Estate", "SUV", "Saloon"}:
        return site_body
    if site_body == "Hatch" or re.search(r"\b(1 series|5-door|3-door)\b", text):
        return "Hatchback"
    return None


def seats_for(title: str | None, derivative: str | None) -> int | None:
    """BMW doesn't list seats; use the known layouts.

    7: 2 Series Gran Tourer and X7.  Unknown: X5 and iX (7 seats optional).
    5: everything else (the site sells passenger cars only; 2-seat Z4 and
    4-seat coupés/convertibles never matter for a 7-seat search).
    """
    text = f"{title or ''} {derivative or ''}".lower()
    if "gran tourer" in text or re.search(r"\bx7\b", text):
        return 7
    if re.search(r"\bx5\b|\bix\b", text):
        return None
    return 5


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def open_session() -> requests.Session:
    session = requests.Session()
    session.headers.update({"User-Agent": "Mozilla/5.0 (CarFinder)", "Accept-Language": "en-GB,en;q=0.9"})
    session.get(f"{SITE_ROOT}/result/", timeout=30).raise_for_status()
    if not session.cookies.get("csrftoken"):
        raise RuntimeError("BMW search: the site did not provide its security cookie")
    return session


def _get(session: requests.Session, params: list[tuple[str, str]], timeout: int = 30) -> dict[str, Any]:
    response = session.get(
        LIST_URL,
        params=params,
        headers={
            "X-CSRFToken": session.cookies.get("csrftoken") or "",
            "X-Requested-With": "XMLHttpRequest",
            "Accept": "application/json",
            "Referer": f"{SITE_ROOT}/result/",
        },
        timeout=timeout,
    )
    response.raise_for_status()
    data = response.json()
    if not data.get("success"):
        raise RuntimeError(f"BMW search refused the request: {data.get('message') or 'unknown reason'}")
    return data


def build_params(car: dict[str, Any]) -> list[tuple[str, str]]:
    params: list[tuple[str, str]] = []
    series = series_for(car.get("model"))
    if series:
        params.append(("series", series))
    body = BODY_VALUES.get(str(car.get("body_type") or "Any"))
    if body:
        params.append(("body_type", body))
    fuel = FUEL_VALUES.get(str(car.get("fuel") or "Any"))
    if fuel:
        params.append(("fuel_type", fuel))
    gear = GEAR_VALUES.get(str(car.get("transmission") or "Any"))
    if gear:
        params.append(("transmission", gear))
    if car.get("mileage_max") is not None:
        params.append(("max_mileage", str(int(car["mileage_max"]))))
    if car.get("year_min") is not None:
        params.append(("min_year", str(int(car["year_min"]))))
    if car.get("price_max") is not None:
        params.append(("max_supplied_price", str(int(car["price_max"]))))
    return params


# ---------------------------------------------------------------------------
# BMW advert -> standard listing
# ---------------------------------------------------------------------------

def dealer_town(dealer: str | None) -> tuple[float, float] | None:
    """Location of the town at the end of a dealer name ("Sytner Milton Keynes")."""
    words = (dealer or "").split()
    for count in (3, 2, 1):
        if len(words) >= count:
            location = place_location(" ".join(words[-count:]))
            if location:
                return location
    return None


def advert_to_row(v: dict[str, Any], home: tuple[float, float] | None, site_body: str | None) -> dict[str, Any] | None:
    ident = v.get("identification") or {}
    reg = normalise_plate(ident.get("registration") or (v.get("registration") or {}).get("registration"))
    if not reg:
        return None
    title = str(v.get("title") or "").strip()
    derivative = str(v.get("derivative") or "").strip() or None
    model = re.sub(r"^BMW\s+", "", title) or None
    first_reg = str((v.get("registration") or {}).get("date") or "")[:10] or None
    price = _int((v.get("cash_price") or {}).get("value"))
    photo_count = _int((v.get("media") or {}).get("total")) or len((v.get("media") or {}).get("items") or [])
    if photo_count > 1:
        photo_status, photo_reason = "photos", f"{photo_count} dealer images"
    elif photo_count == 1:
        photo_status, photo_reason = "awaiting", "Only one image; treating as stock/awaiting"
    else:
        photo_status, photo_reason = "awaiting", "No dealer images yet"
    dealer = str((v.get("retailer_site") or {}).get("name") or "").strip() or None
    year = _int(first_reg[:4]) if first_reg else None
    return standardise({
        "registration": reg,
        "make": "BMW",
        "model": model,
        "trim": derivative,
        "year": year,
        "first_registered": first_reg,
        "colour": v.get("colour") or v.get("exterior_colour"),
        "fuel": v.get("fuel"),
        "transmission": v.get("transmission"),
        "body_type": body_for(title, derivative, site_body),
        "seats": seats_for(title, derivative),
        "mileage": _int(v.get("mileage")),
        "price": price,
        "previous_price": None,
        "dealer": dealer,
        "location": dealer,
        "distance_miles": distance_miles(home, dealer_town(dealer)) if home and dealer else None,
        "url": f"{SITE_ROOT}/vehicle/{v.get('advert_id')}" if v.get("advert_id") else f"{SITE_ROOT}/result/",
        "photo_status": photo_status,
        "photo_count": photo_count,
        "photo_reason": photo_reason,
        "title": " ".join(str(p) for p in (year, title, derivative, reg) if p),
        "raw_text": json.dumps({k: v.get(k) for k in ("advert_id", "title", "derivative", "cash_price", "mileage",
                                                      "registration", "fuel", "transmission", "retailer_site")},
                               ensure_ascii=False),
        "source": SOURCE_KEY,
        "status": "active",
        "last_seen": now_iso(),
    })


def is_wanted(row: dict[str, Any], car: dict[str, Any], derivative: str | None) -> bool:
    if not model_matches(car.get("model"), row.get("model"), derivative):
        return False
    wanted_trim = str(car.get("trim") or "").strip().lower()
    if wanted_trim and wanted_trim not in str(derivative or "").lower():
        return False
    if not fuel_matches(str(car.get("fuel") or "Any"), str(row.get("fuel") or "")):
        return False
    if not transmission_matches(str(car.get("transmission") or "Any"), str(row.get("transmission") or "")):
        return False
    price, mileage, year = row.get("price"), row.get("mileage"), row.get("year")
    if price is None:
        return False  # finance-only listing
    if car.get("price_min") is not None and price < int(car["price_min"]):
        return False
    if car.get("price_max") is not None and price > int(car["price_max"]):
        return False
    if mileage is not None and car.get("mileage_max") is not None and mileage > int(car["mileage_max"]):
        return False
    if year is not None and car.get("year_min") is not None and year < int(car["year_min"]):
        return False
    return body_and_seats_match(car, row.get("body_type"), row.get("seats"))


def extract_rows(data: dict[str, Any], car: dict[str, Any], home: tuple[float, float] | None
                 ) -> tuple[list[dict[str, Any]], set[str], int]:
    rows: list[dict[str, Any]] = []
    raw_regs: set[str] = set()
    adverts = data.get("results") or []
    site_body = BODY_VALUES.get(str(car.get("body_type") or "Any"))
    for v in adverts:
        if v.get("has_sold"):
            continue
        row = advert_to_row(v, home, site_body)
        if not row:
            continue
        raw_regs.add(row["registration"])
        if is_wanted(row, car, row.get("trim")):
            rows.append(row)
    return rows, raw_regs, len(adverts)


def search(car: dict[str, Any], settings: dict[str, Any], timings: dict[str, Any] | None = None) -> SearchResult:
    if car.get("model") and not series_for(car.get("model")):
        raise RuntimeError(f"BMW model '{car['model']}' not recognised: use e.g. '3 Series', 'X5', 'iX3' or 'M3'")
    session = open_session()
    params = build_params(car)
    home = postcode_location(settings.get("home_postcode"))
    result = SearchResult(search_url=f"{SITE_ROOT}/result/?" + "&".join(f"{k}={v}" for k, v in params))
    seen: set[str] = set()
    pages_log: list[dict[str, Any]] = []
    for page in range(1, MAX_PAGES + 1):
        started = time.perf_counter()
        data = _get(session, params + [("size", str(PAGE_SIZE)), ("page", str(page))])
        fetch_seconds = time.perf_counter() - started
        if timings is not None:
            timings["search_fetch_seconds"] = round(float(timings.get("search_fetch_seconds") or 0) + fetch_seconds, 3)
        rows, raw_regs, count = extract_rows(data, car, home)
        result.raw_regs.update(raw_regs)
        for row in rows:
            if row["registration"] not in seen:
                seen.add(row["registration"])
                result.rows.append(row)
        info = data.get("pagination") or {}
        pages_log.append({"search": car.get("name"), "page": page, "fetch_seconds": round(fetch_seconds, 3),
                          "vehicle_objects": count, "rows": len(rows), "total": info.get("items")})
        if count < PAGE_SIZE or page >= int(info.get("total") or 1):
            break
    if timings is not None:
        timings.setdefault("pages", []).extend(pages_log)
    result.debug_text = "\n".join(json.dumps(p) for p in pages_log)
    return result
