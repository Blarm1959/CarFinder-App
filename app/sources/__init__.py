"""Search source modules and shared normalisation helpers."""
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
    rows: list[dict[str, Any]] = field(default_factory=list)
    raw_regs: set[str] = field(default_factory=set)
    search_url: str = ""
    debug_html: str = ""
    debug_text: str = ""


def standardise(row: dict[str, Any]) -> dict[str, Any]:
    out = {key: row.get(key) for key in STANDARD_FIELDS}
    out["status"] = out["status"] or "active"
    out["photo_status"] = out["photo_status"] or "unknown"
    return out


BODY_TYPE_WORDS: dict[str, tuple[str, ...]] = {
    "Estate": ("estate", "tourer", "touring", "variant", "sportswagon", "sports wagon", "avant", "combi",
               "shooting brake", "wagon", " sw ", "alltrack"),
    "Saloon": ("saloon", "sedan", "limousine", "fastback"),
    "SUV": ("suv", "4x4", "crossover", "off-road", "offroad"),
    "MPV": ("mpv", "people carrier", "multi purpose", "multi-purpose"),
    "Hatchback": ("hatch",),
}


def classify_body_type(*texts: Any) -> str | None:
    text = " " + " ".join(str(t) for t in texts if t).lower() + " "
    for body, words in BODY_TYPE_WORDS.items():
        if any(word in text for word in words):
            return body
    return None


def body_and_seats_match(car: dict[str, Any], body_type: str | None, seats: Any) -> bool:
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
    from app.sources import (
        audi, bmw, codeweavers, cupra, hyundai, jeepalfa, jlr, kia, mazda, mercedes, mg, mini,
        mitsubishi, nissan, renew, seat, skoda, spoticar, suzuki, toyota, volvo, vw,
    )
    ordered = (
        vw, skoda, seat, cupra, audi, kia, bmw, spoticar, toyota, hyundai, volvo,
        mercedes, renew, nissan, mitsubishi, mazda, jlr, codeweavers, mini, mg, suzuki, jeepalfa,
    )
    return {m.SOURCE_KEY: m for m in ordered}


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
    compact = re.sub(r"[^A-Z0-9]", "", (plate or "").upper())
    if UK_REG.match(compact):
        return f"{compact[:4]} {compact[4:]}"
    if NI_REG.match(compact):
        letters = re.match(r"^[A-Z]+", compact).group(0)
        return f"{letters} {compact[len(letters):]}"
    return None
