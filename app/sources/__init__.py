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
    body_type        "Hatchback" | "Estate" | "Saloon" | "SUV" | "MPV" | None
    seats            int
    mileage          int
    price            int, current advertised price
    previous_price   int, if the site shows a reduced-from price
    dealer           dealer/branch name as advertised (used for reachability)
    location         town
    distance_miles   int, distance from the home postcode if the site gives it
    url              absolute link to the advert
    photo_status     "photos" | "awaiting" | "unknown"
    photo_count, photo_reason
    first_registered "YYYY-MM-DD" when the site gives the real date (e.g. Skoda)
    title            short human-readable description
    raw_text         JSON of the source record (for diagnostics)
    source           module key, e.g. "vw"
    status           "active"
    last_seen        ISO timestamp
"""
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field
from types import ModuleType
from typing import Any

STANDARD_FIELDS = (
    "registration", "make", "model", "trim", "year", "colour", "fuel", "transmission", "body_type", "seats",
    "mileage", "price", "previous_price", "dealer", "location", "distance_miles", "url",
    "photo_status", "photo_count", "photo_reason", "first_registered", "title", "raw_text", "source",
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


# Words that identify each body type, in model names or body-style fields.
BODY_TYPE_WORDS: dict[str, tuple[str, ...]] = {
    "Estate": ("estate", "tourer", "touring", "variant", "sportswagon", "sports wagon", "avant", "combi",
               "shooting brake", "wagon", " sw ", "alltrack"),
    "Saloon": ("saloon", "sedan", "limousine", "fastback"),
    "SUV": ("suv", "4x4", "crossover", "off-road", "offroad"),
    "MPV": ("mpv", "people carrier", "multi purpose", "multi-purpose"),
    "Hatchback": ("hatch",),
}


def classify_body_type(*texts: Any) -> str | None:
    """Best-effort body type from any descriptive text (body field, model name)."""
    text = " " + " ".join(str(t) for t in texts if t).lower() + " "
    for body, words in BODY_TYPE_WORDS.items():
        if any(word in text for word in words):
            return body
    return None


def body_and_seats_match(car: dict[str, Any], body_type: str | None, seats: Any) -> bool:
    """Apply a car search's body type / minimum seats.

    Unknown values never reject a car, so listings that don't state their
    body style or seats are kept for you to check.
    """
    wanted = str(car.get("body_type") or "Any")
    if wanted != "Any" and body_type and body_type != wanted:
        return False
    seats_min = car.get("seats_min")
    try:
        seat_count = int(seats) if seats not in (None, "") else None
    except (TypeError, ValueError):
        seat_count = None
    if seats_min and seat_count is not None and seat_count < int(seats_min):
        return False
    return True


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


def _load_modules() -> dict[str, ModuleType]:
    from app.sources import audi, bmw, cupra, hyundai, kia, seat, skoda, spoticar, toyota, volvo, vw

    return {m.SOURCE_KEY: m for m in (vw, skoda, seat, cupra, audi, kia, bmw, spoticar, toyota, hyundai, volvo)}


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


def _plain(text: str | None) -> str:
    """Lower-case with accents removed, so "Škoda" matches "Skoda"."""
    return unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().strip().lower()


def source_for_make(make: str | None) -> ModuleType | None:
    wanted = _plain(make)
    for module in modules().values():
        if any(wanted == _plain(m) for m in module.MAKES):
            return module
    return None


UK_REG = re.compile(r"^[A-Z]{2}\d{2}[A-Z]{3}$")
NI_REG = re.compile(r"^[A-Z]{1,3}\d{1,4}$")


def normalise_plate(plate: str | None) -> str | None:
    """Standard UK plates as "AB12 CDE"; Northern Ireland plates as "MXZ 8376"."""
    compact = re.sub(r"[^A-Z0-9]", "", (plate or "").upper())
    if UK_REG.match(compact):
        return f"{compact[:4]} {compact[4:]}"
    if NI_REG.match(compact):
        letters = re.match(r"^[A-Z]+", compact).group(0)
        return f"{letters} {compact[len(letters):]}"
    return None
