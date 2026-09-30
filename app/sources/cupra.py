"""Cupra approved-used source module (VTP stock API, see ``app/sources/vtp.py``)."""
from __future__ import annotations

from typing import Any

from app.sources import SearchResult
from app.sources import vtp

SOURCE_KEY = "cupra"
SOURCE_NAME = "Cupra Approved Used"
MAKES = ("Cupra",)

BRAND = vtp.Brand(
    key=SOURCE_KEY,
    make="Cupra",
    api_base="https://vtpapi.seat.com/restapi/v1/cuukgwb",
    pattern="cuprawebfe",
    detail_url="https://www.cupraofficial.co.uk/new-cars/used-car-stock-locator/w/offer/used/{key}",
    known_models={
        "EXBM": "Ateca", "EXBQ": "Born", "EXFO": "Formentor", "EXAI": "Leon 5dr", "EXSP": "Leon Estate",
        "EXAG": "Raval", "EXAE": "Tavascan", "EXAF": "Terramar",
    },
    body_by_model={
        "ateca": "SUV", "formentor": "SUV", "tavascan": "SUV", "terramar": "SUV",
        "born": "Hatchback", "raval": "Hatchback", "leon": "Hatchback",
    },
)


def search(car: dict[str, Any], settings: dict[str, Any], timings: dict[str, Any] | None = None) -> SearchResult:
    return vtp.search(BRAND, car, settings, timings)
