from __future__ import annotations

import subprocess
import html
import json
import sys
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import streamlit as st
import streamlit.components.v1 as components

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.scorer import (
    carfinder_band,
    carfinder_breakdown_text,
    carfinder_deductions,
    carfinder_headline,
    carfinder_reason,
    carfinder_score_breakdown,
    calculate_carfinder_score,
    confidence_band,
    confidence_reason,
    confidence_score,
)
from app.db import (
    DB_PATH,
    connect,
    get_price_history,
    get_scrape_runs,
    get_vehicle_changes,
    get_vehicles,
    get_dealer_branches,
    get_dealer_groups,
    get_dealer_reachability_settings,
    delete_missing_vehicles,
    init_db,
    refresh_all_vehicle_reachability,
    update_vehicle_manual,
)
from app.reachability import seed_dealer_reachability
from app.settings import (
    BODY_TYPE_OPTIONS,
    FUEL_OPTIONS,
    MAX_CAR_SEARCHES,
    TRANSMISSION_OPTIONS,
    USER_SETTINGS_PATH,
    claim_legacy_settings,
    enabled_car_searches,
    int_or_none,
    load_settings,
    load_version,
    person_settings,
    save_settings,
    set_person_settings,
    text_or_blank,
)
from app.sources import available_makes, modules, source_for_make
from app.users import (
    USERS_PATH,
    authenticate,
    create_user,
    delete_user,
    has_users,
    list_users,
    set_password,
    update_user,
)

SETTINGS = load_settings()
VERSION_INFO = load_version()


def current_user() -> dict:
    return st.session_state.get("user") or {}


def current_username() -> str:
    return str(current_user().get("username") or "")


def is_admin() -> bool:
    return current_user().get("role") == "admin"


def my_settings() -> dict:
    return person_settings(SETTINGS, current_username())


def load_mileage_colour_settings() -> dict[str, object]:
    defaults: dict[str, object] = {
        "green_max": 8000,
        "yellow_max": 12000,
        "orange_max": 16000,
        "green_colour": "#008000",
        "normal_colour": "#E67E22",
        "high_colour": "#C0392B",
        "very_high_colour": "#8E44AD",
    }
    settings = SETTINGS.get("mileage_per_year", {})
    if not isinstance(settings, dict):
        return defaults

    out = defaults.copy()
    for key in defaults:
        if key in settings:
            out[key] = settings[key]

    # Backwards compatibility for older config files that used the old
    # yellow/orange/red names for the three higher mileage bands.
    legacy_key_map = {
        "yellow_colour": "normal_colour",
        "orange_colour": "high_colour",
        "red_colour": "very_high_colour",
    }
    for old_key, new_key in legacy_key_map.items():
        if old_key in settings and new_key not in settings:
            out[new_key] = settings[old_key]
    return out


SENSOR_LABELS = {
    "unknown": "🟣 Not checked",
    "none": "🔴 No sensors",
    "single": "🟠 Single sensors",
    "front_rear": "🟢 Front + rear",
}

PHOTO_LABELS = {
    "photos": "✓ Photos",
    "awaiting": "⏳ Awaiting",
    "unknown": "?",
}

REACHABILITY_LABELS = {
    "LOCAL": "Local",
    "TRANSFERABLE": "Transferable",
    "REMOTE": "Remote",
}

STATUS_OPTIONS = ["active", "missing", "sold", "rejected"]
SENSOR_OPTIONS = ["unknown", "none", "single", "front_rear"]
SENSOR_DETAIL_OPTIONS = ["unknown", "front_only", "rear_only", "front_rear", "none"]
INTEREST_OPTIONS = ["Not reviewed", "Interested", "Not suitable"]
INTEREST_TO_DB = {"Not reviewed": None, "Interested": "interested", "Not suitable": "rejected"}
INTEREST_FROM_DB = {None: "Not reviewed", "": "Not reviewed", "interested": "Interested", "rejected": "Not suitable"}

MPY_SETTINGS = load_mileage_colour_settings()
MPY_GREEN_MAX = int(MPY_SETTINGS.get("green_max", 8000))
MPY_YELLOW_MAX = int(MPY_SETTINGS.get("yellow_max", 12000))
MPY_ORANGE_MAX = int(MPY_SETTINGS.get("orange_max", 16000))
MPY_GREEN = str(MPY_SETTINGS.get("green_colour", "#008000"))
MPY_NORMAL = str(MPY_SETTINGS.get("normal_colour", "#E67E22"))
MPY_HIGH = str(MPY_SETTINGS.get("high_colour", "#C0392B"))
MPY_VERY_HIGH = str(MPY_SETTINGS.get("very_high_colour", "#8E44AD"))

# Bump this whenever default filter behaviour changes.
FILTER_STATE_VERSION = 9

COLOUR_GROUPS = ["Black", "White", "Grey", "Silver", "Blue", "Red", "Green", "Other", "Unknown"]

st.set_page_config(page_title="CarFinder", page_icon="🚗", layout="wide")


def money(value):
    if value is None or pd.isna(value):
        return ""
    try:
        return f"£{int(value):,}"
    except Exception:
        return str(value)


def whole_number(value):
    if value is None or pd.isna(value):
        return ""
    try:
        return f"{int(value):,}"
    except Exception:
        return str(value)


def infer_registration_date(registration: object, year: object = None) -> date | None:
    """Return an estimated first-registration date.

    UK number plates identify a registration period, not an exact date.
    Examples:
    - 24 plate = 1 Mar 2024 to 31 Aug 2024, estimated here as 1 Mar 2024.
    - 74 plate = 1 Sep 2024 to 28 Feb 2025, estimated here as 1 Sep 2024.

    The fallback year is deliberately conservative and is only used if the
    registration does not contain a standard UK age identifier.
    """
    text = safe_text(registration).upper().replace(" ", "")
    if len(text) >= 4:
        age_id = text[2:4]
        if age_id.isdigit():
            code = int(age_id)
            if 1 <= code <= 49:
                return date(2000 + code, 3, 1)
            if 51 <= code <= 99:
                return date(2000 + code - 50, 9, 1)

    try:
        year_int = int(year)
    except Exception:
        return None
    if 1990 <= year_int <= date.today().year + 1:
        return date(year_int, 1, 1)
    return None


def registration_date_display(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    if isinstance(value, date):
        return value.strftime("%d/%m/%Y")
    return safe_text(value)


def first_registered_date(value: object) -> date | None:
    """The real first-registration date, when the source site gives one."""
    text = safe_text(value).strip()[:10]
    if not text:
        return None
    try:
        return datetime.fromisoformat(text).date()
    except ValueError:
        return None


def miles_per_year(registration: object, year: object, mileage: object, first_registered: object = None) -> int | None:
    """Return approximate annual mileage.

    When the source site gives the real first-registration date (Skoda, SEAT,
    Cupra, Audi) that is used: mileage / years since first registration
    (minimum 3 months).  Otherwise the original calculation below is used
    unchanged.

    Original calculation: uses the advertised vehicle year first.

    This deliberately favours the simple sanity-checkable calculation used when
    reviewing cars: a 2023 car in 2026 is treated as roughly 3 years old. UK
    plate dates are only used as a fallback if the scraper has no advertised
    year for the vehicle.
    """
    if mileage is None or pd.isna(mileage):
        return None

    try:
        mileage_float = float(mileage)
    except Exception:
        return None

    real_date = first_registered_date(first_registered)
    if real_date is not None:
        years_old = max((date.today() - real_date).days / 365.25, 0.25)
        return int(round(mileage_float / years_old))

    try:
        year_int = int(year)
    except Exception:
        year_int = None

    if year_int is not None and 1990 <= year_int <= date.today().year + 1:
        years_old = max(date.today().year - year_int, 1)
    else:
        reg_date = infer_registration_date(registration, year)
        if reg_date is None:
            return None
        days_old = max((date.today() - reg_date).days, 1)
        years_old = max(days_old / 365.25, 0.25)

    try:
        return int(round(mileage_float / years_old))
    except Exception:
        return None


def miles_per_year_display(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    try:
        value_int = int(value)
    except Exception:
        return ""
    if value_int >= 10000:
        return f"{value_int / 1000:.0f}k"
    if value_int >= 1000:
        return f"{value_int / 1000:.1f}k"
    return str(value_int)


def miles_per_year_colour(value: object) -> str:
    if value is None or pd.isna(value):
        return ""
    try:
        value_int = int(value)
    except Exception:
        return ""
    if value_int < MPY_GREEN_MAX:
        return MPY_GREEN
    if value_int < MPY_YELLOW_MAX:
        return MPY_NORMAL
    if value_int < MPY_ORANGE_MAX:
        return MPY_HIGH
    return MPY_VERY_HIGH


def safe_text(value) -> str:
    if value is None or pd.isna(value):
        return ""
    return str(value)


def parse_stored_date(value) -> date:
    text = safe_text(value).strip()
    if not text:
        return date.today()
    try:
        return datetime.fromisoformat(text[:10]).date()
    except ValueError:
        return date.today()


def display_short_date(value) -> str:
    text = safe_text(value).strip()
    if not text:
        return ""
    try:
        return datetime.fromisoformat(text[:10]).strftime("%d/%m")
    except ValueError:
        return text[:5]


def interest_display(row) -> str:
    status = row.get("interest_status")
    short_date = display_short_date(row.get("interest_date"))
    if status == "interested":
        return f"👁 {short_date}".strip()
    if status == "rejected":
        return f"✕ {short_date}".strip()
    return ""


def colour_group(value) -> str:
    text = safe_text(value).strip().lower()
    if not text:
        return "Unknown"
    for group in ("Blue", "Silver", "Grey", "Black", "White", "Red", "Green"):
        if group.lower() in text:
            return group
    if "gray" in text:
        return "Grey"
    return "Other"


def sensor_display(row) -> str:
    status = row.get("sensor_status")
    detail = row.get("sensor_detail")
    if status == "single":
        if detail == "rear_only":
            return "🟠 Rear only"
        if detail == "front_only":
            return "🟠 Front only"
        return "🟠 Single sensors"
    return SENSOR_LABELS.get(status, "🟣 Not checked")


def sort_bucket(row) -> int:
    status = row.get("sensor_status")

    if status == "front_rear":
        return 0
    if status == "unknown":
        return 1
    if status == "single":
        return 2
    if status == "none":
        return 3
    return 4


def set_default_filter_state() -> None:
    # The main page should start as the full scrolling list.
    # Status filtering can still be narrowed manually in the sidebar.
    st.session_state.filter_status_active = True
    st.session_state.filter_status_missing = True
    st.session_state.filter_status_sold = True
    st.session_state.filter_status_rejected = True

    st.session_state.filter_sensor_front_rear = True
    st.session_state.filter_sensor_single = False
    st.session_state.filter_sensor_unknown = True
    st.session_state.filter_sensor_none = False

    st.session_state.filter_colours = list(COLOUR_GROUPS)
    st.session_state.filter_car_search = "All cars"
    for key in ("filter_price_range", "filter_max_mileage", "filter_max_distance"):
        st.session_state.pop(key, None)

    st.session_state.filter_photo_photos = True
    st.session_state.filter_photo_awaiting = True
    st.session_state.filter_photo_unknown = True

    st.session_state.filter_reachability_local = True
    st.session_state.filter_reachability_transferable = True
    st.session_state.filter_reachability_remote = True

    st.session_state.filter_hide_no_sensors = False
    st.session_state.filter_unknown_only = False
    st.session_state.filter_state_version = FILTER_STATE_VERSION


def set_show_everything_filter_state() -> None:
    st.session_state.filter_status_active = True
    st.session_state.filter_status_missing = True
    st.session_state.filter_status_sold = True
    st.session_state.filter_status_rejected = True

    st.session_state.filter_sensor_front_rear = True
    st.session_state.filter_sensor_single = False
    st.session_state.filter_sensor_unknown = True
    st.session_state.filter_sensor_none = False

    st.session_state.filter_colours = list(COLOUR_GROUPS)
    st.session_state.filter_car_search = "All cars"
    for key in ("filter_price_range", "filter_max_mileage", "filter_max_distance"):
        st.session_state.pop(key, None)

    st.session_state.filter_photo_photos = True
    st.session_state.filter_photo_awaiting = True
    st.session_state.filter_photo_unknown = True

    st.session_state.filter_reachability_local = True
    st.session_state.filter_reachability_transferable = True
    st.session_state.filter_reachability_remote = True

    st.session_state.filter_hide_no_sensors = False
    st.session_state.filter_unknown_only = False
    st.session_state.filter_state_version = FILTER_STATE_VERSION


def ensure_filter_state() -> None:
    if st.session_state.get("filter_state_version") != FILTER_STATE_VERSION:
        set_default_filter_state()


def car_label(row) -> str:
    name = safe_text(row.get("car_search_name")).strip()
    if name:
        return name
    return " ".join(part for part in (safe_text(row.get("make")), safe_text(row.get("model"))) if part).strip()


def make_dataframe(rows: list[dict]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    if df.empty:
        return df

    if "photo_status" not in df.columns:
        df["photo_status"] = "unknown"

    df["sensor_marker"] = df.apply(sensor_display, axis=1)
    if "interest_status" not in df.columns:
        df["interest_status"] = None
    if "interest_date" not in df.columns:
        df["interest_date"] = None
    if "interest_reason" not in df.columns:
        df["interest_reason"] = None

    if "reachability_status" not in df.columns:
        df["reachability_status"] = "REMOTE"
    df["reachability_status"] = df["reachability_status"].fillna("REMOTE").astype(str).str.upper()

    df["photo_marker"] = df["photo_status"].map(PHOTO_LABELS).fillna("?")
    df["reachability_display"] = df["reachability_status"].map(REACHABILITY_LABELS).fillna("Remote")
    df["reachability_short"] = df["reachability_status"].map({"LOCAL": "L", "TRANSFERABLE": "T", "REMOTE": "R"}).fillna("R")
    df["interest_marker"] = df.apply(interest_display, axis=1)
    df["colour_group"] = df["colour"].apply(colour_group)
    df["sort_bucket"] = df.apply(sort_bucket, axis=1)
    df["status_bucket"] = df["status"].map({"active": 0, "missing": 1, "sold": 2, "rejected": 3}).fillna(4).astype(int)
    df["interest_bucket"] = df["interest_status"].map({"interested": 0, "rejected": 2}).fillna(1).astype(int)
    df["reachability_bucket"] = df["reachability_status"].map({"LOCAL": 0, "TRANSFERABLE": 1, "REMOTE": 2}).fillna(2).astype(int)
    df["price_change"] = df.apply(
        lambda r: "" if pd.isna(r.get("previous_price")) or pd.isna(r.get("price_current"))
        else int(r.get("price_current")) - int(r.get("previous_price")),
        axis=1,
    )
    df["price_change_display"] = df["price_change"].apply(
        lambda x: "" if x == "" else (f"+£{x:,}" if x > 0 else f"-£{abs(x):,}" if x < 0 else "£0")
    )
    df["price_display"] = df["price_current"].apply(money)
    df["mileage_display"] = df["mileage"].apply(whole_number)
    if "first_registered" not in df.columns:
        df["first_registered"] = None
    df["estimated_registration_date"] = df.apply(
        lambda r: first_registered_date(r.get("first_registered")) or infer_registration_date(r.get("registration"), r.get("year")),
        axis=1,
    )
    df["estimated_registration_date_display"] = df["estimated_registration_date"].apply(registration_date_display)
    df["miles_per_year"] = df.apply(
        lambda r: miles_per_year(r.get("registration"), r.get("year"), r.get("mileage"), r.get("first_registered")),
        axis=1,
    )
    df["miles_per_year_sort"] = df["miles_per_year"].fillna(999999).astype(int)
    df["miles_per_year_display"] = df["miles_per_year"].apply(miles_per_year_display)
    df["miles_per_year_colour"] = df["miles_per_year"].apply(miles_per_year_colour)
    for column in ("car_search_id", "car_search_name", "make", "model", "fuel", "transmission", "source"):
        if column not in df.columns:
            df[column] = None
    df["car_label"] = df.apply(car_label, axis=1)
    # Price points are relative to other cars from the same car search, so a
    # cheaper model never makes a dearer one look poor value.
    group_key = df["car_search_id"].fillna("").astype(str)
    prices = pd.to_numeric(df["price_current"], errors="coerce")
    df["price_min"] = prices.groupby(group_key).transform("min")
    df["price_max"] = prices.groupby(group_key).transform("max")
    df["carfinder_score"] = df.apply(lambda r: calculate_carfinder_score(r.to_dict()), axis=1).astype(int)
    df["carfinder_reason"] = df.apply(lambda r: carfinder_reason(r.to_dict()), axis=1)
    df["carfinder_headline"] = df.apply(lambda r: carfinder_headline(r.to_dict()), axis=1)
    df["carfinder_breakdown"] = df.apply(lambda r: carfinder_breakdown_text(r.to_dict()), axis=1)
    df["carfinder_score_sort"] = df["carfinder_score"].fillna(0).astype(int)
    df["distance_display"] = df["distance_miles"].apply(lambda x: "" if pd.isna(x) else f"{int(x)} mi")
    df["checked_display"] = df["checked"].apply(lambda x: "Yes" if int(x or 0) else "No")
    return df



def safe_url(value: object) -> str | None:
    if value is None:
        return None
    if pd.isna(value):
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.startswith("//"):
        return "https:" + text
    if text.startswith("/"):
        # Older rows stored site-relative VW links.
        return "https://usedcars.volkswagen.co.uk" + text
    if text.startswith("http://") or text.startswith("https://"):
        return text
    return None

def clear_current_selection() -> None:
    st.session_state.selected_vehicle_registration = None
    st.session_state.cars_table_version = st.session_state.get("cars_table_version", 0) + 1
    set_selected_registration_query(None)


def checked_status_values() -> list[str]:
    selected = []
    st.markdown("#### Status")
    c1, c2 = st.columns(2)
    with c1:
        if st.checkbox("Active", key="filter_status_active", on_change=clear_current_selection):
            selected.append("active")
        if st.checkbox("Sold", key="filter_status_sold", on_change=clear_current_selection):
            selected.append("sold")
    with c2:
        if st.checkbox("Missing", key="filter_status_missing", on_change=clear_current_selection):
            selected.append("missing")
        if st.checkbox("Rejected", key="filter_status_rejected", on_change=clear_current_selection):
            selected.append("rejected")
    return selected


def checked_sensor_values() -> list[str]:
    selected = []
    st.markdown("#### Sensors")
    if st.checkbox("Front + rear", key="filter_sensor_front_rear", on_change=clear_current_selection):
        selected.append("front_rear")
    if st.checkbox("Rear only / single", key="filter_sensor_single", on_change=clear_current_selection):
        selected.append("single")
    if st.checkbox("Unknown", key="filter_sensor_unknown", on_change=clear_current_selection):
        selected.append("unknown")
    if st.checkbox("No sensors", key="filter_sensor_none", on_change=clear_current_selection):
        selected.append("none")
    return selected


def checked_colour_values() -> list[str]:
    st.markdown("#### Colour group")
    return list(st.multiselect(
        "Colour group",
        COLOUR_GROUPS,
        key="filter_colours",
        label_visibility="collapsed",
        on_change=clear_current_selection,
    ))


def selected_car_search(df: pd.DataFrame) -> str:
    names = [text_or_blank(car.get("name")) for car in my_settings().get("car_searches") or []]
    for name in df.get("car_label", pd.Series(dtype=str)).dropna().astype(str):
        if name and name not in names:
            names.append(name)
    options = ["All cars"] + [n for n in names if n]
    if st.session_state.get("filter_car_search") not in options:
        st.session_state.filter_car_search = "All cars"
    return st.selectbox("Car", options, key="filter_car_search", on_change=clear_current_selection)


def slider_bounds(series: pd.Series, step: int, empty_high: int) -> tuple[int, int]:
    """Slider range covering the data, rounded out to whole steps."""
    values = pd.to_numeric(series, errors="coerce").dropna()
    if values.empty:
        return 0, empty_high
    low = int(values.min()) // step * step
    high = -(-int(values.max()) // step) * step
    if high <= low:
        high = low + step
    return low, high


def checked_photo_values() -> list[str]:
    selected = []
    st.markdown("#### Photos")
    if st.checkbox("✓ Photos", key="filter_photo_photos", help="Several real dealer photos are available", on_change=clear_current_selection):
        selected.append("photos")
    if st.checkbox("⏳ Awaiting photos", key="filter_photo_awaiting", help="Listing appears to have a stock/coming-soon photo only", on_change=clear_current_selection):
        selected.append("awaiting")
    if st.checkbox("? Unknown photo status", key="filter_photo_unknown", on_change=clear_current_selection):
        selected.append("unknown")
    return selected


def checked_reachability_values() -> list[str]:
    selected = []
    st.markdown("#### Dealer reachability")
    if st.checkbox("Local", key="filter_reachability_local", help="Vehicle is already within the configured local radius.", on_change=clear_current_selection):
        selected.append("LOCAL")
    if st.checkbox("Transferable", key="filter_reachability_transferable", help="Dealer group has a configured nearby branch.", on_change=clear_current_selection):
        selected.append("TRANSFERABLE")
    if st.checkbox("Remote", key="filter_reachability_remote", help="No configured local viewing route yet.", on_change=clear_current_selection):
        selected.append("REMOTE")
    return selected


def apply_filters(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df

    ensure_filter_state()

    with st.sidebar:
        st.header("Filters")

        c1, c2 = st.columns(2)
        with c1:
            if st.button("Default filters", use_container_width=True):
                clear_current_selection()
                set_default_filter_state()
                st.rerun()
        with c2:
            if st.button("Show everything", use_container_width=True):
                clear_current_selection()
                set_show_everything_filter_state()
                st.rerun()

        selected_car = selected_car_search(df)
        selected_status = checked_status_values()
        selected_sensors = checked_sensor_values()
        selected_colours = checked_colour_values()
        selected_photos = checked_photo_values()
        selected_reachability = checked_reachability_values()

        max_distance_value = 900
        if df["distance_miles"].notna().any():
            max_distance_value = max(25, min(900, int(df["distance_miles"].dropna().max())))
        max_distance = st.slider("Max distance", 0, 900, max_distance_value, 25, key="filter_max_distance", on_change=clear_current_selection)

        price_low, price_high = slider_bounds(df["price_current"], 500, 50000)
        current_range = st.session_state.get("filter_price_range")
        if not (isinstance(current_range, (list, tuple)) and len(current_range) == 2
                and price_low <= current_range[0] <= current_range[1] <= price_high):
            st.session_state.filter_price_range = (price_low, price_high)
        price_range = st.slider(
            "App price filter",
            min_value=price_low,
            max_value=price_high,
            step=100,
            key="filter_price_range",
            on_change=clear_current_selection,
        )

        _, mileage_high = slider_bounds(df["mileage"], 5000, 40000)
        mileage_high = max(mileage_high, 10000)
        current_mileage = st.session_state.get("filter_max_mileage")
        if not isinstance(current_mileage, int) or not (0 <= current_mileage <= mileage_high):
            st.session_state.filter_max_mileage = mileage_high
        max_mileage = st.slider("Max mileage", 0, mileage_high, step=1000, key="filter_max_mileage", on_change=clear_current_selection)
        hide_no_sensors = st.checkbox("Hide no-sensor cars", key="filter_hide_no_sensors", on_change=clear_current_selection)
        unknown_only = st.checkbox("Only show unchecked sensor cars", key="filter_unknown_only", on_change=clear_current_selection)

    out = df.copy()
    if selected_car and selected_car != "All cars":
        out = out[out["car_label"] == selected_car]
    if selected_status:
        out = out[out["status"].isin(selected_status)]
    if selected_sensors:
        out = out[out["sensor_status"].isin(selected_sensors)]
    if selected_colours:
        out = out[out["colour_group"].isin(selected_colours)]
    if selected_photos:
        out = out[out["photo_status"].isin(selected_photos)]
    if selected_reachability:
        out = out[out["reachability_status"].isin(selected_reachability)]

    out = out[(out["distance_miles"].isna()) | (out["distance_miles"] <= max_distance)]
    out = out[(out["price_current"].isna()) | ((out["price_current"] >= price_range[0]) & (out["price_current"] <= price_range[1]))]
    out = out[(out["mileage"].isna()) | (out["mileage"] <= max_mileage)]

    if hide_no_sensors:
        out = out[out["sensor_status"] != "none"]
    if unknown_only:
        out = out[out["sensor_status"] == "unknown"]

    return out.sort_values(
        by=["interest_bucket", "carfinder_score_sort", "price_current", "registration"],
        ascending=[True, False, True, True],
        na_position="last",
    )


def get_selected_registration_from_query() -> str | None:
    # Do not persist selected rows in the browser URL.  Persisted query/session
    # selection was the cause of a random car opening when the app first loaded.
    return None


def set_selected_registration_query(registration: str | None) -> None:
    # Kept as a no-op wrapper so older call-sites remain harmless.
    return None


def display_table(df: pd.DataFrame) -> dict | None:
    display_cols = [
        "interest_marker",
        "sensor_marker",
        "photo_marker",
        "registration",
        "car_label",
        "status",
        "year",
        "colour",
        "mileage_display",
        "miles_per_year_display",
        "carfinder_score",
        "reachability_short",
        "price_display",
        "price_change_display",
        "dealer",
        "distance_miles",
        "checked_display",
        "notes",
    ]
    rename = {
        "interest_marker": "👁",
        "sensor_marker": "sensors",
        "photo_marker": "photos",
        "registration": "reg",
        "car_label": "car",
        "mileage_display": "miles",
        "miles_per_year_display": "mi/yr",
        "carfinder_score": "score",
        "reachability_short": "DR",
        "price_display": "price",
        "price_change_display": "change",
        "distance_miles": "distance",
        "checked_display": "checked",
    }

    st.caption("Click a car row in the list to open the popup with the full details and edit controls.")

    if df.empty:
        st.info("No cars match the current filters.")
        return None

    table_df = df.reset_index(drop=True).copy()
    visible = table_df[[c for c in display_cols if c in table_df.columns]].rename(columns=rename)
    reg_colour = dict(zip(table_df["registration"].astype(str).str.strip(), table_df.get("miles_per_year_colour", "")))

    def style_registration(value: object) -> str:
        colour = reg_colour.get(str(value).strip(), "")
        if not colour:
            return ""
        return f"color: {colour}; font-weight: 700;"

    styled_visible = visible.style.map(style_registration, subset=["reg"]) if "reg" in visible.columns else visible

    selected_row = None

    # Show the full filtered list down the page, with no pagination and no selector checkbox column.
    # The table key includes a version number so closing a popup or changing filters clears any old row selection.
    row_height_px = 35
    header_height_px = 38
    table_height = header_height_px + (len(visible) * row_height_px) + 8
    table_key = f"cars_table_{st.session_state.get('cars_table_version', 0)}"

    try:
        event = st.dataframe(
            styled_visible,
            use_container_width=True,
            hide_index=True,
            height=table_height,
            column_config={
                "DR": st.column_config.TextColumn("DR", help="Dealer route: L = Local, T = Transferable, R = Remote."),
                "distance": st.column_config.NumberColumn("distance", format="%d mi"),
                "mi/yr": st.column_config.TextColumn("mi/yr", help="Approximate miles per year. Registration text colour uses this value: green low, orange normal, red high, purple very high."),
                "score": st.column_config.NumberColumn("score", help="CarFinder score out of 100. Higher is better.", format="%d"),
                "👁": st.column_config.TextColumn("👁", help="Interest: blank=not reviewed, 👁=interested, ✕=not suitable. Open the popup for date/reason."),
            },
            selection_mode="single-row",
            on_select="rerun",
            key=table_key,
        )

        def _selection_rows(source) -> list[int]:
            if source is None:
                return []
            selection = getattr(source, "selection", None)
            if isinstance(source, dict) and "selection" in source:
                selection = source.get("selection")
            if isinstance(selection, dict):
                return list(selection.get("rows", []) or [])
            if selection is not None:
                return list(getattr(selection, "rows", []) or [])
            return []

        selected_positions = _selection_rows(event)
        if not selected_positions:
            selected_positions = _selection_rows(st.session_state.get(table_key))

        if selected_positions:
            row_pos = int(selected_positions[0])
            if 0 <= row_pos < len(table_df):
                selected_row = table_df.iloc[row_pos].to_dict()
                selected_reg = str(selected_row.get("registration") or "").strip()
                if selected_reg:
                    st.session_state.selected_vehicle_registration = selected_reg

        if selected_row is None:
            selected_reg = st.session_state.get("selected_vehicle_registration")
            if selected_reg:
                matches = table_df[table_df["registration"].astype(str).str.strip() == str(selected_reg).strip()]
                if not matches.empty:
                    selected_row = matches.iloc[0].to_dict()
    except TypeError:
        # Fallback for older Streamlit versions: still show the full list, but without row-click selection.
        st.dataframe(
            styled_visible,
            use_container_width=True,
            hide_index=True,
            height=table_height,
            column_config={
                "DR": st.column_config.TextColumn("DR", help="Dealer route: L = Local, T = Transferable, R = Remote."),
                "distance": st.column_config.NumberColumn("distance", format="%d mi"),
                "mi/yr": st.column_config.TextColumn("mi/yr", help="Approximate miles per year. Registration text colour uses this value: green low, orange normal, red high, purple very high."),
                "score": st.column_config.NumberColumn("score", help="CarFinder score out of 100. Higher is better.", format="%d"),
                "👁": st.column_config.TextColumn("👁", help="Interest: blank=not reviewed, 👁=interested, ✕=not suitable. Open the popup for date/reason."),
            },
            key=f"cars_table_fallback_{st.session_state.get('cars_table_version', 0)}",
        )
        st.warning("This Streamlit version does not support row-click selection. Upgrade Streamlit to enable the popup.")

    return selected_row

def _best_buy_candidates(df: pd.DataFrame) -> pd.DataFrame:
    if df.empty:
        return df
    out = df.copy()
    if "status" in out.columns:
        out = out[out["status"].astype(str).str.lower() == "active"]
    if "interest_status" in out.columns:
        out = out[out["interest_status"].fillna("").astype(str).str.lower() != "rejected"]
    if out.empty:
        return out
    return out.sort_values(
        by=["carfinder_score_sort", "interest_bucket", "reachability_bucket", "miles_per_year_sort", "price_current", "distance_miles", "mileage"],
        ascending=[False, True, True, True, True, True, True],
        na_position="last",
    )


def render_top_best_buys(df: pd.DataFrame) -> None:
    candidates = _best_buy_candidates(df).head(10)
    if candidates.empty:
        st.info("No active suitable cars are available for the Top 10 Best Buys panel with the current filters.")
        return

    st.subheader("Top 10 Best Buys")
    st.caption("Active cars only, excluding cars marked Not suitable. Click Open to view the full popup and score explanation.")

    for position, (_, row) in enumerate(candidates.iterrows(), start=1):
        reg = str(row.get("registration") or "").strip()
        title = f"#{position} · {int(row.get('carfinder_score') or 0)}/100 · {safe_text(row.get('carfinder_headline'))}"
        with st.container():
            c1, c2, c3, c4, c5 = st.columns([1.4, 1, 1, 1, 0.8])
            c1.markdown(f"**{title}**")
            c1.caption(f"{reg} · {safe_text(row.get('car_label'))} · {safe_text(row.get('colour_group'))} {safe_text(row.get('year'))}")
            c2.metric("Dealer route", safe_text(row.get("reachability_display")) or "Remote")
            c3.metric("Mi/year", miles_per_year_display(row.get("miles_per_year")))
            c4.metric("Price", money(row.get("price_current")))
            if c5.button("Open", key=f"open_top_buy_{reg}_{position}", use_container_width=True):
                st.session_state.selected_vehicle_registration = reg
                st.session_state.cars_table_version = st.session_state.get("cars_table_version", 0) + 1
                st.rerun()
            st.caption(safe_text(row.get("carfinder_reason")))


def build_chatgpt_export(df: pd.DataFrame) -> str:
    if df.empty:
        return "CARFINDER EXPORT\n" + "=" * 80 + "\n\nNo cars currently shown."

    lines = [
        "CARFINDER EXPORT",
        "=" * 80,
        "",
        "Registration | Car | Make | Model | Trim | Interest | Interest Date | Interest Reason | Status | Reachability | CarFinder Score | Top Reason | Score Breakdown | Main Deductions | CarFinder Reason | Reachability Reason | Sensors | Photos | Checked | Year | Colour Group | Colour | Mileage | Miles Per Year | Price | Previous Price | Price Change | Dealer | Location | Distance | Notes",
        "-" * 80,
    ]

    export_df = df.sort_values(
        by=["interest_bucket", "carfinder_score_sort", "price_current", "registration"],
        ascending=[True, False, True, True],
        na_position="last",
    )

    for _, r in export_df.iterrows():
        distance = r.get("distance_miles")
        distance_text = "" if distance is None or pd.isna(distance) else f"{int(distance)} mi"
        lines.append(
            f"{r.get('registration', '')} | "
            f"{safe_text(r.get('car_label'))} | "
            f"{safe_text(r.get('make'))} | "
            f"{safe_text(r.get('model'))} | "
            f"{safe_text(r.get('trim'))} | "
            f"{r.get('interest_status') or ''} | "
            f"{display_short_date(r.get('interest_date'))} | "
            f"{'' if pd.isna(r.get('interest_reason')) else (r.get('interest_reason') or '')} | "
            f"{r.get('status', '')} | "
            f"{r.get('reachability_display', '')} | "
            f"{r.get('carfinder_score', '')} | "
            f"{r.get('carfinder_headline', '')} | "
            f"{r.get('carfinder_breakdown', '')} | "
            f"{'; '.join(carfinder_deductions(r.to_dict()))} | "
            f"{r.get('carfinder_reason', '')} | "
            f"{'' if pd.isna(r.get('reachability_reason')) else (r.get('reachability_reason') or '')} | "
            f"{r.get('sensor_marker', '')} | "
            f"{r.get('photo_marker', '')} | "
            f"{'Checked' if int(r.get('checked') or 0) else 'Unchecked'} | "
            f"{'' if pd.isna(r.get('year')) else r.get('year')} | "
            f"{r.get('colour_group', '')} | "
            f"{'' if pd.isna(r.get('colour')) else r.get('colour')} | "
            f"{whole_number(r.get('mileage'))} | "
            f"{miles_per_year_display(r.get('miles_per_year'))} | "
            f"{money(r.get('price_current'))} | "
            f"{money(r.get('previous_price'))} | "
            f"{'' if pd.isna(r.get('price_change')) else r.get('price_change_display', '')} | "
            f"{'' if pd.isna(r.get('dealer')) else r.get('dealer')} | "
            f"{'' if pd.isna(r.get('location')) else r.get('location')} | "
            f"{distance_text} | "
            f"{'' if pd.isna(r.get('notes')) else (r.get('notes') or '')}"
        )

    return "\n".join(lines)



def format_seconds(value) -> str:
    if value is None:
        return ""
    try:
        return f"{float(value):.2f}s"
    except (TypeError, ValueError):
        return ""


def parse_timing_json(run: dict) -> dict:
    raw = run.get("timing_json")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def build_scraper_diagnostics_text(runs: list[dict]) -> str:
    if not runs:
        return "No scraper runs recorded yet."

    latest = runs[0]
    timing = parse_timing_json(latest)
    runtime = latest.get("runtime_seconds")
    detail_count = latest.get("detail_fetch_count") or timing.get("detail_fetch_count") or 0
    detail_seconds = latest.get("detail_fetch_seconds") if latest.get("detail_fetch_seconds") is not None else timing.get("detail_fetch_seconds")
    avg_detail = None
    if detail_count and detail_seconds is not None:
        avg_detail = float(detail_seconds) / int(detail_count)

    lines = [
        "===== CarFinder Scraper Diagnostics =====",
        "",
        "Latest run",
        "----------",
        f"Started: {latest.get('started_at') or ''}",
        f"Completed: {latest.get('completed_at') or ''}",
        f"Status: {latest.get('status') or ''}",
        f"Runtime: {format_seconds(runtime)}",
        f"Cars scraped: {latest.get('cars_found') or 0}",
        f"Matched existing: {latest.get('cars_matched') or 0}",
        f"New cars: {latest.get('cars_new') or 0}",
        f"Marked missing: {latest.get('cars_marked_missing') or 0}",
        f"Dealer photos: {latest.get('cars_with_dealer_photos') or 0}",
        f"Stock/awaiting photos: {latest.get('cars_with_stock_photos') or 0}",
        f"Skipped regs: {latest.get('registrations_skipped') or 0}",
        f"Parsing errors: {latest.get('parsing_errors') or 0}",
        f"Message: {latest.get('message') or ''}",
        "",
        "Timing breakdown",
        "----------------",
        f"Search page fetches: {format_seconds(latest.get('search_fetch_seconds') if latest.get('search_fetch_seconds') is not None else timing.get('search_fetch_seconds'))}",
        f"Parsing + detail checks: {format_seconds(latest.get('parsing_seconds') if latest.get('parsing_seconds') is not None else timing.get('parsing_seconds'))}",
        f"Detail page requests: {detail_count}",
        f"Detail page fetch time: {format_seconds(detail_seconds)}",
        f"Average detail page: {format_seconds(avg_detail)}",
        f"Detail page errors: {latest.get('detail_fetch_errors') if latest.get('detail_fetch_errors') is not None else timing.get('detail_fetch_errors', 0)}",
        f"Database writes: {format_seconds(latest.get('db_write_seconds') if latest.get('db_write_seconds') is not None else timing.get('db_write_seconds'))}",
        f"Missing-car marking: {format_seconds(latest.get('missing_mark_seconds') if latest.get('missing_mark_seconds') is not None else timing.get('missing_mark_seconds'))}",
    ]

    pages = timing.get("pages") or []
    if pages:
        lines.extend(["", "Per-page timing", "---------------"])
        for page in pages:
            lines.append(
                " | ".join([
                    f"page={page.get('page')}",
                    f"fetch={format_seconds(page.get('fetch_seconds'))}",
                    f"parse_detail={format_seconds(page.get('parse_and_detail_seconds'))}",
                    f"total={format_seconds(page.get('total_seconds'))}",
                    f"rows={page.get('rows')}",
                    f"vehicle_objects={page.get('vehicle_objects')}",
                    f"raw_regs={page.get('raw_regs')}",
                ])
            )

    vehicle_changes = timing.get("vehicle_changes") or []
    lines.extend(["", "Vehicle changes", "---------------"])
    if vehicle_changes:
        for item in vehicle_changes:
            lines.append(
                " | ".join([
                    str(item.get("registration") or ""),
                    str(item.get("change_type") or ""),
                    f"{item.get('old_value') if item.get('old_value') is not None else ''} -> {item.get('new_value') if item.get('new_value') is not None else ''}",
                    str(item.get("reason") or ""),
                ])
            )
    else:
        lines.append("None")

    photo_status_changes = timing.get("photo_status_changes") or []
    lines.extend(["", "Photo status changes", "--------------------"])
    if photo_status_changes:
        for item in photo_status_changes:
            lines.append(
                " | ".join([
                    str(item.get("registration") or ""),
                    f"{item.get('from') or ''} -> {item.get('to') or ''}",
                    str(item.get("reason") or ""),
                ])
            )
    else:
        lines.append("None")

    photo_decisions = timing.get("photo_decisions") or []
    if photo_decisions:
        lines.extend(["", "Photo decision log", "------------------"])
        for item in photo_decisions:
            count = item.get("photo_count")
            count_text = "" if count is None else f" | count={count}"
            lines.append(
                " | ".join([
                    str(item.get("registration") or ""),
                    str(item.get("photo_status") or ""),
                    str(item.get("reason") or ""),
                ]) + count_text
            )

    lines.extend(["", "Recent runs", "-----------"])

    for run in runs:
        run_runtime = run.get("runtime_seconds")
        lines.append(
            " | ".join([
                f"{run.get('started_at') or ''}",
                f"status={run.get('status') or ''}",
                f"scraped={run.get('cars_found') or 0}",
                f"matched={run.get('cars_matched') or 0}",
                f"new={run.get('cars_new') or 0}",
                f"missing={run.get('cars_marked_missing') or 0}",
                f"dealer_photos={run.get('cars_with_dealer_photos') or 0}",
                f"stock_photos={run.get('cars_with_stock_photos') or 0}",
                f"skipped_regs={run.get('registrations_skipped') or 0}",
                f"parsing_errors={run.get('parsing_errors') or 0}",
                f"runtime={format_seconds(run_runtime)}",
            ])
        )

    lines.extend(["", "========================================="])
    return "\n".join(lines)


def render_copy_diagnostics(diagnostics_text: str) -> None:
    escaped_text = html.escape(diagnostics_text)
    components.html(
        f"""
        <div style="font-family: sans-serif;">
          <button id="copy-btn" style="padding: 0.4rem 0.75rem; cursor: pointer;">Copy Diagnostics</button>
          <span id="copy-msg" style="margin-left: 0.75rem; color: #555;"></span>
          <textarea id="diag-text" readonly style="width: 100%; height: 260px; margin-top: 0.75rem; font-family: monospace; white-space: pre;">{escaped_text}</textarea>
        </div>
        <script>
          const button = document.getElementById('copy-btn');
          const box = document.getElementById('diag-text');
          const msg = document.getElementById('copy-msg');
          button.addEventListener('click', async () => {{
            try {{
              await navigator.clipboard.writeText(box.value);
              msg.textContent = 'Copied.';
            }} catch (err) {{
              box.focus();
              box.select();
              document.execCommand('copy');
              msg.textContent = 'Copied, or press Ctrl+C while the text is selected.';
            }}
          }});
        </script>
        """,
        height=360,
    )
    st.caption("If the browser blocks automatic copy, click inside the diagnostics box and press Ctrl+A then Ctrl+C.")


def render_scraper_diagnostics(conn) -> None:
    runs = get_scrape_runs(conn, limit=10)

    with st.expander("Scraper diagnostics / recent runs", expanded=False):
        if not runs:
            st.caption("No scraper runs recorded yet.")
            return

        latest = runs[0]
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Cars scraped", latest.get("cars_found") or 0)
        c2.metric("Matched existing", latest.get("cars_matched") or 0)
        c3.metric("New cars", latest.get("cars_new") or 0)
        c4.metric("Marked missing", latest.get("cars_marked_missing") or 0)

        c5, c6, c7, c8 = st.columns(4)
        c5.metric("Dealer photos", latest.get("cars_with_dealer_photos") or 0)
        c6.metric("Stock/awaiting photos", latest.get("cars_with_stock_photos") or 0)
        c7.metric("Skipped regs", latest.get("registrations_skipped") or 0)
        c8.metric("Runtime", format_seconds(latest.get("runtime_seconds")))

        timing = parse_timing_json(latest)
        reachability_counts = timing.get("reachability_counts") or {}
        if reachability_counts:
            r1, r2, r3 = st.columns(3)
            r1.metric("Local", reachability_counts.get("LOCAL", 0))
            r2.metric("Transferable", reachability_counts.get("TRANSFERABLE", 0))
            r3.metric("Remote", reachability_counts.get("REMOTE", 0))

        detail_count = latest.get("detail_fetch_count") or timing.get("detail_fetch_count") or 0
        detail_seconds = latest.get("detail_fetch_seconds") if latest.get("detail_fetch_seconds") is not None else timing.get("detail_fetch_seconds")
        avg_detail = None
        if detail_count and detail_seconds is not None:
            avg_detail = float(detail_seconds) / int(detail_count)

        t1, t2, t3, t4 = st.columns(4)
        t1.metric("Search fetches", format_seconds(latest.get("search_fetch_seconds") if latest.get("search_fetch_seconds") is not None else timing.get("search_fetch_seconds")))
        t2.metric("Parsing/detail", format_seconds(latest.get("parsing_seconds") if latest.get("parsing_seconds") is not None else timing.get("parsing_seconds")))
        t3.metric("Detail requests", int(detail_count or 0))
        t4.metric("Detail fetch time", format_seconds(detail_seconds))

        t5, t6, t7, t8 = st.columns(4)
        t5.metric("Average detail", format_seconds(avg_detail))
        t6.metric("Detail errors", latest.get("detail_fetch_errors") if latest.get("detail_fetch_errors") is not None else timing.get("detail_fetch_errors", 0))
        t7.metric("DB writes", format_seconds(latest.get("db_write_seconds") if latest.get("db_write_seconds") is not None else timing.get("db_write_seconds")))
        t8.metric("Missing check", format_seconds(latest.get("missing_mark_seconds") if latest.get("missing_mark_seconds") is not None else timing.get("missing_mark_seconds")))

        if latest.get("message"):
            st.caption(str(latest.get("message")))

        diagnostics_text = build_scraper_diagnostics_text(runs)
        render_copy_diagnostics(diagnostics_text)

        pages = timing.get("pages") or []
        if pages:
            st.markdown("**Per-page timing**")
            st.dataframe(pd.DataFrame(pages), use_container_width=True, hide_index=True)

        vehicle_changes = timing.get("vehicle_changes") or []
        db_vehicle_changes = []
        latest_run_id = latest.get("id")
        if latest_run_id is not None:
            try:
                db_vehicle_changes = get_vehicle_changes(conn, int(latest_run_id), limit=200)
            except Exception:
                db_vehicle_changes = []
        display_vehicle_changes = vehicle_changes or db_vehicle_changes

        st.markdown("**Vehicle changes**")
        if display_vehicle_changes:
            st.dataframe(pd.DataFrame(display_vehicle_changes), use_container_width=True, hide_index=True)
        else:
            st.caption("No vehicle changes recorded for the latest run.")

        photo_status_changes = timing.get("photo_status_changes") or []
        st.markdown("**Photo status changes**")
        if photo_status_changes:
            st.dataframe(pd.DataFrame(photo_status_changes), use_container_width=True, hide_index=True)
        else:
            st.caption("No photo status changes recorded for the latest run.")

        photo_decisions = timing.get("photo_decisions") or []
        if photo_decisions:
            with st.expander("Photo decision log", expanded=False):
                st.dataframe(pd.DataFrame(photo_decisions), use_container_width=True, hide_index=True)

        display_rows = []
        for run in runs:
            runtime_value = run.get("runtime_seconds")
            display_rows.append({
                "Started": run.get("started_at"),
                "Status": run.get("status"),
                "Scraped": run.get("cars_found"),
                "Matched": run.get("cars_matched"),
                "New": run.get("cars_new"),
                "Missing": run.get("cars_marked_missing"),
                "Dealer photos": run.get("cars_with_dealer_photos"),
                "Stock photos": run.get("cars_with_stock_photos"),
                "Skipped regs": run.get("registrations_skipped"),
                "Parsing errors": run.get("parsing_errors"),
                "Runtime": format_seconds(runtime_value),
                "Search fetches": format_seconds(run.get("search_fetch_seconds")),
                "Parsing/detail": format_seconds(run.get("parsing_seconds")),
                "Detail requests": run.get("detail_fetch_count"),
                "Detail fetch": format_seconds(run.get("detail_fetch_seconds")),
                "DB writes": format_seconds(run.get("db_write_seconds")),
                "Missing check": format_seconds(run.get("missing_mark_seconds")),
                "Message": run.get("message"),
            })

        st.dataframe(pd.DataFrame(display_rows), use_container_width=True, hide_index=True)
        st.caption("Skipped regs means the site exposed a registration in the page source but the scraper did not create a full tracker row for it.")

def render_dealer_reachability_diagnostics(conn) -> None:
    with st.expander("Dealer reachability setup", expanded=False):
        mine = my_settings()
        radius = mine.get("local_radius_miles") or 30
        postcode = mine.get("home_postcode") or "not set"
        st.caption(
            f"Your postcode: {postcode}. Your local radius: {radius} miles. "
            "Your postcode and radius are in ⚙ Settings → My cars; dealer groups are in ⚙ Settings → Household (admin)."
        )

        groups = get_dealer_groups(conn)
        branches = get_dealer_branches(conn)

        if groups:
            st.markdown("**Dealer groups**")
            st.dataframe(pd.DataFrame(groups), use_container_width=True, hide_index=True)
        else:
            st.info("No dealer groups configured yet.")

        if branches:
            st.markdown("**Nearby branches**")
            st.dataframe(pd.DataFrame(branches), use_container_width=True, hide_index=True)
        else:
            st.caption("No nearby branches configured yet.")


def run_script(script: Path) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            [sys.executable, str(script)],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=True,
        )
        return True, (result.stdout or "Done").strip()
    except subprocess.CalledProcessError as exc:
        return False, ((exc.stdout or "") + "\n" + (exc.stderr or str(exc))).strip()


def initialise_browser_session() -> None:
    """Clear stale UI-only state for each new browser session.

    This prevents an old row selection from opening a random car when the
    page is first opened or hard-refreshed.
    """
    if st.session_state.get("browser_session_initialised"):
        return

    st.session_state.browser_session_initialised = True
    st.session_state.selected_vehicle_registration = None
    st.session_state.cars_table_version = st.session_state.get("cars_table_version", 0) + 1


SEARCH_SCRIPT = REPO_ROOT / "scripts" / "run_search.py"


def run_initial_vw_search_once() -> None:
    """Refresh all car searches once when a browser session first opens the app."""
    if st.session_state.get("initial_vw_search_done"):
        return

    st.session_state.initial_vw_search_done = True
    if not enabled_car_searches(SETTINGS):
        return
    with st.spinner("Refreshing car searches..."):
        ok, msg = run_script(SEARCH_SCRIPT)

    st.session_state.initial_vw_search_result = (ok, msg)
    if ok:
        st.rerun()


def show_initial_vw_search_result() -> None:
    result = st.session_state.pop("initial_vw_search_result", None)
    if not result:
        return

    ok, msg = result
    if ok:
        st.success(msg)
    else:
        st.warning("Automatic search had a problem. Use 'Run search' to try again.")
        st.code(msg)


def render_selected_vehicle(row: dict) -> None:
    reg = row.get("registration") or ""

    c1, c2, c3, c4, c5, c6, c7, c8 = st.columns(8)
    c1.metric("Interest", interest_display(row) or "Not reviewed")
    c2.metric("Reachability", row.get("reachability_display") or "Remote")
    c3.metric("CarFinder", f"{int(row.get('carfinder_score') or 0)}/100")
    c4.metric("Sensors", sensor_display(row))
    c5.metric("Photos", PHOTO_LABELS.get(row.get("photo_status"), "?"))
    c6.metric("Price", money(row.get("price_current")))
    c7.metric("Mileage", whole_number(row.get("mileage")))
    c8.metric("Mi/year", miles_per_year_display(row.get("miles_per_year")))

    st.markdown(
        f"""
**Car:** {safe_text(row.get("car_label"))} — {safe_text(row.get("make"))} {safe_text(row.get("model"))} {safe_text(row.get("trim"))}  
**Fuel / gearbox:** {safe_text(row.get("fuel"))} / {safe_text(row.get("transmission"))}  
**Body / seats:** {safe_text(row.get("body_type")) or "not stated"} / {whole_number(row.get("seats")) or "not stated"}  
**Year:** {safe_text(row.get("year"))}  
**Colour:** {safe_text(row.get("colour"))}  
**Dealer:** {safe_text(row.get("dealer"))}  
**Location:** {safe_text(row.get("location"))}  
**Distance:** {safe_text(row.get("distance_display")) or (str(row.get("distance_miles")) + " mi" if row.get("distance_miles") is not None and not pd.isna(row.get("distance_miles")) else "")}  
**CarFinder reason:** {safe_text(row.get("carfinder_reason"))}  
**Reachability reason:** {safe_text(row.get("reachability_reason"))}  
**Dealer group:** {safe_text(row.get("dealer_group_name"))}  
**Nearest branch:** {safe_text(row.get("nearest_branch_name"))}  
**Registration date used for mi/yr and age:** {safe_text(row.get("estimated_registration_date_display"))}{" (from the dealer site)" if safe_text(row.get("first_registered")) else " (estimated from plate/year)"}  
**Interest:** {interest_display(row) or "Not reviewed"}{(" — " + safe_text(row.get("interest_reason"))) if safe_text(row.get("interest_reason")) else ""}
"""
    )

    with st.expander("Explain My Score", expanded=True):
        breakdown = carfinder_score_breakdown(row)
        score_rows = []
        for item in breakdown:
            score_rows.append({
                "Factor": item["factor"],
                "Points": f"{item['points']} / {item['max_points']}",
                "Detail": item["detail"],
            })
        st.table(pd.DataFrame(score_rows))
        deductions = carfinder_deductions(row)
        if deductions:
            st.markdown("**Why isn't this higher?**")
            for deduction in deductions:
                st.write(f"- {deduction}")
        else:
            st.success("This car is already scoring the maximum available points.")

    url = safe_url(row.get("url"))
    if url:
        st.link_button(
            "Open listing",
            url,
            use_container_width=True,
        )
    else:
        st.caption("Listing URL is not available for this car yet.")

    with st.expander("Edit status / sensors / notes", expanded=True):
        conn = connect()
        try:
            current_status = row.get("status") or "active"
            if current_status not in STATUS_OPTIONS:
                current_status = "active"

            current_sensor = row.get("sensor_status") or "unknown"
            if current_sensor not in SENSOR_OPTIONS:
                current_sensor = "unknown"

            status = st.selectbox(
                "Vehicle status",
                STATUS_OPTIONS,
                index=STATUS_OPTIONS.index(current_status),
                key=f"status_{reg}",
            )

            sensor_status = st.selectbox(
                "Sensor status",
                SENSOR_OPTIONS,
                index=SENSOR_OPTIONS.index(current_sensor),
                help="Purple=unknown, Red=none, Orange=single end, Green=front+rear",
                key=f"sensor_status_{reg}",
            )

            sensor_detail_value = row.get("sensor_detail") or "unknown"
            if sensor_detail_value not in SENSOR_DETAIL_OPTIONS:
                sensor_detail_value = "unknown"

            sensor_detail = st.selectbox(
                "Sensor detail",
                SENSOR_DETAIL_OPTIONS,
                index=SENSOR_DETAIL_OPTIONS.index(sensor_detail_value),
                key=f"sensor_detail_{reg}",
            )

            checked = st.checkbox(
                "Checked for sensors",
                value=bool(row.get("checked")),
                key=f"checked_{reg}",
            )

            current_interest = INTEREST_FROM_DB.get(row.get("interest_status"), "Not reviewed")
            interest_choice = st.selectbox(
                "Interest",
                INTEREST_OPTIONS,
                index=INTEREST_OPTIONS.index(current_interest),
                help="Not reviewed = blank, Interested = one to enquire about, Not suitable = checked and rejected.",
                key=f"interest_{reg}",
            )

            interest_date = st.date_input(
                "Interest/review date",
                value=parse_stored_date(row.get("interest_date")),
                help="Defaults to today. Used for Interested and Not suitable cars.",
                key=f"interest_date_{reg}",
            )

            interest_reason = st.text_area(
                "Reason if not suitable",
                value=row.get("interest_reason") or "",
                height=80,
                key=f"interest_reason_{reg}",
            )

            notes = st.text_area(
                "Notes",
                value=row.get("notes") or "",
                height=120,
                key=f"notes_{reg}",
            )

            if st.button("Save selected car", type="primary", key=f"save_{reg}"):
                db_interest = INTEREST_TO_DB.get(interest_choice)
                db_interest_date = interest_date.isoformat() if db_interest else None
                update_vehicle_manual(
                    conn,
                    reg,
                    status,
                    sensor_status,
                    sensor_detail,
                    checked,
                    notes,
                    db_interest,
                    db_interest_date,
                    interest_reason,
                    owner=current_username(),
                )
                st.success("Saved.")
                st.rerun()
        finally:
            conn.close()

    st.markdown("#### Price history")
    conn = connect()
    try:
        history = get_price_history(conn, reg)
    finally:
        conn.close()

    if history:
        hist_df = pd.DataFrame(history)
        hist_df["price"] = hist_df["price"].apply(money)
        st.dataframe(hist_df, use_container_width=True, hide_index=True)
    else:
        st.caption("No price history yet.")


def clear_selected_vehicle() -> None:
    st.session_state.selected_vehicle_registration = None
    st.session_state.cars_table_version = st.session_state.get("cars_table_version", 0) + 1
    set_selected_registration_query(None)


def show_selected_vehicle_dialog(row: dict) -> None:
    reg = row.get("registration") or "Selected car"

    if hasattr(st, "dialog"):
        @st.dialog(f"Selected car: {reg}", width="large")
        def _vehicle_dialog():
            render_selected_vehicle(row)
            if st.button("Close", key=f"close_{reg}"):
                clear_selected_vehicle()
                st.rerun()

        _vehicle_dialog()
    else:
        st.warning("Your Streamlit version does not support pop-up dialogs, so the selected car is shown below the table.")
        st.subheader(f"Selected car: {reg}")
        render_selected_vehicle(row)
        if st.button("Close selected car", key=f"close_{reg}"):
            clear_selected_vehicle()
            st.rerun()


def settings_dataframe(rows: list[dict], columns: list[str]) -> pd.DataFrame:
    return pd.DataFrame([{c: row.get(c) for c in columns} for row in rows], columns=columns)


CAR_EDITOR_COLUMNS = [
    "id", "enabled", "name", "make", "model", "trim", "body_type", "seats_min", "fuel", "transmission",
    "price_min", "price_max", "mileage_max", "year_min", "power_min", "search_url",
]
DEALER_EDITOR_COLUMNS = ["name", "aliases", "branch_name", "branch_town", "branch_distance_miles", "notes"]


def editor_records(df: pd.DataFrame) -> list[dict]:
    records = []
    for record in df.to_dict("records"):
        if any(text_or_blank(v) for k, v in record.items() if k not in {"enabled", "id", "owner"}):
            records.append(record)
    return records


def render_my_cars_tab(person: dict, makes: list[str]):
    st.caption(
        f"Your car searches (up to {MAX_CAR_SEARCHES}). Only you see the cars they find. Each one is searched "
        f"with the module for its make (available now: {', '.join(makes)}). Leave a field blank for no limit. "
        "Model is optional, e.g. any Volkswagen estate with 7 seats."
    )
    c1, c2 = st.columns(2)
    postcode = c1.text_input("Your home postcode", value=person.get("home_postcode") or "", help="Distances are measured from here.")
    local_radius = c2.number_input(
        "Your local radius (miles)", min_value=0, max_value=200, value=int(person.get("local_radius_miles") or 30), step=5,
        help="Cars within this distance count as Local (L).",
    )
    cars_df = settings_dataframe(person.get("car_searches") or [], CAR_EDITOR_COLUMNS)
    edited_cars = st.data_editor(
        cars_df,
        num_rows="dynamic",
        hide_index=True,
        use_container_width=True,
        key="settings_cars_editor",
        column_order=[c for c in CAR_EDITOR_COLUMNS if c != "id"],
        column_config={
            "enabled": st.column_config.CheckboxColumn("On", default=True, help="Include this car when searching."),
            "name": st.column_config.TextColumn("Name", help="Short name shown in the car column, e.g. Octavia Estate.", max_chars=20),
            "make": st.column_config.SelectboxColumn("Make", options=makes, default=makes[0] if makes else None, required=True),
            "model": st.column_config.TextColumn("Model", help="e.g. Golf, Octavia, Touran. Blank = any model."),
            "trim": st.column_config.TextColumn("Trim", help="Exact trim name, e.g. Life or Style. Blank = any."),
            "body_type": st.column_config.SelectboxColumn("Body", options=BODY_TYPE_OPTIONS, default="Any"),
            "seats_min": st.column_config.NumberColumn("Min seats", min_value=2, max_value=9, step=1, format="%d"),
            "fuel": st.column_config.SelectboxColumn("Fuel", options=FUEL_OPTIONS, default="Any"),
            "transmission": st.column_config.SelectboxColumn("Gearbox", options=TRANSMISSION_OPTIONS, default="Any"),
            "price_min": st.column_config.NumberColumn("Min £", min_value=0, step=500, format="%d"),
            "price_max": st.column_config.NumberColumn("Max £", min_value=0, step=500, format="%d"),
            "mileage_max": st.column_config.NumberColumn("Max miles", min_value=0, step=5000, format="%d"),
            "year_min": st.column_config.NumberColumn("From year", min_value=1990, max_value=2100, step=1, format="%d"),
            "power_min": st.column_config.NumberColumn("Min PS", min_value=0, step=5, format="%d"),
            "search_url": st.column_config.TextColumn("Search URL (optional)", help="Overrides the fields above for the search itself."),
        },
    )
    car_rows = editor_records(edited_cars)
    if len(car_rows) > MAX_CAR_SEARCHES:
        st.error(f"Only {MAX_CAR_SEARCHES} car searches are allowed. Remove {len(car_rows) - MAX_CAR_SEARCHES}.")
    return {"home_postcode": postcode, "local_radius_miles": local_radius, "car_searches": car_rows}


def render_household_tab(settings: dict) -> dict:
    st.caption("Household settings apply to everyone. Only admins can change them.")
    c1, c2 = st.columns(2)
    default_radius = c1.number_input(
        "Default local radius (miles)", min_value=0, max_value=200, value=int(settings["local_radius_miles"]), step=5,
        help="Used for anyone who hasn't set their own.",
    )
    search_radius = c2.number_input("Search radius (miles)", min_value=10, max_value=900, value=int(settings["search_radius_miles"]), step=10)
    st.markdown("**Dealer groups**")
    st.caption(
        "A car from a dealer matching a group's name or alias is Transferable (T) when the group has a branch "
        "within the person's local radius. Aliases are comma-separated."
    )
    groups = [dict(g, aliases=", ".join(g.get("aliases") or [])) for g in settings["dealer_groups"]]
    edited_groups = st.data_editor(
        settings_dataframe(groups, DEALER_EDITOR_COLUMNS),
        num_rows="dynamic",
        hide_index=True,
        use_container_width=True,
        key="settings_dealers_editor",
        column_config={
            "name": st.column_config.TextColumn("Group", required=True),
            "aliases": st.column_config.TextColumn("Aliases"),
            "branch_name": st.column_config.TextColumn("Nearby branch"),
            "branch_town": st.column_config.TextColumn("Town"),
            "branch_distance_miles": st.column_config.NumberColumn("Miles", min_value=0, step=1, format="%d"),
            "notes": st.column_config.TextColumn("Notes"),
        },
    )
    st.markdown("**Mileage colours**")
    st.caption("The registration is coloured by miles per year so low-use cars stand out.")
    mpy = settings["mileage_per_year"]
    c1, c2, c3 = st.columns(3)
    green_max = c1.number_input("Green below (mi/yr)", min_value=1000, max_value=50000, value=int(mpy["green_max"]), step=500)
    yellow_max = c2.number_input("Orange below (mi/yr)", min_value=1000, max_value=50000, value=int(mpy["yellow_max"]), step=500)
    orange_max = c3.number_input("Red below (mi/yr)", min_value=1000, max_value=50000, value=int(mpy["orange_max"]), step=500)
    c4, c5, c6, c7 = st.columns(4)
    colours = {
        "green_colour": c4.color_picker("Low", value=str(mpy["green_colour"])),
        "normal_colour": c5.color_picker("Normal", value=str(mpy["normal_colour"])),
        "high_colour": c6.color_picker("High", value=str(mpy["high_colour"])),
        "very_high_colour": c7.color_picker("Very high", value=str(mpy["very_high_colour"])),
    }
    st.caption("CarFinder Score weights are fixed: Interest 25 · Reachability 30 · Mileage/year 25 · Price 15 · Age 5.")
    return {
        "local_radius_miles": default_radius,
        "search_radius_miles": search_radius,
        "dealer_groups": editor_records(edited_groups),
        "mileage_per_year": {"green_max": green_max, "yellow_max": yellow_max, "orange_max": orange_max, **colours},
    }


def render_users_tab() -> None:
    me = current_username()
    with st.form("change_own_password", clear_on_submit=True):
        st.markdown("**Change your password**")
        current = st.text_input("Current password", type="password")
        new1 = st.text_input("New password", type="password")
        new2 = st.text_input("New password again", type="password")
        if st.form_submit_button("Change password"):
            if not authenticate(me, current):
                st.error("Current password is wrong.")
            elif new1 != new2:
                st.error("The new passwords don't match.")
            else:
                try:
                    set_password(me, new1)
                    st.success("Password changed.")
                except ValueError as exc:
                    st.error(str(exc))

    if not is_admin():
        return

    st.divider()
    st.markdown("**People**")
    users = list_users()
    st.dataframe(
        pd.DataFrame([{"Username": u["username"], "Name": u["display_name"], "Role": u["role"]} for u in users]),
        hide_index=True, use_container_width=True,
    )

    with st.form("add_user", clear_on_submit=True):
        st.markdown("**Add a person**")
        c1, c2, c3 = st.columns(3)
        username = c1.text_input("Username", help="Used to log in, e.g. sarah.")
        display_name = c2.text_input("Name")
        role = c3.selectbox("Role", ["user", "admin"], help="Admins can change household settings and people.")
        password = st.text_input("Starting password", type="password", help="They can change it in Settings → Users.")
        if st.form_submit_button("Add person"):
            try:
                created = create_user(username, password, display_name, role)
                st.success(f"Added {created['display_name']} ({created['username']}).")
            except ValueError as exc:
                st.error(str(exc))

    others = [u["username"] for u in users if u["username"] != me]
    if others:
        with st.form("manage_user", clear_on_submit=True):
            st.markdown("**Reset password or remove someone**")
            c1, c2 = st.columns(2)
            who = c1.selectbox("Person", others)
            new_password = c2.text_input("New password", type="password")
            b1, b2, b3 = st.columns(3)
            do_reset = b1.form_submit_button("Reset password")
            do_admin = b2.form_submit_button("Toggle admin")
            do_delete = b3.form_submit_button("Remove person")
            try:
                if do_reset:
                    set_password(who, new_password)
                    st.success(f"Password reset for {who}.")
                elif do_admin:
                    target = next(u for u in users if u["username"] == who)
                    update_user(who, role="user" if target["role"] == "admin" else "admin")
                    st.success(f"Changed {who}'s role.")
                elif do_delete:
                    delete_user(who)
                    st.success(f"Removed {who}. Their car searches stay in settings until you delete them.")
            except ValueError as exc:
                st.error(str(exc))


def render_settings_body() -> None:
    """Settings screen, in the style of the Quarto/Lipfty settings dialogs."""
    settings = load_settings()
    makes = available_makes()
    me = current_username()
    names = ["My cars"] + (["Household"] if is_admin() else []) + ["Users", "About"]
    tabs = dict(zip(names, st.tabs(names)))

    with tabs["My cars"]:
        person = render_my_cars_tab(person_settings(settings, me), makes)
    household = None
    if "Household" in tabs:
        with tabs["Household"]:
            household = render_household_tab(settings)
    with tabs["Users"]:
        render_users_tab()
    with tabs["About"]:
        version = VERSION_INFO.get("version") or "development"
        built = VERSION_INFO.get("built")
        st.markdown(f"**CarFinder** v{version}" + (f" · built {built}" if built else ""))
        st.markdown(f"Signed in as **{current_user().get('display_name')}** ({me}, {current_user().get('role')})")
        st.markdown("**Search modules**")
        for module in modules().values():
            st.write(f"- {module.SOURCE_NAME}: {', '.join(module.MAKES)}")
        st.caption(f"Database: `{DB_PATH}`")
        st.caption(f"Settings: `{USER_SETTINGS_PATH}` · People: `{USERS_PATH}` (private, not published)")

    st.divider()
    b1, b2, b3 = st.columns([1, 1, 2])
    save = b1.button("Save", type="primary", use_container_width=True)
    save_and_run = b2.button("Save & run search", use_container_width=True)
    if save or save_and_run:
        new_settings = set_person_settings(settings, me, person)
        if household:
            new_settings.update(household)
        try:
            save_settings(new_settings)
        except ValueError as exc:
            st.error(str(exc))
            return
        conn = connect()
        try:
            init_db(conn)
        finally:
            conn.close()
        if save_and_run:
            with st.spinner("Running car searches..."):
                ok, msg = run_script(SEARCH_SCRIPT)
            st.session_state.initial_vw_search_result = (ok, msg)
        st.rerun()


if hasattr(st, "dialog"):
    @st.dialog("⚙ Settings", width="large")
    def open_settings_dialog() -> None:
        render_settings_body()
else:  # pragma: no cover - very old Streamlit
    def open_settings_dialog() -> None:
        st.session_state.show_settings_inline = True


NARROW_SCREEN_CSS = """
<style>
.carfinder-narrow-warning { display: none; }
@media (max-width: 900px) {
  .carfinder-narrow-warning {
    display: block; margin: 0 0 0.75rem 0; padding: 0.6rem 0.9rem; border-radius: 0.5rem;
    background: #FFF4E5; color: #7A4B00; border: 1px solid #F0C27B; font-size: 0.95rem;
  }
}
</style>
<div class="carfinder-narrow-warning">
  CarFinder is designed for a laptop or a tablet in landscape. Some columns may not fit on this screen.
</div>
"""


def render_narrow_screen_warning() -> None:
    st.markdown(NARROW_SCREEN_CSS, unsafe_allow_html=True)


def searchable_makes_text() -> str:
    return "Searchable makes: " + " · ".join(available_makes())


def log_out() -> None:
    for key in list(st.session_state.keys()):
        del st.session_state[key]


def render_login() -> None:
    """Login screen, or first-run admin setup when nobody exists yet."""
    version = VERSION_INFO.get("version")
    render_narrow_screen_warning()
    st.markdown("<h1 style='margin-bottom:0'>🚗 CarFinder</h1>", unsafe_allow_html=True)
    st.caption((f"v{version} · " if version else "") + searchable_makes_text())
    _, middle, _ = st.columns([1, 1.2, 1])
    with middle:
        if not has_users():
            st.subheader("Create the admin account")
            st.caption("First run: this account manages household settings and the other people's logins.")
            with st.form("first_admin"):
                username = st.text_input("Username", help="e.g. bill")
                display_name = st.text_input("Name")
                password = st.text_input("Password", type="password")
                password2 = st.text_input("Password again", type="password")
                if st.form_submit_button("Create account", type="primary"):
                    if password != password2:
                        st.error("The passwords don't match.")
                    else:
                        try:
                            user = create_user(username, password, display_name, role="admin")
                        except ValueError as exc:
                            st.error(str(exc))
                        else:
                            # The v2.0.0 postcode and car searches become this person's.
                            save_settings(claim_legacy_settings(load_settings(), user["username"]))
                            st.session_state.user = user
                            st.rerun()
            return

        st.subheader("Log in")
        with st.form("login"):
            username = st.text_input("Username")
            password = st.text_input("Password", type="password")
            if st.form_submit_button("Log in", type="primary"):
                user = authenticate(username, password)
                if user:
                    st.session_state.user = user
                    st.rerun()
                else:
                    st.error("Username or password is wrong.")


def render_header() -> None:
    version = VERSION_INFO.get("version")
    render_narrow_screen_warning()
    c1, c2 = st.columns([8, 1.4])
    with c1:
        badge = (
            f" <span style='font-size:0.9rem;font-weight:600;padding:0.15rem 0.5rem;border-radius:999px;"
            f"border:1px solid rgba(128,128,128,0.5);vertical-align:middle;'>v{html.escape(version)}</span>"
            if version else ""
        )
        st.markdown(f"<h1 style='margin-bottom:0'>🚗 CarFinder{badge}</h1>", unsafe_allow_html=True)
        cars = enabled_car_searches(SETTINGS, current_username())
        who = html.escape(str(current_user().get("display_name") or ""))
        if cars:
            st.caption(f"{who} · searching: " + " · ".join(f"{c['name']}" for c in cars))
        else:
            st.caption(f"{who} · no car searches yet. Open ⚙ Settings → My cars to add up to 10.")
        st.caption(searchable_makes_text() + " — more makes are being added.")
    with c2:
        st.write("")
        if st.button("⚙ Settings", use_container_width=True, help="Your cars, postcode, password" + (", household and people" if is_admin() else "")):
            clear_selected_vehicle()
            open_settings_dialog()
        if st.button("Log out", use_container_width=True):
            log_out()
            st.rerun()
    if st.session_state.pop("show_settings_inline", False):
        with st.expander("⚙ Settings", expanded=True):
            render_settings_body()


def main():
    if not current_user():
        render_login()
        return
    render_header()

    initialise_browser_session()
    run_initial_vw_search_once()
    show_initial_vw_search_result()

    conn = connect()
    init_db(conn)

    with st.sidebar:
        st.header("Actions")
        if st.button("Run search", type="primary", use_container_width=True, disabled=not enabled_car_searches(SETTINGS),
                     help="Runs everyone's car searches."):
            ok, msg = run_script(SEARCH_SCRIPT)
            if ok:
                st.success(msg)
                st.rerun()
            else:
                st.error("Search had a problem")
                st.code(msg)

        if st.button("Initialise / repair database", use_container_width=True):
            init_db(conn)
            refresh_all_vehicle_reachability(conn)
            conn.commit()
            st.success("Database checked.")

        st.markdown("### Database cleanup")
        st.caption("Use this after changing searches or once missing cars have clearly disappeared from the source website.")
        if st.button("Clear all missing cars", use_container_width=True):
            deleted = delete_missing_vehicles(conn, current_username())
            clear_selected_vehicle()
            if deleted:
                st.success(f"Removed {deleted} missing car{'s' if deleted != 1 else ''} from the database.")
            else:
                st.info("There were no missing cars to remove.")
            st.rerun()

        st.markdown("### Legend")
        st.write("🟢 Front + rear")
        st.write("🟠 Rear only / single")
        st.write("🟣 Not checked")
        st.write("🔴 No sensors")
        st.write("✓ Photos = several real photos")
        st.write("⏳ Awaiting = stock image / 1 photo only")
        st.write("👁 Interested / ✕ Not suitable")
        st.write("DR: L = Local / T = Transferable / R = Remote")
        st.write("Reg colour = miles/year: green low, orange normal, red high, purple very high")
        st.caption(f"Database: `{DB_PATH.name}`")

    render_scraper_diagnostics(conn)
    render_dealer_reachability_diagnostics(conn)

    rows = get_vehicles(conn, current_username())
    conn.close()

    if not rows:
        st.info("No cars yet. Add a car search in ⚙ Settings → My cars, then use 'Run search'.")
        return

    df = make_dataframe(rows)

    total = len(df)
    active = int((df["status"] == "active").sum())
    unchecked = int((df["sensor_status"] == "unknown").sum())
    full = int((df["sensor_status"] == "front_rear").sum())
    awaiting = int((df["photo_status"] == "awaiting").sum())
    reachable = int(df["reachability_status"].isin(["LOCAL", "TRANSFERABLE"]).sum())
    c1, c2, c3, c4, c5, c6 = st.columns(6)
    c1.metric("Tracked cars", total)
    c2.metric("Active", active)
    c3.metric("Reachable", reachable)
    c4.metric("Unchecked", unchecked)
    c5.metric("Full sensors", full)
    c6.metric("Awaiting photos", awaiting)

    filtered = apply_filters(df)
    render_top_best_buys(filtered)

    st.subheader(f"Cars ({len(filtered)} of {len(df)} shown)")

    selected = display_table(filtered)

    if selected is not None:
        show_selected_vehicle_dialog(selected)

    with st.expander("Raw search/debug files"):
        st.write("Last fetched HTML/text are stored under `data/cache/` after a search.")

    st.markdown("---")
    st.subheader("ChatGPT export")
    st.caption("This exports the cars currently shown after filters. Click 'Show everything' first if you want the full database.")
    if st.button("Create copy/paste list for ChatGPT", use_container_width=True):
        export_text = build_chatgpt_export(filtered)
        st.success(f"Created export of {len(filtered)} cars. Copy the text below into ChatGPT.")
        st.text_area("Copy everything below", export_text, height=500, key="chatgpt_export")


if __name__ == "__main__":
    main()
