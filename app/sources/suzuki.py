"""Suzuki Approved Used source using the server-rendered UK stock pages."""
from __future__ import annotations

import json
import random
import re
import time
from typing import Any

import requests
try:
    from curl_cffi import requests as cffi_requests
except ImportError:  # pragma: no cover
    cffi_requests = None

from app.db import now_iso
from app.geo import distance_miles, place_location, postcode_location
from app.sources import SearchResult, body_and_seats_match, classify_body_type, fuel_matches, normalise_plate, standardise, transmission_matches
from app.sources.generic_used import html_vehicle_blocks, int_from

SOURCE_KEY = "suzuki"
SOURCE_NAME = "Suzuki Approved Used"
MAKES = ("Suzuki",)
SITE_ROOT = "https://ucl.suzuki.co.uk"
LIST_ROOT = SITE_ROOT + "/usedcars/suzuki"
ALT_ROOT = SITE_ROOT + "/cars/used/approved/suzuki"
MAX_PAGES = 80
PAGE_DELAY = 0.5
PAGE_JITTER = 0.4
IMPERSONATE = "chrome"
KNOWN_MODELS = ("e-vitara", "swift", "s-cross", "vitara", "across", "baleno", "celerio", "ignis", "jimny", "swace", "swift-sport")
_s = None


def _plain(v: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(v or "").lower())


def _slug(model: str) -> str:
    wanted = _plain(model)
    for slug in KNOWN_MODELS:
        if _plain(slug).startswith(wanted) or wanted.startswith(_plain(slug)):
            return slug
    return re.sub(r"[^a-z0-9]+", "-", model.lower()).strip("-")


def _http():
    global _s
    if _s is None:
        _s = cffi_requests.Session(impersonate=IMPERSONATE) if cffi_requests is not None else requests.Session()
    return _s


def _get(url: str) -> str:
    headers = {"Accept": "text/html,application/xhtml+xml", "Accept-Language": "en-GB,en;q=0.9"}
    r = _http().get(url, headers=headers, timeout=60)
    r.raise_for_status()
    return r.text


def _url(model: str | None, page: int, alt: bool = False) -> str:
    root = ALT_ROOT if alt else LIST_ROOT
    if model:
        root += "/" + _slug(model)
    if page > 1:
        root += f"/page/{page}"
    return root


def _extract(block: dict[str, Any]) -> dict[str, Any] | None:
    text = block.get("text") or ""
    reg = normalise_plate(block.get("registration"))
    if not reg:
        return None
    title = block.get("title") or ""
    # Suzuki pages usually present model/trim in the heading. Remove make and plate if repeated.
    clean_title = re.sub(r"\bSUZUKI\b", "", title, flags=re.I)
    clean_title = re.sub(rf"\b{re.escape(reg.replace(' ', ''))}\b", "", clean_title, flags=re.I).strip(" -|")
    model = None
    for name in ("S-Cross", "E Vitara", "E-Vitara", "Swift Sport", "Swift", "Vitara", "Across", "Baleno", "Celerio", "Ignis", "Jimny", "Swace"):
        if _plain(name) in _plain(clean_title):
            model = name.replace("E Vitara", "e Vitara").replace("E-Vitara", "e Vitara")
            break
    if not model and clean_title:
        model = clean_title.split(" ", 1)[0]
    trim = clean_title
    if model and _plain(trim).startswith(_plain(model)):
        # only cosmetic; retain the full heading if tokenisation is uncertain
        m = re.match(rf"^\s*{re.escape(model)}\s*(.*)$", clean_title, re.I)
        trim = (m.group(1).strip() if m else "") or None

    price = None
    m = re.search(r"£\s*([\d,]+)", text)
    if m:
        price = int(m.group(1).replace(",", ""))
    mileage = None
    m = re.search(r"([\d,]+)\s*(?:miles|mls)\b", text, re.I)
    if m:
        mileage = int(m.group(1).replace(",", ""))
    first = None
    for pat in (r"(?:First\s+registration|Registered|Registration\s+date)\s*[:\-]?\s*(\d{1,2}[/-]\d{1,2}[/-]\d{4})",
                r"(?:First\s+registration|Registered|Registration\s+date)\s*[:\-]?\s*(\d{4}-\d{2}-\d{2})"):
        m = re.search(pat, text, re.I)
        if m:
            raw = m.group(1)
            if re.fullmatch(r"\d{2}/\d{2}/\d{4}", raw):
                d, mo, y = raw.split("/"); first = f"{y}-{mo}-{d}"
            else:
                first = raw.replace("/", "-")
            break
    year = int_from(first[:4]) if first else None
    if year is None:
        m = re.search(r"\b(20[0-3]\d)\b", text)
        year = int(m.group(1)) if m else None

    fuel = next((x for x in ("Plug-in Hybrid", "Hybrid", "Electric", "Petrol", "Diesel") if re.search(rf"\b{re.escape(x)}\b", text, re.I)), None)
    transmission = next((x for x in ("Automatic", "Manual") if re.search(rf"\b{x}\b", text, re.I)), None)
    body = classify_body_type(text, model)
    if model and _plain(model) in {_plain(x) for x in ("Vitara", "S-Cross", "Across", "e Vitara")}: body = "SUV"
    if model and _plain(model) in {_plain(x) for x in ("Swift", "Swift Sport", "Baleno", "Celerio", "Ignis")}: body = "Hatchback"
    if model and _plain(model) == _plain("Swace"): body = "Estate"
    colour = None
    m = re.search(r"(?:Colour|Color)\s*[:\-]?\s*([A-Za-z][A-Za-z /-]{2,40})(?=\s+(?:Fuel|Transmission|Mileage|Registration|£)|$)", text, re.I)
    if m: colour = re.sub(r"\s+", " ", m.group(1)).strip()
    town = None
    m = re.search(r"(?:Dealer|Location)\s*[:\-]?\s*([^£]{2,80}?)(?=\s+(?:£|Mileage|Fuel|Transmission|Registration)|$)", text, re.I)
    if m: town = re.sub(r"\s+", " ", m.group(1)).strip()
    photos = len(re.findall(r"<img\b", block.get("html") or "", re.I)) or None
    commercial = bool(re.search(r"\bcommercial\b|\+\s*VAT\b", text, re.I))
    return {"registration": reg, "model": model, "trim": trim, "title": clean_title or title, "year": year,
            "first_registered": first, "price": price, "mileage": mileage, "fuel": fuel, "transmission": transmission,
            "colour": colour, "body_type": body, "seats": None, "dealer": None, "location": town,
            "photo_count": photos, "url": block.get("url") or LIST_ROOT, "commercial": commercial, "raw": text}


def _wanted(c: dict[str, Any], car: dict[str, Any]) -> bool:
    if c.get("commercial"): return False
    if car.get("model") and _plain(car["model"]) not in _plain(f"{c.get('model')} {c.get('title')}"): return False
    if not fuel_matches(str(car.get("fuel") or "Any"), str(c.get("fuel") or "")): return False
    if not transmission_matches(str(car.get("transmission") or "Any"), str(c.get("transmission") or "")): return False
    checks = (("price", "price_min", lambda a,b:a<b), ("price", "price_max", lambda a,b:a>b),
              ("mileage", "mileage_max", lambda a,b:a>b), ("year", "year_min", lambda a,b:a<b))
    return not any(c.get(f) is not None and car.get(k) is not None and op(int(c[f]), int(car[k])) for f,k,op in checks)


def _row(c: dict[str, Any], home):
    loc = place_location(c.get("location")) if c.get("location") else None
    n = c.get("photo_count")
    return standardise({"registration": c["registration"], "make": "Suzuki", "model": c.get("model"), "trim": c.get("trim"),
        "year": c.get("year"), "first_registered": c.get("first_registered"), "colour": c.get("colour"), "fuel": c.get("fuel"),
        "transmission": c.get("transmission"), "body_type": c.get("body_type"), "seats": c.get("seats"), "mileage": c.get("mileage"),
        "price": c.get("price"), "previous_price": None, "dealer": c.get("dealer"), "location": c.get("location"),
        "distance_miles": distance_miles(home, loc) if loc else None, "url": c.get("url"),
        "photo_status": "photos" if n and n > 1 else "awaiting" if n is not None else "unknown", "photo_count": n,
        "photo_reason": f"{n} page images" if n else None, "title": c.get("title"), "raw_text": json.dumps({"text": c.get("raw")}, ensure_ascii=False),
        "source": SOURCE_KEY, "status": "active", "last_seen": now_iso()})


def search(car: dict[str, Any], settings: dict[str, Any], timings: dict[str, Any] | None = None) -> SearchResult:
    if _plain(car.get("make")) != "suzuki": raise RuntimeError(f"Suzuki source does not search make '{car.get('make')}'")
    result = SearchResult(search_url=_url(str(car.get("model") or "") or None, 1)); home = postcode_location(settings.get("home_postcode")); seen=set(); logs=[]; alt=False
    for page in range(1, MAX_PAGES + 1):
        if page > 1: time.sleep(PAGE_DELAY + random.uniform(0, PAGE_JITTER))
        url = _url(str(car.get("model") or "") or None, page, alt=alt); started=time.perf_counter(); html=_get(url); secs=time.perf_counter()-started
        blocks=html_vehicle_blocks(html, SITE_ROOT)
        if page == 1 and not blocks and not alt:
            alt=True; url=_url(str(car.get("model") or "") or None, page, alt=True); started=time.perf_counter(); html=_get(url); secs+=time.perf_counter()-started; blocks=html_vehicle_blocks(html, SITE_ROOT)
            result.search_url=url
        records=[c for b in blocks if (c:=_extract(b))]
        new=[c for c in records if c["registration"] not in seen]; added=0
        for c in new:
            seen.add(c["registration"]); result.raw_regs.add(c["registration"])
            if _wanted(c,car) and body_and_seats_match(car,c.get("body_type"),c.get("seats")): result.rows.append(_row(c,home)); added+=1
        logs.append({"search":car.get("name"),"page":page,"fetch_seconds":round(secs,3),"vehicle_objects":len(records),"rows":added})
        if not new: break
    if timings is not None:
        timings["search_fetch_seconds"] = round(float(timings.get("search_fetch_seconds") or 0) + sum(x["fetch_seconds"] for x in logs), 3); timings.setdefault("pages", []).extend(logs)
    result.debug_text="\n".join(json.dumps(x) for x in logs); return result
