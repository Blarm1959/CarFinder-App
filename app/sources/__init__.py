"""Search source modules.

Each make has its own module (e.g. ``app/sources/vw.py``) that knows how to
search that manufacturer's approved-used site.  Every module returns cars in
the same *standard listing* format, so the database, CarFinder Score, dealer
reachability and the UI never need to know which site a car came from.

To add a make:
1. Create ``app/sources/<make>.py`` with ``SOURCE_NAME``, ``MAKES`` and
   ``search(car_search, settings, timings) -> SearchResult``.
2. Register it in ``_MODULES`` below.

Standard listing (one dict per car; unknown values are None):

    registration     "AB12 CDE" (normalised UK format)
    make, model, trim
    year             model/listing year (int)
    colour
    fuel, transmission
    mileage          int
    price            int, current advertised price
    previous_price   int, if the site shows a reduced-from price
    dealer           dealer/branch name as advertised (used for reachability)
    location         town
    distance_miles   int, distance from the home postcode if the site gives it
    url              absolute link to the advert
    photo_status     "photos" | "awaiting" | "unknown"
    photo_count, photo_reason
    title            short human-readable description
    raw_text         JSON of the source record (for diagnostics)
    source           module key, e.g. "vw"
    status           "active"
    last_seen        ISO timestamp
"""
from __future__ import annotations

from dataclasses import dataclass, field
from types import ModuleType
from typing import Any

STANDARD_FIELDS = (
    "registration", "make", "model", "trim", "year", "colour", "fuel", "transmission",
    "mileage", "price", "previous_price", "dealer", "location", "distance_miles", "url",
    "photo_status", "photo_count", "photo_reason", "title", "raw_text", "source",
    "status", "last_seen",
)


@dataclass
class SearchResult:
    """What a source module returns for one car search."""

    rows: list[dict[str, Any]] = field(default_factory=list)
    # Every registration seen in the raw page data, even ones that could not be
    # turned into a full row.  Used as a safety net before marking cars missing.
    raw_regs: set[str] = field(default_factory=set)
    search_url: str = ""
    debug_html: str = ""
    debug_text: str = ""


def standardise(row: dict[str, Any]) -> dict[str, Any]:
    """Return a row containing every standard field (missing ones as None)."""
    out = {key: row.get(key) for key in STANDARD_FIELDS}
    out["status"] = out["status"] or "active"
    out["photo_status"] = out["photo_status"] or "unknown"
    return out


def _load_modules() -> dict[str, ModuleType]:
    from app.sources import vw

    return {vw.SOURCE_KEY: vw}


_MODULES: dict[str, ModuleType] | None = None


def modules() -> dict[str, ModuleType]:
    global _MODULES
    if _MODULES is None:
        _MODULES = _load_modules()
    return _MODULES


def available_makes() -> list[str]:
    makes: list[str] = []
    for module in modules().values():
        for make in module.MAKES:
            if make not in makes:
                makes.append(make)
    return makes


def source_for_make(make: str | None) -> ModuleType | None:
    wanted = (make or "").strip().lower()
    for module in modules().values():
        if any(wanted == m.lower() for m in module.MAKES):
            return module
    return None
