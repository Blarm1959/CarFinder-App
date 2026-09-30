"""SEAT approved-used source module (VTP stock API, see ``app/sources/vtp.py``)."""
from __future__ import annotations

from typing import Any

from app.sources import SearchResult
from app.sources import vtp

SOURCE_KEY = "seat"
SOURCE_NAME = "SEAT Approved Used"
MAKES = ("SEAT",)

BRAND = vtp.Brand(
    key=SOURCE_KEY,
    make="SEAT",
    api_base="https://vtpapi.seat.com/restapi/v1/stukgwb",
    pattern="seatwebfe",
    detail_url="https://www.seat.co.uk/new-cars/new-car-stock-locator/w/offer/used/{key}",
    # Model codes seen on the site (Sept 2026); new ones are discovered and cached.
    known_models={
        "BHAF": "Alhambra", "BHBO": "Arona", "BHBM": "Ateca", "BHAB": "Ibiza", "BHAI": "Leon 5dr",
        "BHBK": "Leon Estate", "BHBC": "Mii", "BHBN": "Mii electric", "BHBP": "Tarraco",
    },
    body_by_model={
        "alhambra": "MPV", "arona": "SUV", "ateca": "SUV", "tarraco": "SUV",
        "ibiza": "Hatchback", "leon": "Hatchback", "mii": "Hatchback",
    },
)


def search(car: dict[str, Any], settings: dict[str, Any], timings: dict[str, Any] | None = None) -> SearchResult:
    return vtp.search(BRAND, car, settings, timings)
