"""Kia approved-used source module.

Searches https://used-uk.kia.com (Kia UK's approved-used site), which runs on
a dealer search platform ("HyperSearch", ``hsearchapi.cogplatform.co.uk``):

* a search is a POST of the site's own configuration string plus a query
  string such as ``?franchiseApproved=1&manufacturer=KIA&model=SORENTO``;
* only listed values are accepted for price / mileage / year, so CarFinder
  sends model, fuel, gearbox and body type, then applies the exact limits
  itself;
* the results do not include the registration, so it is read once from each
  car's advert page and cached in ``data/cache/kia_regs.json``;
* the dealer's town is given (not its postcode), so distance is the straight
  line from the person's postcode to that town.
"""
from __future__ import annotations

import json
import re
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote_plus

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

SOURCE_KEY = "kia"
SOURCE_NAME = "Kia Approved Used"
MAKES = ("Kia",)

SITE_ROOT = "https://used-uk.kia.com"
API_ROOT = "https://hsearchapi.cogplatform.co.uk/api/v1"
PAGE_SIZE = 48
MAX_PAGES = 15
REG_CACHE_PATH = Path(__file__).resolve().parent.parent.parent / "data" / "cache" / "kia_regs.json"

# Search configuration sent by used-uk.kia.com's own search page (Sept 2026).
# "w:461" is Kia UK's website id on the platform.
SITE_CONFIG = (
    "w:461|d:|x:|t:All|bc:|wi:461|url:/car-search|fc:24,5,0,1,0,1,0,Approved Used,,,0,,,"
    "All+Kia+Approved+Used+Cars+come+with+an+unrivalled+peace-of-mind+package+as+standard%2c+designed+to+provide+you"
    "+with+as+much+reassurance+as+buying+a+brand+new+car.+Only+Kia+offer+an+industry+leading+Big+7+Year+Warranty+on"
    "+all+Approved+Used+Cars+from+the+day+you+drive+away.,Only KIA Approved Used,,0,,0,,approved,0,1,1,0,0;"
    "1,1,0,2,0,0,0,Dealership,,,0,,,,,,0,,0,,,0,,0,0,0;2,0,0,3,0,1,0,Manufacturer,,,0,,,,,,0,,0,,,0,2,1,0,0;"
    "3,0,1,4,0,1,0,Model,,,0,,,,,,0,,0,,,0,3,1,0,0;27,0,1,5,0,1,0,Grade,,,0,,,,,,0,,0,,,0,4,1,0,0;"
    "4,1,1,6,0,1,0,Version,,,0,,,,,,0,,0,,,0,5,1,0,0;10,3,0,7,500,0,0,Price,£,,0,,,,,,0,,0,,,0,,0,0,0;"
    "10,2,0,8,500,0,0,Price,£,,0,,,,,,0,,0,,,0,,0,0,0;6,0,0,9,0,1,0,Transmission,,,0,,,,,,0,,0,,,0,6,1,0,0;"
    "7,0,0,10,0,1,0,Fuel Type,,,0,,,,,,0,,0,,,0,7,1,0,0;5,0,0,11,0,1,0,Body Type,,,0,,,,,,0,,0,,,0,8,1,0,0;"
    "8,0,0,12,0,1,0,Colour,,,0,,,,,,0,,0,,,0,9,1,0,0;9,0,1,13,0,1,0,Kia Colour,,,0,,,,,,0,,0,,,0,10,1,0,0;"
    "14,2,0,14,50,0,0,Distance,, miles,0,,,,,,0,,0,,,0,,0,0,0;22,3,0,15,1,0,0,Year,,,0,,,,,,0,,0,,,0,,0,0,0;"
    "22,2,0,16,1,0,0,Year,,,0,,,,,,0,,0,,,0,,0,0,0;13,3,0,17,10000,0,0,Mileage,, miles,0,,,,,,0,,0,,,0,,0,0,0;"
    "13,2,0,18,10000,0,0,Mileage,, miles,0,,,,,,0,,0,,,0,,0,0,0;"
    "34,0,0,19,0,0,0,Specification,,,0,,,,,,1,,0,,,1,,1,0,0|r:24|s:Distance|o:Asc|cw:-1,-1,-1,,|"
    "of:False,0,False,False,0,,,False,False,|ae:0|bs:0|sv:False|vru:|cminr:0|"
    "vf:Auto_ID,Stock_ID,DealerID,IsNewCar,FinanceLowRegularPayment,Manufacturer,Model,Version,Mileage,FuelType,"
    "Transmission,Price,RegYear,Images,Co2,MPG,TaxCost,ManagersSpecial,RegDate,IsExDemoCar,IsReserved,"
    "FranchiseApproved,HasOffer,WLTPCo2,WLTPMPGCombined¬FinanceLowRegularPaymentType,FinanceLowRegularPaymentAPR,"
    "BatteryCapacity,BatteryRange,WLTPMPGExtraHigh,WLTPMPGHigh,WLTPMPGMedium,WLTPMPGLow,WLTPECMileskWhCombined,"
    "ServiceHistory¬¬Aspiration¬¬|ato:0|atr:0|atfp:0|atf:0|mi:0|ctsf:1|atot:|atrt:|atfpt:|atft:|"
    "hrv:0|hsv:0|tgc:|cmsid:|ufsl:0"
)

# Model names on the site (Sept 2026); the site's own list is read on each search too.
KNOWN_MODELS = ["Ceed", "Ceed Sportswagon", "EV3", "EV4", "EV6", "EV9", "K4", "Niro", "Picanto",
                "ProCeed", "Sorento", "Sportage", "Stonic", "XCeed"]
FUEL_VALUES = {"Petrol": "Petrol", "Diesel": "Diesel", "Hybrid": "Hybrid", "Electric": "Electric"}
BODY_VALUES = {"Estate": "Estate", "SUV": "SUV", "Hatchback": "Hatchback", "Saloon": "Saloon"}
SEVEN_SEATERS = ("sorento", "ev9")

HEADERS = {
    "Accept": "application/json, text/plain, */*",
    "Content-Type": "application/json",
    "Origin": SITE_ROOT,
    "Referer": SITE_ROOT + "/used-cars/",
    "User-Agent": "Mozilla/5.0 (CarFinder)",
}


def _squash(text: Any) -> str:
    return re.sub(r"[^a-z0-9]+", "", str(text or "").lower())


def _int(value: Any) -> int | None:
    try:
        return int(round(float(value))) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------

def _post(query: str, timeout: int = 30) -> dict[str, Any]:
    body = {
        "config": SITE_CONFIG,
        "lastQuery": query,
        "location": {"postcode": "", "longitude": None, "latitude": None, "hasCoordinates": False},
    }
    response = requests.post(f"{API_ROOT}/search/search", data=json.dumps(body), headers=HEADERS, timeout=timeout)
    response.raise_for_status()
    return response.json()


def _get_page(path: str, timeout: int = 20) -> str:
    response = requests.get(SITE_ROOT + path, headers={"User-Agent": HEADERS["User-Agent"]}, timeout=timeout)
    response.raise_for_status()
    return response.text


# ---------------------------------------------------------------------------
# Registration from the advert page (cached)
# ---------------------------------------------------------------------------

def _load_reg_cache() -> dict[str, dict[str, Any]]:
    try:
        data = json.loads(REG_CACHE_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save_reg_cache(cache: dict[str, dict[str, Any]]) -> None:
    REG_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    REG_CACHE_PATH.write_text(json.dumps(cache), encoding="utf-8")


def details_from_advert(html: str) -> dict[str, Any]:
    """Registration, colour, body type and seats from a Kia advert page."""
    out: dict[str, Any] = {}
    match = re.search(r'name="DealerEmail_Registration[^"]*"[^>]*value="([A-Z0-9 ]+)"', html) or \
        re.search(r'value="([A-Z0-9 ]+)"[^>]*id="DealerEmail_Registration', html)
    spec = dict(
        (key.strip(), re.sub(r"<[^>]+>", " ", value).strip())
        for key, value in re.findall(
            r'used-short-spec__item-key">\s*([^<]+?)\s*</span>(.*?)</li>', html, flags=re.S)
    )
    spec = {k: re.sub(r"\s+", " ", v) for k, v in spec.items()}
    out["registration"] = normalise_plate(match.group(1) if match else spec.get("Registration"))
    out["colour"] = spec.get("Colour") or None
    out["body"] = spec.get("Body Type") or None
    seats = re.search(r"\b([5-9])\s*seats?\b", html, flags=re.I)
    out["seats"] = int(seats.group(1)) if seats else None
    return out


def advert_details(vehicle: dict[str, Any], cache: dict[str, dict[str, Any]], timings: dict[str, Any] | None) -> dict[str, Any] | None:
    key = str(vehicle.get("stock_ID_Str") or vehicle.get("stock_ID") or "")
    if key and key in cache:
        return cache[key]
    path = vehicle.get("cogUsedDetailsUrl")
    if not path:
        return None
    started = time.perf_counter()
    if timings is not None:
        timings["detail_fetch_count"] = int(timings.get("detail_fetch_count") or 0) + 1
    try:
        details = details_from_advert(_get_page(path))
    except requests.RequestException:
        if timings is not None:
            timings["detail_fetch_errors"] = int(timings.get("detail_fetch_errors") or 0) + 1
        return None
    finally:
        if timings is not None:
            timings["detail_fetch_seconds"] = round(float(timings.get("detail_fetch_seconds") or 0) + time.perf_counter() - started, 3)
    if key and details.get("registration"):
        cache[key] = details
    return details


# ---------------------------------------------------------------------------
# Car search -> query string
# ---------------------------------------------------------------------------

def models_for(model: str, known: list[str]) -> list[str]:
    """Site model names starting with what the user typed ("Ceed" -> Ceed, Ceed Sportswagon)."""
    wanted = _squash(model)
    if not wanted:
        return []
    return [name for name in known if _squash(name).startswith(wanted)]


def build_query(car: dict[str, Any], model_name: str | None, page: int) -> str:
    parts = ["franchiseApproved=1", "manufacturer=KIA"]
    if model_name:
        parts.append("model=" + quote_plus(model_name.upper()))
    fuel = FUEL_VALUES.get(str(car.get("fuel") or "Any"))
    if fuel:
        parts.append(f"fuelType={fuel}")
    gear = {"Manual": "Manual", "Automatic": "Automatic"}.get(str(car.get("transmission") or "Any"))
    if gear:
        parts.append(f"transmissionType={gear}")
    body = BODY_VALUES.get(str(car.get("body_type") or "Any"))
    if body:
        parts.append(f"bodyType={body}")
    parts.extend(["sort=price", "order=asc", f"results={PAGE_SIZE}", f"page={page}"])
    return "?" + "&".join(parts)


# ---------------------------------------------------------------------------
# Kia vehicle -> standard listing
# ---------------------------------------------------------------------------

def body_for(model: str | None, site_body: str | None) -> str | None:
    body = (site_body or "").strip()
    if body in {"Estate", "SUV", "Hatchback", "Saloon", "MPV"}:
        return body
    name = (model or "").lower()
    if "sportswagon" in name:
        return "Estate"
    if any(m in name for m in ("sportage", "sorento", "niro", "stonic", "xceed", "ev3", "ev5", "ev9")):
        return "SUV"
    if any(m in name for m in ("picanto", "ceed", "rio", "k4")):
        return "Hatchback"
    return None


def vehicle_to_row(v: dict[str, Any], details: dict[str, Any], home: tuple[float, float] | None) -> dict[str, Any] | None:
    reg = normalise_plate(details.get("registration"))
    if not reg:
        return None
    model = str(v.get("model") or "").strip() or None
    trim = str(v.get("version") or "").strip() or None
    first_reg = str(v.get("regDate") or "")[:10] or None
    images = v.get("images") or []
    photo_count = len(images)
    if photo_count > 1:
        photo_status, photo_reason = "photos", f"{photo_count} dealer images"
    elif photo_count == 1:
        photo_status, photo_reason = "awaiting", "Only one image; treating as stock/awaiting"
    else:
        photo_status, photo_reason = "awaiting", "No dealer images yet"
    town = str(v.get("dealerTownCity") or "").strip() or None
    seats = details.get("seats")
    if seats is None and any(m in (model or "").lower() for m in SEVEN_SEATERS):
        seats = 7
    year = _int(v.get("regYear"))
    title = " ".join(str(p) for p in (year, "Kia", model, trim, reg) if p)
    return standardise({
        "registration": reg,
        "make": "Kia",
        "model": model,
        "trim": trim,
        "year": year,
        "first_registered": first_reg,
        "colour": details.get("colour"),
        "fuel": v.get("fuelType"),
        "transmission": v.get("transmission") or v.get("transmissionType"),
        "body_type": body_for(model, details.get("body")),
        "seats": seats,
        "mileage": _int(v.get("mileage")),
        "price": _int(v.get("price")),
        "previous_price": None,
        "dealer": str(v.get("dealerName") or "").strip() or None,
        "location": town,
        "distance_miles": distance_miles(home, place_location(town)) if town else None,
        "url": SITE_ROOT + str(v.get("cogUsedDetailsUrl") or "/used-cars/"),
        "photo_status": photo_status,
        "photo_count": photo_count,
        "photo_reason": photo_reason,
        "title": title,
        "raw_text": json.dumps({k: v.get(k) for k in ("stock_ID_Str", "dealerName", "dealerTownCity", "model", "version",
                                                      "mileage", "fuelType", "transmission", "price", "regDate",
                                                      "franchiseApproved", "cogUsedDetailsUrl")}, ensure_ascii=False),
        "source": SOURCE_KEY,
        "status": "active",
        "last_seen": now_iso(),
    })


def quick_wanted(v: dict[str, Any], car: dict[str, Any]) -> bool:
    """Checks that need no advert page, so pages are only fetched for likely matches."""
    if v.get("franchiseApproved") is False or v.get("isNewCar"):
        return False
    wanted_model = _squash(car.get("model"))
    if wanted_model and not _squash(v.get("model")).startswith(wanted_model):
        return False
    wanted_trim = str(car.get("trim") or "").strip().lower()
    if wanted_trim and wanted_trim not in str(v.get("version") or "").lower():
        return False
    if not fuel_matches(str(car.get("fuel") or "Any"), str(v.get("fuelType") or "")):
        return False
    if not transmission_matches(str(car.get("transmission") or "Any"), str(v.get("transmission") or "")):
        return False
    price, mileage, year = _int(v.get("price")), _int(v.get("mileage")), _int(v.get("regYear"))
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


def search(car: dict[str, Any], settings: dict[str, Any], timings: dict[str, Any] | None = None) -> SearchResult:
    model_names: list[str | None] = [None]
    if car.get("model") and not car.get("search_url"):
        names = models_for(str(car["model"]), KNOWN_MODELS)
        if not names:
            first = _post(build_query({}, None, 1))
            site_models = next((f for f in first.get("filters") or [] if f.get("name") == "model"), {})
            names = models_for(str(car["model"]), [str(i.get("value")) for i in site_models.get("values") or []])
        if not names:
            raise RuntimeError(f"Kia has no model matching '{car['model']}' in approved-used stock right now")
        model_names = list(names)

    home = postcode_location(settings.get("home_postcode"))
    cache = _load_reg_cache()
    result = SearchResult(search_url=SITE_ROOT + "/used-cars/" + build_query(car, model_names[0], 1))
    seen: set[str] = set()
    pages_log: list[dict[str, Any]] = []
    try:
        for name in model_names:
            for page in range(1, MAX_PAGES + 1):
                started = time.perf_counter()
                data = _post(build_query(car, name, page))
                fetch_seconds = time.perf_counter() - started
                if timings is not None:
                    timings["search_fetch_seconds"] = round(float(timings.get("search_fetch_seconds") or 0) + fetch_seconds, 3)
                vehicles = data.get("vehicles") or []
                rows = 0
                for v in vehicles:
                    known = cache.get(str(v.get("stock_ID_Str") or ""))
                    if known and known.get("registration"):
                        result.raw_regs.add(known["registration"])
                    if not quick_wanted(v, car):
                        continue
                    details = advert_details(v, cache, timings)
                    if not details:
                        continue
                    row = vehicle_to_row(v, details, home)
                    if not row:
                        continue
                    result.raw_regs.add(row["registration"])
                    if row["registration"] in seen:
                        continue
                    if body_and_seats_match(car, row.get("body_type"), row.get("seats")):
                        seen.add(row["registration"])
                        result.rows.append(row)
                        rows += 1
                pages_log.append({"search": car.get("name"), "model": name, "page": page,
                                  "fetch_seconds": round(fetch_seconds, 3), "vehicle_objects": len(vehicles),
                                  "rows": rows, "total": data.get("totalResults")})
                if len(vehicles) < PAGE_SIZE or page >= int(data.get("totalPages") or 1):
                    break
    finally:
        _save_reg_cache(cache)
    if timings is not None:
        timings.setdefault("pages", []).extend(pages_log)
    result.debug_text = "\n".join(json.dumps(p) for p in pages_log)
    return result
