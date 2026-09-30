"""CarFinder settings.

Defaults ship in ``config/settings.default.json`` (tracked, published to the
public CarFinder-App repo, so it holds nothing personal).  Values entered in
the Settings screen are saved to ``data/settings.json``, which is gitignored,
so a ``git pull`` never overwrites them and they are never published.

Layout of ``data/settings.json`` (v2.0.1+):

    household settings (admin):  search_radius_miles, dealer_groups,
                                 mileage_per_year, local_radius_miles (default)
    people.<username>:           home_postcode, local_radius_miles,
                                 car_searches (up to 10 each)

v2.0.0 kept one postcode and one list of car searches at the top level.  Those
are kept as ``legacy`` until the first admin account claims them.
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any

APP_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SETTINGS_PATH = APP_ROOT / "config" / "settings.default.json"
USER_SETTINGS_PATH = APP_ROOT / "data" / "settings.json"

MAX_CAR_SEARCHES = 10

FUEL_OPTIONS = ["Any", "Petrol", "Diesel", "Hybrid", "Electric"]
TRANSMISSION_OPTIONS = ["Any", "Manual", "Automatic"]
BODY_TYPE_OPTIONS = ["Any", "Hatchback", "Estate", "Saloon", "SUV", "MPV"]

CAR_SEARCH_FIELDS: dict[str, Any] = {
    "id": "",
    "owner": "",
    "enabled": True,
    "name": "",
    "make": "Volkswagen",
    "model": "",
    "trim": "",
    "fuel": "Any",
    "transmission": "Any",
    "body_type": "Any",
    "seats_min": None,
    "price_min": None,
    "price_max": None,
    "mileage_max": None,
    "year_min": None,
    "power_min": None,
    "search_url": "",
}

_FALLBACK_DEFAULTS: dict[str, Any] = {
    "local_radius_miles": 30,
    "search_radius_miles": 900,
    "dealer_groups": [],
    "mileage_per_year": {
        "green_max": 8000,
        "yellow_max": 12000,
        "orange_max": 16000,
        "green_colour": "#008000",
        "normal_colour": "#E67E22",
        "high_colour": "#C0392B",
        "very_high_colour": "#8E44AD",
    },
    "people": {},
}

PERSON_FIELDS: dict[str, Any] = {"home_postcode": "", "local_radius_miles": None, "car_searches": []}


def _read_json(path: Path) -> dict[str, Any] | None:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return data if isinstance(data, dict) else None


def int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        if value != value:  # NaN from pandas/data_editor
            return None
    except Exception:
        return None
    text = str(value).strip().replace(",", "").replace("£", "")
    if not text:
        return None
    try:
        return int(round(float(text)))
    except (TypeError, ValueError):
        return None


def text_or_blank(value: Any) -> str:
    if value is None:
        return ""
    try:
        if value != value:
            return ""
    except Exception:
        return ""
    text = str(value).strip()
    return "" if text.lower() in {"nan", "none", "<na>"} else text


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return slug or "car"


def default_settings() -> dict[str, Any]:
    data = _read_json(DEFAULT_SETTINGS_PATH) or {}
    merged = copy.deepcopy(_FALLBACK_DEFAULTS)
    merged.update({k: v for k, v in data.items() if k in merged})
    return merged


def normalise_car_search(raw: dict[str, Any], used_ids: set[str] | None = None) -> dict[str, Any]:
    used_ids = used_ids if used_ids is not None else set()
    car = copy.deepcopy(CAR_SEARCH_FIELDS)
    for key in car:
        if key in raw:
            car[key] = raw[key]

    for key in ("name", "make", "model", "trim", "search_url", "id", "owner"):
        car[key] = text_or_blank(car[key])
    for key in ("price_min", "price_max", "mileage_max", "year_min", "power_min", "seats_min"):
        car[key] = int_or_none(car[key])

    enabled = raw.get("enabled", True)
    try:
        if enabled is None or enabled != enabled:  # new data_editor rows / NaN
            enabled = True
    except Exception:
        enabled = True
    car["enabled"] = bool(enabled)

    if car["fuel"] not in FUEL_OPTIONS:
        car["fuel"] = "Any"
    if car["transmission"] not in TRANSMISSION_OPTIONS:
        car["transmission"] = "Any"
    if car["body_type"] not in BODY_TYPE_OPTIONS:
        car["body_type"] = "Any"
    if not car["make"]:
        car["make"] = "Volkswagen"
    if not car["name"]:
        body = car["body_type"] if car["body_type"] != "Any" else ""
        car["name"] = " ".join(part for part in (car["make"], car["model"], car["trim"], body) if part).strip() or "Car"

    # Ids are unique across everyone's searches and start with the owner.
    prefix = f"{car['owner']}-" if car["owner"] else ""
    base_id = car["id"] if car["id"] and car["id"].startswith(prefix) else prefix + slugify(car["name"])
    unique_id = base_id
    suffix = 2
    while unique_id in used_ids:
        unique_id = f"{base_id}-{suffix}"
        suffix += 1
    car["id"] = unique_id
    used_ids.add(unique_id)
    return car


def normalise_dealer_group(raw: dict[str, Any]) -> dict[str, Any] | None:
    name = text_or_blank(raw.get("name"))
    if not name:
        return None
    aliases = raw.get("aliases") or []
    if isinstance(aliases, str):
        aliases = [a for a in aliases.split(",")]
    aliases = [text_or_blank(a) for a in aliases if text_or_blank(a)]

    # Older configs used a "branches" list; keep the nearest one.
    branch_name = text_or_blank(raw.get("branch_name"))
    branch_town = text_or_blank(raw.get("branch_town"))
    branch_distance = int_or_none(raw.get("branch_distance_miles"))
    branches = raw.get("branches")
    if not branch_name and isinstance(branches, list) and branches:
        first = branches[0] if isinstance(branches[0], dict) else {}
        branch_name = text_or_blank(first.get("name"))
        branch_town = text_or_blank(first.get("town"))
        branch_distance = int_or_none(first.get("distance_miles"))

    return {
        "name": name,
        "aliases": aliases,
        "notes": text_or_blank(raw.get("notes")),
        "branch_name": branch_name,
        "branch_town": branch_town,
        "branch_distance_miles": branch_distance,
    }


def _has_car_rows(items: Any) -> list[dict[str, Any]]:
    return [
        item for item in (items or [])
        if isinstance(item, dict)
        and (text_or_blank(item.get("model")) or text_or_blank(item.get("name")) or text_or_blank(item.get("search_url")))
    ]


def normalise_person(raw: dict[str, Any] | None, owner: str, used_ids: set[str]) -> dict[str, Any]:
    raw = raw or {}
    person = copy.deepcopy(PERSON_FIELDS)
    person["home_postcode"] = text_or_blank(raw.get("home_postcode")).upper()
    radius = int_or_none(raw.get("local_radius_miles"))
    person["local_radius_miles"] = radius if radius is not None and radius >= 0 else None
    person["car_searches"] = [
        normalise_car_search(dict(item, owner=owner), used_ids)
        for item in _has_car_rows(raw.get("car_searches"))
    ][:MAX_CAR_SEARCHES]
    return person


def normalise_settings(raw: dict[str, Any] | None) -> dict[str, Any]:
    settings = default_settings()
    raw = raw or {}

    for key in ("local_radius_miles", "search_radius_miles"):
        value = int_or_none(raw.get(key))
        if value is not None and value >= 0:
            settings[key] = value

    if isinstance(raw.get("dealer_groups"), list):
        settings["dealer_groups"] = raw["dealer_groups"]
    settings["dealer_groups"] = [
        g for g in (normalise_dealer_group(item) for item in settings["dealer_groups"] if isinstance(item, dict)) if g
    ]

    mpy = dict(settings["mileage_per_year"])
    if isinstance(raw.get("mileage_per_year"), dict):
        for key, value in raw["mileage_per_year"].items():
            if key in mpy and value not in (None, ""):
                mpy[key] = value
    for key in ("green_max", "yellow_max", "orange_max"):
        mpy[key] = int_or_none(mpy[key]) or _FALLBACK_DEFAULTS["mileage_per_year"][key]
    settings["mileage_per_year"] = mpy

    used: set[str] = set()
    people: dict[str, Any] = {}
    raw_people = raw.get("people") if isinstance(raw.get("people"), dict) else {}
    for owner, person in raw_people.items():
        owner = text_or_blank(owner).lower()
        if owner:
            people[owner] = normalise_person(person if isinstance(person, dict) else {}, owner, used)
    settings["people"] = people

    # v2.0.0 layout: one postcode and one list of searches at the top level.
    legacy_cars = _has_car_rows(raw.get("car_searches"))
    legacy_postcode = text_or_blank(raw.get("home_postcode"))
    if isinstance(raw.get("legacy"), dict):
        settings["legacy"] = raw["legacy"]
    elif legacy_cars or legacy_postcode:
        settings["legacy"] = {"home_postcode": legacy_postcode, "car_searches": legacy_cars}
    return settings


def person_settings(settings: dict[str, Any], username: str) -> dict[str, Any]:
    """One person's settings, with household defaults filled in."""
    person = copy.deepcopy((settings.get("people") or {}).get(username) or PERSON_FIELDS)
    if person.get("local_radius_miles") is None:
        person["local_radius_miles"] = int(settings.get("local_radius_miles") or 30)
    person.setdefault("home_postcode", "")
    person.setdefault("car_searches", [])
    return person


def set_person_settings(settings: dict[str, Any], username: str, person: dict[str, Any]) -> dict[str, Any]:
    updated = copy.deepcopy(settings)
    updated.setdefault("people", {})[username] = person
    return updated


def claim_legacy_settings(settings: dict[str, Any], username: str) -> dict[str, Any]:
    """Give the v2.0.0 postcode and car searches to ``username`` (the first admin)."""
    legacy = settings.get("legacy")
    if not legacy:
        return settings
    updated = copy.deepcopy(settings)
    current = (updated.get("people") or {}).get(username) or {}
    person = {
        "home_postcode": current.get("home_postcode") or legacy.get("home_postcode") or "",
        "local_radius_miles": current.get("local_radius_miles"),
        "car_searches": list(current.get("car_searches") or []) + [
            {k: v for k, v in car.items() if k != "id"} for car in legacy.get("car_searches") or []
        ],
    }
    updated.setdefault("people", {})[username] = person
    updated.pop("legacy", None)
    return updated


def load_settings(path: Path | None = None) -> dict[str, Any]:
    path = path or USER_SETTINGS_PATH
    return normalise_settings(_read_json(path))


def validate_settings(settings: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for owner, person in (settings.get("people") or {}).items():
        for car in person.get("car_searches") or []:
            low, high = car.get("price_min"), car.get("price_max")
            if low is not None and high is not None and low > high:
                errors.append(f"{car.get('name')}: minimum price is higher than maximum price.")
    mpy = settings.get("mileage_per_year") or {}
    if not (int(mpy.get("green_max", 0)) < int(mpy.get("yellow_max", 0)) < int(mpy.get("orange_max", 0))):
        errors.append("Mileage colour bands must increase: green < orange < red.")
    return errors


def save_settings(settings: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    path = path or USER_SETTINGS_PATH
    errors: list[str] = []
    for owner, person in (settings.get("people") or {}).items():
        count = len(_has_car_rows((person or {}).get("car_searches")))
        if count > MAX_CAR_SEARCHES:
            errors.append(f"{owner}: a maximum of {MAX_CAR_SEARCHES} car searches is allowed ({count} entered).")
    clean = normalise_settings(settings)
    errors.extend(validate_settings(clean))
    if errors:
        raise ValueError(" ".join(errors))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(clean, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)
    return clean


def all_car_searches(settings: dict[str, Any]) -> list[dict[str, Any]]:
    cars: list[dict[str, Any]] = []
    for person in (settings.get("people") or {}).values():
        cars.extend(person.get("car_searches") or [])
    return cars


def enabled_car_searches(settings: dict[str, Any], owner: str | None = None) -> list[dict[str, Any]]:
    if owner is not None:
        cars = person_settings(settings, owner).get("car_searches") or []
    else:
        cars = all_car_searches(settings)
    return [car for car in cars if car.get("enabled")]


def search_settings_for(settings: dict[str, Any], owner: str) -> dict[str, Any]:
    """Household settings plus the owner's postcode, as passed to a source module."""
    person = person_settings(settings, owner)
    merged = {k: v for k, v in settings.items() if k not in {"people", "legacy"}}
    merged["home_postcode"] = person.get("home_postcode") or ""
    merged["local_radius_miles"] = person.get("local_radius_miles")
    return merged


def load_version() -> dict[str, str]:
    """Version/build information maintained by PSTP."""
    info = {"version": "", "built": ""}
    build = _read_json(APP_ROOT / "build-info.json") or {}
    release = _read_json(APP_ROOT / "release.json") or {}
    info["version"] = str(release.get("version") or build.get("version") or "")
    info["built"] = str(build.get("builtAt") or "")[:10]
    return info
