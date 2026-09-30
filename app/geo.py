"""Postcode locations and distances.

Some sites (e.g. Skoda) give each dealer's latitude/longitude but not a
distance from you, so CarFinder works out the straight-line distance itself.
Postcodes are looked up once with the free postcodes.io service and cached in
``data/cache/postcodes.json``.
"""
from __future__ import annotations

import json
import math
import re
from pathlib import Path
from typing import Any

import requests

APP_ROOT = Path(__file__).resolve().parent.parent
CACHE_PATH = APP_ROOT / "data" / "cache" / "postcodes.json"
LOOKUP_URL = "https://api.postcodes.io/postcodes/{postcode}"


def clean_postcode(postcode: str | None) -> str:
    return re.sub(r"\s+", "", (postcode or "").upper())


def _load_cache(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def postcode_location(postcode: str | None, path: Path | None = None) -> tuple[float, float] | None:
    """(latitude, longitude) for a UK postcode, or None if unknown."""
    path = path or CACHE_PATH
    key = clean_postcode(postcode)
    if not key:
        return None
    cache = _load_cache(path)
    if key in cache:
        value = cache[key]
        return (float(value[0]), float(value[1])) if value else None
    try:
        response = requests.get(LOOKUP_URL.format(postcode=key), timeout=10)
        result = response.json().get("result") if response.status_code == 200 else None
    except (requests.RequestException, ValueError):
        return None  # don't cache network failures
    location = (float(result["latitude"]), float(result["longitude"])) if result else None
    cache[key] = list(location) if location else None
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache), encoding="utf-8")
    return location


def distance_miles(a: tuple[float, float] | None, b: tuple[float, float] | None) -> int | None:
    """Straight-line distance in miles between two (lat, lon) points."""
    if not a or not b:
        return None
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return int(round(3958.8 * 2 * math.asin(math.sqrt(h))))
