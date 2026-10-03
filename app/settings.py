"""CarFinder v3 settings and search model.

v3 separates:
- Discovery: broad "what kind of car am I looking for?" criteria.
- My Car List: saved make/model targets to keep watching.
- Common limits: normal buying limits shared by My Car List targets.
- Per-target overrides: only values that differ from the common limits.

Personal values are written only to data/settings.json, which is gitignored.
config/settings.default.json contains household/public defaults only.
"""
from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from typing import Any, Iterable

APP_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SETTINGS_PATH = APP_ROOT / "config" / "settings.default.json"
USER_SETTINGS_PATH = APP_ROOT / "data" / "settings.json"

SCHEMA_VERSION = 3

FUEL_OPTIONS = ["Any", "Petrol", "Diesel", "Hybrid", "Electric"]
TRANSMISSION_OPTIONS = ["Any", "Manual", "Automatic"]
BODY_TYPE_OPTIONS = ["Any", "Hatchback", "Estate", "Saloon", "SUV", "MPV"]
MAKE_SCOPE_OPTIONS = ["all", "preferred", "selected"]

LIMIT_FIELDS = ("price_min", "price_max", "mileage_max", "year_min", "seats_min", "power_min")


class _NoFixedLimit:
    """Compatibility value for the old v2 Settings table.

    app/main.py still imports MAX_CAR_SEARCHES.  v3 deliberately has no
    user-facing limit.  The old comparison ``len(rows) > MAX_CAR_SEARCHES``
    therefore always evaluates False, while its caption reads sensibly.
    """

    def __str__(self) -> str:
        return "no fixed limit"

    def __format__(self, _spec: str) -> str:
        return "no fixed limit"

    def __lt__(self, _other: Any) -> bool:
        return False


MAX_CAR_SEARCHES = _NoFixedLimit()

LIMIT_DEFAULTS: dict[str, int | None] = {
    "price_min": None,
    "price_max": None,
    "mileage_max": None,
    "year_min": None,
    "seats_min": None,
    "power_min": None,
}

DISCOVERY_DEFAULTS: dict[str, Any] = {
    "make_scope": "preferred",
    "selected_makes": [],
    "fuel": "Any",
    "transmission": "Any",
    "body_type": "Any",
    **LIMIT_DEFAULTS,
}

TARGET_DEFAULTS: dict[str, Any] = {
    "id": "",
    "owner": "",
    "enabled": True,
    "name": "",
    "make": "Volkswagen",
    "model": "",
    "variant_text": "",
    "fuel": "Any",
    "transmission": "Any",
    "body_type": "Any",
    "overrides": {},
}

# Compatibility shape consumed by the existing result/settings page.
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
    "schema_version": SCHEMA_VERSION,
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

PERSON_FIELDS: dict[str, Any] = {
    "home_postcode": "",
    "local_radius_miles": None,
    "preferred_makes": [],
    "discovery": DISCOVERY_DEFAULTS,
    "my_car_list": {"common_limits": LIMIT_DEFAULTS, "targets": []},
}


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
        if value != value:  # NaN / pandas NA
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


def _clean_make_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        text = text_or_blank(item)
        key = text.casefold()
        if text and key not in seen:
            seen.add(key)
            out.append(text)
    return out


def default_settings() -> dict[str, Any]:
    data = _read_json(DEFAULT_SETTINGS_PATH) or {}
    merged = copy.deepcopy(_FALLBACK_DEFAULTS)
    for key in ("local_radius_miles", "search_radius_miles", "dealer_groups", "mileage_per_year"):
        if key in data:
            merged[key] = copy.deepcopy(data[key])
    merged["schema_version"] = SCHEMA_VERSION
    merged["people"] = {}
    return merged


def normalise_limits(raw: Any) -> dict[str, int | None]:
    raw = raw if isinstance(raw, dict) else {}
    return {key: int_or_none(raw.get(key)) for key in LIMIT_FIELDS}


def normalise_overrides(raw: Any) -> dict[str, int | None]:
    """Preserve only explicitly supplied override keys.

    Missing key = inherit common limit.
    Present key with null = explicitly remove the common limit.
    """
    raw = raw if isinstance(raw, dict) else {}
    out: dict[str, int | None] = {}
    for key in LIMIT_FIELDS:
        if key in raw:
            out[key] = int_or_none(raw.get(key))
    return out


def _normalise_choice(value: Any, allowed: list[str], default: str = "Any") -> str:
    text = text_or_blank(value)
    return text if text in allowed else default


def normalise_discovery(raw: Any) -> dict[str, Any]:
    raw = raw if isinstance(raw, dict) else {}
    out = copy.deepcopy(DISCOVERY_DEFAULTS)
    scope = text_or_blank(raw.get("make_scope")).lower()
    out["make_scope"] = scope if scope in MAKE_SCOPE_OPTIONS else "preferred"
    out["selected_makes"] = _clean_make_list(raw.get("selected_makes"))
    out["fuel"] = _normalise_choice(raw.get("fuel"), FUEL_OPTIONS)
    out["transmission"] = _normalise_choice(raw.get("transmission"), TRANSMISSION_OPTIONS)
    out["body_type"] = _normalise_choice(raw.get("body_type"), BODY_TYPE_OPTIONS)
    out.update(normalise_limits(raw))
    return out


def normalise_target(raw: Any, owner: str, used_ids: set[str]) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    make = text_or_blank(raw.get("make"))
    model = text_or_blank(raw.get("model"))
    name = text_or_blank(raw.get("name"))
    if not (make and (model or name)):
        return None

    out = copy.deepcopy(TARGET_DEFAULTS)
    out["owner"] = owner
    out["enabled"] = bool(raw.get("enabled", True))
    out["make"] = make
    out["model"] = model
    out["variant_text"] = text_or_blank(raw.get("variant_text") if "variant_text" in raw else raw.get("trim"))
    out["fuel"] = _normalise_choice(raw.get("fuel"), FUEL_OPTIONS)
    out["transmission"] = _normalise_choice(raw.get("transmission"), TRANSMISSION_OPTIONS)
    out["body_type"] = _normalise_choice(raw.get("body_type"), BODY_TYPE_OPTIONS)
    out["overrides"] = normalise_overrides(raw.get("overrides"))

    body_for_name = out["body_type"] if out["body_type"] != "Any" else ""
    if not name:
        bits = [out["model"], body_for_name]
        name = " ".join(x for x in bits if x).strip() or f"{out['make']} target"
    out["name"] = name

    prefix = f"{owner}-" if owner else ""
    requested_id = text_or_blank(raw.get("id"))
    base = requested_id if requested_id.startswith(prefix) else prefix + slugify(
        " ".join(
            x for x in (
                out["make"], out["model"],
                out["body_type"] if out["body_type"] != "Any" else "",
                out["fuel"] if out["fuel"] != "Any" else "",
                out["transmission"] if out["transmission"] != "Any" else "",
            ) if x
        )
    )
    unique = base
    suffix = 2
    while unique in used_ids:
        unique = f"{base}-{suffix}"
        suffix += 1
    out["id"] = unique
    used_ids.add(unique)
    return out


def normalise_person(raw: Any, owner: str, used_ids: set[str], *, keep_v2_searches: bool = False) -> dict[str, Any]:
    """Normalise one v3 person.

    Old v2 search rows are deliberately not migrated.  The user chose to start
    the v3 search model from scratch.  Postcode/radius are retained because
    they are location preferences, not saved car-search data.
    """
    raw = raw if isinstance(raw, dict) else {}
    person = copy.deepcopy(PERSON_FIELDS)
    person["home_postcode"] = text_or_blank(raw.get("home_postcode")).upper()
    radius = int_or_none(raw.get("local_radius_miles"))
    person["local_radius_miles"] = radius if radius is not None and radius >= 0 else None
    person["preferred_makes"] = _clean_make_list(raw.get("preferred_makes"))
    person["discovery"] = normalise_discovery(raw.get("discovery"))

    my_list = raw.get("my_car_list") if isinstance(raw.get("my_car_list"), dict) else {}
    person["my_car_list"] = {
        "common_limits": normalise_limits(my_list.get("common_limits")),
        "targets": [],
    }
    for item in my_list.get("targets") or []:
        target = normalise_target(item, owner, used_ids)
        if target:
            person["my_car_list"]["targets"].append(target)

    # Compatibility path used only when the existing Settings -> My cars table
    # writes back flattened rows during a v3 session.
    if keep_v2_searches and raw.get("car_searches"):
        person["my_car_list"]["targets"] = compatibility_rows_to_targets(
            raw.get("car_searches") or [],
            owner,
            person["my_car_list"]["common_limits"],
            used_ids,
        )
    return person


def normalise_dealer_group(raw: dict[str, Any]) -> dict[str, Any] | None:
    name = text_or_blank(raw.get("name"))
    if not name:
        return None
    aliases = raw.get("aliases") or []
    if isinstance(aliases, str):
        aliases = aliases.split(",")
    aliases = [text_or_blank(a) for a in aliases if text_or_blank(a)]
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


def normalise_settings(raw: dict[str, Any] | None) -> dict[str, Any]:
    raw = raw or {}
    settings = default_settings()

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

    # v3 intentionally starts car-search configuration from scratch if the
    # file is still a v2 schema.  Preserve only people's location settings.
    incoming_is_v3 = int_or_none(raw.get("schema_version")) == SCHEMA_VERSION
    raw_people = raw.get("people") if isinstance(raw.get("people"), dict) else {}
    used: set[str] = set()
    people: dict[str, Any] = {}
    for owner, pdata in raw_people.items():
        owner = text_or_blank(owner).lower()
        if not owner:
            continue
        source = pdata if isinstance(pdata, dict) else {}
        if not incoming_is_v3:
            source = {
                "home_postcode": source.get("home_postcode", ""),
                "local_radius_miles": source.get("local_radius_miles"),
            }
        people[owner] = normalise_person(source, owner, used)
    settings["people"] = people
    settings["schema_version"] = SCHEMA_VERSION
    return settings


def _effective_limits(common: dict[str, Any], overrides: dict[str, Any]) -> dict[str, int | None]:
    out = normalise_limits(common)
    for key in LIMIT_FIELDS:
        if key in overrides:
            out[key] = int_or_none(overrides.get(key))
    return out


def effective_target_search(target: dict[str, Any], common_limits: dict[str, Any]) -> dict[str, Any]:
    limits = _effective_limits(common_limits, target.get("overrides") or {})
    return {
        "id": target.get("id") or "",
        "owner": target.get("owner") or "",
        "enabled": bool(target.get("enabled", True)),
        "name": target.get("name") or "",
        "make": target.get("make") or "",
        "model": target.get("model") or "",
        "trim": target.get("variant_text") or "",
        "fuel": target.get("fuel") or "Any",
        "transmission": target.get("transmission") or "Any",
        "body_type": target.get("body_type") or "Any",
        **limits,
        "search_url": "",
        "search_kind": "target",
    }


def target_searches(settings: dict[str, Any], owner: str | None = None) -> list[dict[str, Any]]:
    owners: Iterable[tuple[str, dict[str, Any]]]
    if owner is None:
        owners = (settings.get("people") or {}).items()
    else:
        owners = [(owner, person_settings(settings, owner))]

    out: list[dict[str, Any]] = []
    for username, person in owners:
        my_list = person.get("my_car_list") or {}
        common = my_list.get("common_limits") or {}
        for target in my_list.get("targets") or []:
            t = dict(target)
            t["owner"] = username
            out.append(effective_target_search(t, common))
    return out


def enabled_car_searches(settings: dict[str, Any], owner: str | None = None) -> list[dict[str, Any]]:
    """Compatibility name: in v3 this means enabled My Car List targets."""
    return [car for car in target_searches(settings, owner) if car.get("enabled")]


def all_car_searches(settings: dict[str, Any]) -> list[dict[str, Any]]:
    """Compatibility name retained for existing result code."""
    return target_searches(settings)


def discovery_makes(person: dict[str, Any], available: list[str]) -> list[str]:
    discovery = person.get("discovery") or {}
    scope = discovery.get("make_scope") or "preferred"
    wanted = (
        available if scope == "all"
        else person.get("preferred_makes") or [] if scope == "preferred"
        else discovery.get("selected_makes") or []
    )
    allowed = {m.casefold(): m for m in available}
    out: list[str] = []
    seen: set[str] = set()
    for item in wanted:
        actual = allowed.get(text_or_blank(item).casefold())
        if actual and actual.casefold() not in seen:
            seen.add(actual.casefold())
            out.append(actual)
    return out


def discovery_searches(settings: dict[str, Any], owner: str, available: list[str]) -> list[dict[str, Any]]:
    person = person_settings(settings, owner)
    d = person.get("discovery") or {}
    out: list[dict[str, Any]] = []
    for make in discovery_makes(person, available):
        out.append({
            "id": f"{owner}-discovery-{slugify(make)}",
            "owner": owner,
            "enabled": True,
            "name": f"Discovery · {make}",
            "make": make,
            "model": "",
            "trim": "",
            "fuel": d.get("fuel") or "Any",
            "transmission": d.get("transmission") or "Any",
            "body_type": d.get("body_type") or "Any",
            **{k: int_or_none(d.get(k)) for k in LIMIT_FIELDS},
            "search_url": "",
            "search_kind": "discovery",
        })
    return out


def compatibility_rows_to_targets(
    rows: list[dict[str, Any]],
    owner: str,
    common_limits: dict[str, Any],
    used_ids: set[str] | None = None,
) -> list[dict[str, Any]]:
    """Convert the existing Settings data_editor rows back into v3 targets."""
    used_ids = used_ids if used_ids is not None else set()
    common = normalise_limits(common_limits)
    out: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if not (text_or_blank(row.get("model")) or text_or_blank(row.get("name"))):
            continue
        overrides: dict[str, int | None] = {}
        for key in LIMIT_FIELDS:
            value = int_or_none(row.get(key))
            if value != common.get(key):
                overrides[key] = value
        target = normalise_target({
            "id": row.get("id"),
            "enabled": row.get("enabled", True),
            "name": row.get("name"),
            "make": row.get("make"),
            "model": row.get("model"),
            "variant_text": row.get("trim"),
            "fuel": row.get("fuel"),
            "transmission": row.get("transmission"),
            "body_type": row.get("body_type"),
            "overrides": overrides,
        }, owner, used_ids)
        if target:
            out.append(target)
    return out


def person_settings(settings: dict[str, Any], username: str) -> dict[str, Any]:
    person = copy.deepcopy((settings.get("people") or {}).get(username) or PERSON_FIELDS)
    if person.get("local_radius_miles") is None:
        person["local_radius_miles"] = int(settings.get("local_radius_miles") or 30)
    person.setdefault("home_postcode", "")
    person.setdefault("preferred_makes", [])
    person.setdefault("discovery", copy.deepcopy(DISCOVERY_DEFAULTS))
    person.setdefault("my_car_list", {"common_limits": copy.deepcopy(LIMIT_DEFAULTS), "targets": []})
    # Existing result/filter/settings code still asks for car_searches.
    my_list = person.get("my_car_list") or {}
    common = my_list.get("common_limits") or {}
    person["car_searches"] = []
    for target in my_list.get("targets") or []:
        t = dict(target)
        t["owner"] = username
        person["car_searches"].append(effective_target_search(t, common))
    return person


def set_person_settings(settings: dict[str, Any], username: str, person: dict[str, Any]) -> dict[str, Any]:
    """Replace one person's v3 settings, preserving fields omitted by old UI."""
    updated = copy.deepcopy(settings)
    current = copy.deepcopy((updated.get("people") or {}).get(username) or PERSON_FIELDS)
    merged = copy.deepcopy(current)
    for key in ("home_postcode", "local_radius_miles", "preferred_makes", "discovery", "my_car_list"):
        if key in person:
            merged[key] = copy.deepcopy(person[key])

    # Existing Settings -> My cars returns only postcode/radius/car_searches.
    if "car_searches" in person and "my_car_list" not in person:
        common = ((current.get("my_car_list") or {}).get("common_limits") or LIMIT_DEFAULTS)
        merged.setdefault("my_car_list", {})
        merged["my_car_list"]["common_limits"] = normalise_limits(common)
        merged["my_car_list"]["targets"] = compatibility_rows_to_targets(
            person.get("car_searches") or [], username, common, set()
        )

    updated.setdefault("people", {})[username] = merged
    updated["schema_version"] = SCHEMA_VERSION
    return updated


def claim_legacy_settings(settings: dict[str, Any], username: str) -> dict[str, Any]:
    """Compatibility hook for app/main.py.

    v3 deliberately does not claim/migrate old saved searches.
    """
    return settings


def load_settings(path: Path | None = None) -> dict[str, Any]:
    path = path or USER_SETTINGS_PATH
    return normalise_settings(_read_json(path))


def validate_settings(settings: dict[str, Any]) -> list[str]:
    errors: list[str] = []
    for owner, person in (settings.get("people") or {}).items():
        common = normalise_limits(((person.get("my_car_list") or {}).get("common_limits")))
        for label, limits in [("Common limits", common)]:
            low, high = limits.get("price_min"), limits.get("price_max")
            if low is not None and high is not None and low > high:
                errors.append(f"{owner}: {label} minimum price is higher than maximum price.")
        for target in (person.get("my_car_list") or {}).get("targets") or []:
            effective = _effective_limits(common, target.get("overrides") or {})
            low, high = effective.get("price_min"), effective.get("price_max")
            if low is not None and high is not None and low > high:
                errors.append(f"{target.get('name')}: minimum price is higher than maximum price.")
        d = normalise_discovery(person.get("discovery"))
        if d["price_min"] is not None and d["price_max"] is not None and d["price_min"] > d["price_max"]:
            errors.append(f"{owner}: Discovery minimum price is higher than maximum price.")

    mpy = settings.get("mileage_per_year") or {}
    if not (int(mpy.get("green_max", 0)) < int(mpy.get("yellow_max", 0)) < int(mpy.get("orange_max", 0))):
        errors.append("Mileage colour bands must increase: green < orange < red.")
    return errors


def save_settings(settings: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    path = path or USER_SETTINGS_PATH
    # Treat data passed by the v3 page as v3 even when it started from defaults.
    raw = copy.deepcopy(settings)
    raw["schema_version"] = SCHEMA_VERSION
    clean = normalise_settings(raw)
    errors = validate_settings(clean)
    if errors:
        raise ValueError(" ".join(errors))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(clean, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)
    return clean


def search_settings_for(settings: dict[str, Any], owner: str) -> dict[str, Any]:
    """Household settings plus the owner's private location values."""
    person = person_settings(settings, owner)
    merged = {k: v for k, v in settings.items() if k not in {"people"}}
    merged["home_postcode"] = person.get("home_postcode") or ""
    merged["local_radius_miles"] = person.get("local_radius_miles")
    return merged


def load_version() -> dict[str, str]:
    info = {"version": "", "built": ""}
    build = _read_json(APP_ROOT / "build-info.json") or {}
    release = _read_json(APP_ROOT / "release.json") or {}
    info["version"] = str(release.get("version") or build.get("version") or "")
    info["built"] = str(build.get("builtAt") or "")[:10]
    return info
