"""Honda and Lexus approved-used source (Codeweavers/Modix rendered stock pages).

Both UK approved-used sites render the important vehicle data into the results
HTML, including registration, first-registration date, price, mileage and
retailer address.  CarFinder reads those rendered cards rather than depending
on the site's private UI JavaScript.
"""
from __future__ import annotations

import datetime as dt
import json
import random
import re
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode

import requests

try:
    from curl_cffi import requests as cffi_requests
except ImportError:  # pragma: no cover
    cffi_requests = None

from app.db import now_iso
from app.geo import distance_miles, postcode_location
from app.sources import SearchResult, body_and_seats_match, classify_body_type, fuel_matches, normalise_plate, standardise, transmission_matches
from app.sources.generic_used import POSTCODE, html_vehicle_blocks, int_from

SOURCE_KEY = "codeweavers"
SOURCE_NAME = "Honda / Lexus approved used"
MAKES = ("Honda", "Lexus")

@dataclass(frozen=True)
class Brand:
    make: str
    root: str
    results_path: str
    known_models: tuple[str, ...]

BRANDS = {
    "Honda": Brand("Honda", "https://usedcars.honda.co.uk", "/en/used-cars/approved-cars/all-brands/all-models",
                   ("CR-V", "HR-V", "ZR-V", "Civic", "Civic Type R", "Jazz", "Crosstar", "Accord", "Prelude", "Honda e", "e:Ny1", "NSX", "CR-Z")),
    "Lexus": Brand("Lexus", "https://usedcars.lexus.co.uk", "/en/used-cars/approved-cars/all-brands/all-models",
                   ("LBX", "UX", "NX", "RX", "RZ", "ES", "LS", "LC", "RC", "IS", "CT", "GS")),
}

MODEL_BODIES = {
    "Honda": {"crv":"SUV","hrv":"SUV","zrv":"SUV","eny1":"SUV","jazz":"Hatchback","crosstar":"Hatchback","civic":"Hatchback","accord":"Saloon"},
    "Lexus": {"lbx":"SUV","ux":"SUV","nx":"SUV","rx":"SUV","rz":"SUV","es":"Saloon","ls":"Saloon","is":"Saloon","gs":"Saloon","ct":"Hatchback"},
}
SEVEN_SEATERS = {"Honda": set(), "Lexus": {"rx l", "rxl"}}
PAGE_DELAY = 1.0
PAGE_JITTER = 0.5
MAX_PAGES = 160
IMPERSONATE = "chrome"
HEADERS = {"Accept":"text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8", "Accept-Language":"en-GB,en;q=0.9",
           "User-Agent":"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
_sessions: dict[str, Any] = {}


def _plain(v: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(v or "").lower())


def brand_for(make: Any) -> Brand | None:
    wanted = _plain(make)
    return next((b for b in BRANDS.values() if _plain(b.make) == wanted), None)


def model_matches(wanted: str, have: str | None) -> bool:
    w, h = _plain(wanted), _plain(have)
    return not w or h.startswith(w) or w in h


def _session(brand: Brand) -> Any:
    if brand.make not in _sessions:
        if cffi_requests is not None:
            _sessions[brand.make] = cffi_requests.Session(impersonate=IMPERSONATE)
        else:
            s = requests.Session(); s.headers.update(HEADERS); _sessions[brand.make] = s
    return _sessions[brand.make]


def page_url(brand: Brand, page: int) -> str:
    q = {"page": page}
    if brand.make == "Honda":
        q["warrantyProgram"] = 22
    return f"{brand.root}{brand.results_path}?{urlencode(q)}"


def _get(brand: Brand, page: int) -> str:
    headers = {"Accept-Language": HEADERS["Accept-Language"]} if cffi_requests is not None else None
    r = _session(brand).get(page_url(brand, page), headers=headers, timeout=60)
    r.raise_for_status()
    return r.text


def _model_from_title(title: str, brand: Brand) -> str | None:
    low = _plain(title)
    matches = [m for m in brand.known_models if _plain(m) in low]
    return max(matches, key=lambda x: len(_plain(x))) if matches else None


def _trim_from_title(title: str, make: str, model: str | None) -> str | None:
    text = re.sub(r"\s+", " ", title or "").strip()
    text = re.sub(rf"^{re.escape(make)}\s+", "", text, flags=re.I)
    if model:
        text = re.sub(rf"^{re.escape(model)}\s+", "", text, flags=re.I)
    return text or None


def _date(text: str) -> str | None:
    m = re.search(r"First registration date\s+(\d{1,2}/\d{1,2}/\d{4})", text, re.I)
    if not m: return None
    try: return dt.datetime.strptime(m.group(1), "%d/%m/%Y").date().isoformat()
    except ValueError: return None


def _label(text: str, label: str, values: str) -> str | None:
    m = re.search(rf"{re.escape(label)}\s+({values})(?=\s|$)", text, re.I)
    return m.group(1).strip() if m else None


def _body(make: str, model: str | None, title: str) -> str | None:
    key = _plain(model)
    if any(w in _plain(title) for w in ("coupe", "convertible", "roadster")):
        return None
    for token, body in MODEL_BODIES[make].items():
        if key.startswith(token): return body
    return classify_body_type(title)


def parse_page(html: str, brand: Brand) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for block in html_vehicle_blocks(html, brand.root):
        text, title = block["text"], block["title"]
        reg = normalise_plate(block["registration"])
        if not reg: continue
        model = _model_from_title(title, brand)
        price_m = re.search(r"Vehicle price\s*£\s*([\d,]+)", text, re.I) or re.search(r"\b£\s*([\d,]+)", text)
        mileage_m = re.search(r"Mileage\s*([\d,]+)\s*(?:miles|mls)", text, re.I)
        colour_m = re.search(r"Exterior colour\s+(.+?)(?=\s+Interior\b|\s+Mileage\b)", text, re.I)
        fuel_m = re.search(r"Fuel Type\s+(.+?)(?=\s+Transmission\b|\s+Doors\b)", text, re.I)
        gear_m = re.search(r"Transmission\s+(Manual|Automatic|CVT|Semi[- ]Automatic)", text, re.I)
        date = _date(text)
        postcode_m = POSTCODE.search(text)
        images_m = re.search(r"(\d+)\s+Images\b", text, re.I)
        rows.append({"registration":reg,"make":brand.make,"model":model,"trim":_trim_from_title(title, brand.make, model),
                     "year": int(date[:4]) if date else None,"first_registered":date,"price":int_from(price_m.group(1)) if price_m else None,
                     "mileage":int_from(mileage_m.group(1)) if mileage_m else None,"colour":colour_m.group(1).strip() if colour_m else None,
                     "fuel":fuel_m.group(1).strip() if fuel_m else None,"transmission":gear_m.group(1).strip() if gear_m else None,
                     "body_type":_body(brand.make, model, title),"seats":7 if any(k in _plain(title) for k in SEVEN_SEATERS[brand.make]) else None,
                     "postcode":postcode_m.group(1).upper() if postcode_m else None,"dealer":None,"location":None,
                     "photo_count":int(images_m.group(1)) if images_m else None,"url":block.get("url"),"title":title,"raw_text":text})
    return rows


def wanted(c: dict[str, Any], car: dict[str, Any]) -> bool:
    if car.get("model") and not model_matches(str(car["model"]), c.get("model") or c.get("title")): return False
    if str(car.get("body_type") or "Any") != "Any" and c.get("body_type") is None: return False
    if not fuel_matches(str(car.get("fuel") or "Any"), str(c.get("fuel") or "")): return False
    if not transmission_matches(str(car.get("transmission") or "Any"), str(c.get("transmission") or "")): return False
    for key, test in (("price_min", lambda x,y:x<y),("price_max",lambda x,y:x>y),("mileage_max",lambda x,y:x>y),("year_min",lambda x,y:x<y)):
        v=c.get("price" if key.startswith("price") else "mileage" if "mileage" in key else "year")
        if v is not None and car.get(key) is not None and test(int(v), int(car[key])): return False
    return True


def to_row(c: dict[str, Any], home: tuple[float,float] | None) -> dict[str, Any]:
    count=c.get("photo_count")
    ps="photos" if count and count>1 else "awaiting" if count is not None else "unknown"
    where=postcode_location(c.get("postcode")) if c.get("postcode") else None
    return standardise({**{k:c.get(k) for k in ("registration","make","model","trim","year","first_registered","colour","fuel","transmission","body_type","seats","mileage","price","dealer","location","url","title")},
        "previous_price":None,"distance_miles":distance_miles(home,where) if where else None,"photo_status":ps,"photo_count":count,
        "photo_reason":f"{count} dealer images" if count else None,"raw_text":json.dumps({"text":c.get("raw_text"),"postcode":c.get("postcode")},ensure_ascii=False),
        "source":SOURCE_KEY,"status":"active","last_seen":now_iso()})


def search(car: dict[str, Any], settings: dict[str, Any], timings: dict[str, Any] | None = None) -> SearchResult:
    brand=brand_for(car.get("make"))
    if not brand: raise RuntimeError(f"Honda/Lexus source does not search make '{car.get('make')}'")
    home=postcode_location(settings.get("home_postcode")); result=SearchResult(search_url=page_url(brand,1)); seen=set(); pages=[]
    price_max=int(car["price_max"]) if car.get("price_max") is not None else None
    for page in range(1,MAX_PAGES+1):
        if page>1: time.sleep(PAGE_DELAY+random.uniform(0,PAGE_JITTER))
        started=time.perf_counter(); html=_get(brand,page); secs=time.perf_counter()-started
        cars=parse_page(html,brand); new=[c for c in cars if c["registration"] not in seen]
        added=0
        for c in new:
            seen.add(c["registration"]); result.raw_regs.add(c["registration"])
            if wanted(c,car) and body_and_seats_match(car,c.get("body_type"),c.get("seats")):
                result.rows.append(to_row(c,home)); added+=1
        pages.append({"search":car.get("name"),"page":page,"fetch_seconds":round(secs,3),"vehicle_objects":len(cars),"rows":added})
        if not new or not cars: break
        if price_max is not None and cars and all((c.get("price") or 0)>price_max for c in cars): break
    if timings is not None:
        timings["search_fetch_seconds"]=round(float(timings.get("search_fetch_seconds") or 0)+sum(p["fetch_seconds"] for p in pages),3); timings.setdefault("pages",[]).extend(pages)
    result.debug_text="\n".join(json.dumps(p) for p in pages)
    return result
