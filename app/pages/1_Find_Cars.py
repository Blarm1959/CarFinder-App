from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import Any

import pandas as pd
import streamlit as st

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.db import connect, init_db
from app.settings import (
    BODY_TYPE_OPTIONS,
    FUEL_OPTIONS,
    LIMIT_FIELDS,
    TRANSMISSION_OPTIONS,
    int_or_none,
    load_settings,
    normalise_target,
    person_settings,
    save_settings,
    set_person_settings,
    text_or_blank,
)
from app.sources import available_makes
from app.users import list_users

SEARCH_SCRIPT = REPO_ROOT / "scripts" / "run_search.py"

st.set_page_config(page_title="Find Cars · CarFinder", page_icon="🔎", layout="wide")


def current_user() -> dict[str, Any]:
    return st.session_state.get("user") or {}


def current_username() -> str:
    return str(current_user().get("username") or "")


def run_mode(mode: str, owner: str) -> tuple[bool, str]:
    try:
        result = subprocess.run(
            [sys.executable, str(SEARCH_SCRIPT), "--mode", mode, "--owner", owner],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            check=True,
        )
        return True, (result.stdout or "Done").strip()
    except subprocess.CalledProcessError as exc:
        return False, ((exc.stdout or "") + "\n" + (exc.stderr or str(exc))).strip()


def optional_number(label: str, value: Any, *, key: str, help: str | None = None) -> int | None:
    text = st.text_input(
        label,
        value="" if value is None else str(value),
        key=key,
        help=help,
        placeholder="No limit",
    )
    return int_or_none(text)


def save_person(person: dict[str, Any]) -> None:
    settings = load_settings()
    updated = set_person_settings(settings, current_username(), person)
    save_settings(updated)




def sync_target_links(owner: str, targets: list[dict[str, Any]]) -> None:
    """Detach results belonging only to My Car List targets that were removed.

    Vehicle rows, price history and per-person reviews remain in the database
    and will reappear if another active search finds the same registration.
    Disabled targets remain linked; only removed targets are detached.
    """
    keep = {str(t.get("id") or "") for t in targets if t.get("id")}
    conn = connect()
    try:
        init_db(conn)
        rows = conn.execute(
            """
            SELECT DISTINCT car_search_id
            FROM vehicle_searches
            WHERE owner = ? AND car_search_id NOT LIKE ?
            """,
            (owner, f"{owner}-discovery-%"),
        ).fetchall()
        remove = [r["car_search_id"] for r in rows if r["car_search_id"] not in keep]
        if remove:
            placeholders = ",".join("?" for _ in remove)
            conn.execute(
                f"DELETE FROM vehicle_searches WHERE owner = ? AND car_search_id IN ({placeholders})",
                (owner, *remove),
            )
            conn.commit()
    finally:
        conn.close()


def save_target_list(person: dict[str, Any]) -> None:
    save_person(person)
    sync_target_links(
        current_username(),
        list((person.get("my_car_list") or {}).get("targets") or []),
    )


def target_signature(target: dict[str, Any]) -> tuple[str, ...]:
    return tuple(
        str(target.get(k) or "").strip().casefold()
        for k in ("make", "model", "body_type", "fuel", "transmission")
    )


def discovery_rows(owner: str) -> list[dict[str, Any]]:
    conn = connect()
    try:
        init_db(conn)
        rows = conn.execute(
            """
            SELECT
                v.registration, v.make, v.model, v.trim, v.fuel, v.transmission,
                v.body_type, v.seats, v.year, v.mileage, v.price_current,
                v.dealer, v.location, vs.distance_miles, vs.car_search_name
            FROM vehicle_searches vs
            JOIN vehicles v ON v.registration = vs.registration
            WHERE vs.owner = ?
              AND vs.status = 'active'
              AND vs.car_search_id LIKE ?
            ORDER BY v.price_current IS NULL, v.price_current, v.year DESC, v.registration
            """,
            (owner, f"{owner}-discovery-%"),
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def add_result_to_list(person: dict[str, Any], row: dict[str, Any]) -> tuple[bool, str]:
    d = person.get("discovery") or {}
    model = text_or_blank(row.get("model"))
    if not model:
        return False, "This result has no usable model name."

    body = d.get("body_type") if d.get("body_type") != "Any" else "Any"
    fuel = d.get("fuel") if d.get("fuel") != "Any" else "Any"
    gear = d.get("transmission") if d.get("transmission") != "Any" else "Any"

    # Use the source's model identity, but avoid a doubled display name when
    # the model already includes the chosen body form (e.g. Octavia Estate).
    display_model = model
    name = display_model
    if body != "Any" and body.casefold() not in display_model.casefold():
        name = f"{display_model} {body}"

    raw = {
        "enabled": True,
        "name": name,
        "make": text_or_blank(row.get("make")),
        "model": model,
        "variant_text": "",
        "body_type": body,
        "fuel": fuel,
        "transmission": gear,
        "overrides": {},
    }

    targets = list((person.get("my_car_list") or {}).get("targets") or [])
    used_ids = {str(t.get("id") or "") for t in targets}
    target = normalise_target(raw, current_username(), used_ids)
    if not target:
        return False, "Could not create a target from this result."

    if any(target_signature(t) == target_signature(target) for t in targets):
        return False, "That car type is already in My Car List."

    person.setdefault("my_car_list", {}).setdefault("common_limits", {})
    person["my_car_list"]["targets"] = targets + [target]
    save_target_list(person)
    return True, f"Added {target['name']} to My Car List."


def render_location(person: dict[str, Any]) -> None:
    with st.expander("Your location", expanded=False):
        c1, c2 = st.columns(2)
        postcode = c1.text_input(
            "Home postcode",
            value=person.get("home_postcode") or "",
            help="Stored only in your private local settings file.",
        )
        radius = c2.number_input(
            "Local radius (miles)",
            min_value=0,
            max_value=200,
            value=int(person.get("local_radius_miles") or 30),
            step=5,
        )
        if st.button("Save location", key="save_location"):
            person["home_postcode"] = postcode.strip().upper()
            person["local_radius_miles"] = int(radius)
            save_person(person)
            st.success("Location saved.")


def render_discovery(person: dict[str, Any], makes: list[str]) -> None:
    st.subheader("Discovery Search")
    st.caption(
        "Describe the sort of car you want. CarFinder searches the manufacturers you choose "
        "and combines the matching cars into one list."
    )

    preferred = st.multiselect(
        "Preferred manufacturers",
        makes,
        default=[m for m in person.get("preferred_makes") or [] if m in makes],
        help="Your normal shortlist of manufacturers. There is no fixed maximum.",
    )
    person["preferred_makes"] = preferred

    scope_labels = {
        "All supported manufacturers": "all",
        "Preferred manufacturers": "preferred",
        "Choose manufacturers for this search": "selected",
    }
    current_scope = (person.get("discovery") or {}).get("make_scope") or "preferred"
    reverse = {v: k for k, v in scope_labels.items()}
    scope_label = st.radio(
        "Where should CarFinder look?",
        list(scope_labels),
        index=list(scope_labels).index(reverse.get(current_scope, "Preferred manufacturers")),
        horizontal=True,
    )
    scope = scope_labels[scope_label]

    d = dict(person.get("discovery") or {})
    d["make_scope"] = scope

    selected = list(d.get("selected_makes") or [])
    if scope == "selected":
        selected = st.multiselect(
            "Manufacturers for this Discovery search",
            makes,
            default=[m for m in selected if m in makes],
            help="Choose any number. You can select all 32 if you want.",
        )
    d["selected_makes"] = selected

    c1, c2, c3 = st.columns(3)
    d["fuel"] = c1.selectbox(
        "Fuel", FUEL_OPTIONS,
        index=FUEL_OPTIONS.index(d.get("fuel") if d.get("fuel") in FUEL_OPTIONS else "Any"),
    )
    d["body_type"] = c2.selectbox(
        "Body", BODY_TYPE_OPTIONS,
        index=BODY_TYPE_OPTIONS.index(d.get("body_type") if d.get("body_type") in BODY_TYPE_OPTIONS else "Any"),
    )
    d["transmission"] = c3.selectbox(
        "Gearbox", TRANSMISSION_OPTIONS,
        index=TRANSMISSION_OPTIONS.index(
            d.get("transmission") if d.get("transmission") in TRANSMISSION_OPTIONS else "Any"
        ),
    )

    c1, c2, c3, c4 = st.columns(4)
    d["price_min"] = optional_number("Minimum price £", d.get("price_min"), key="disc_price_min")
    d["price_max"] = optional_number("Maximum price £", d.get("price_max"), key="disc_price_max")
    d["mileage_max"] = optional_number("Maximum mileage", d.get("mileage_max"), key="disc_mileage")
    d["year_min"] = optional_number("From year", d.get("year_min"), key="disc_year")

    with st.expander("More options", expanded=False):
        c1, c2 = st.columns(2)
        d["seats_min"] = optional_number(
            "Minimum seats",
            d.get("seats_min"),
            key="disc_seats",
            help="If set, cars with unknown seating capacity are excluded.",
        )
        d["power_min"] = optional_number(
            "Minimum power (PS)",
            d.get("power_min"),
            key="disc_power",
            help="Applied where the manufacturer source provides usable power data.",
        )

    person["discovery"] = d

    if d.get("price_min") is not None and d.get("price_max") is not None and d["price_min"] > d["price_max"]:
        st.error("Minimum price cannot be higher than maximum price.")
        valid = False
    else:
        valid = True

    if scope == "preferred":
        chosen = preferred
    elif scope == "selected":
        chosen = selected
    else:
        chosen = makes

    if not chosen:
        st.warning("Choose at least one manufacturer before running Discovery.")

    b1, b2, _ = st.columns([1, 1, 2])
    if b1.button("Save Discovery", type="primary", use_container_width=True, disabled=not valid):
        save_person(person)
        st.success("Discovery saved.")

    if b2.button(
        "Run Discovery",
        use_container_width=True,
        disabled=not valid or not chosen,
        help="All selected manufacturers are searched. There is no fixed manufacturer limit.",
    ):
        save_person(person)
        with st.spinner(f"Searching {len(chosen)} manufacturer{'s' if len(chosen) != 1 else ''}..."):
            ok, msg = run_mode("discovery", current_username())
        if ok:
            st.success(msg)
        else:
            st.error("Discovery search had a problem.")
            st.code(msg)

    st.divider()
    st.markdown("### Discovery results")
    rows = discovery_rows(current_username())
    if not rows:
        st.caption("No current Discovery results yet. Save the criteria and run Discovery.")
        return

    display = pd.DataFrame(rows)
    display["price"] = display["price_current"].apply(
        lambda x: "" if pd.isna(x) else f"£{int(x):,}"
    )
    display["miles"] = display["mileage"].apply(
        lambda x: "" if pd.isna(x) else f"{int(x):,}"
    )
    st.dataframe(
        display[
            [
                "registration", "make", "model", "trim", "fuel", "transmission",
                "body_type", "year", "miles", "price", "dealer", "distance_miles",
            ]
        ],
        hide_index=True,
        use_container_width=True,
    )

    labels = {}
    for row in rows:
        label = (
            f"{row['registration']} · {row.get('make') or ''} {row.get('model') or ''} "
            f"{row.get('trim') or ''} · "
            f"{'' if row.get('price_current') is None else '£' + format(int(row['price_current']), ',')}"
        ).strip()
        labels[label] = row
    chosen_label = st.selectbox("Car type to add to My Car List", list(labels))
    if st.button("Add to My Car List", type="primary"):
        ok, msg = add_result_to_list(person, labels[chosen_label])
        (st.success if ok else st.info)(msg)
        if ok:
            st.rerun()


def editor_targets(person: dict[str, Any], makes: list[str]) -> None:
    st.subheader("My Car List")
    st.caption(
        "These are the car types you have decided are worth watching. There is no fixed list size. "
        "Common limits apply to every target unless an override is entered."
    )
    my_list = person.setdefault("my_car_list", {"common_limits": {}, "targets": []})
    common = dict(my_list.get("common_limits") or {})

    st.markdown("#### Common limits")
    c1, c2, c3 = st.columns(3)
    common["price_min"] = optional_number("Minimum price £", common.get("price_min"), key="common_price_min")
    common["price_max"] = optional_number("Maximum price £", common.get("price_max"), key="common_price_max")
    common["mileage_max"] = optional_number("Maximum mileage", common.get("mileage_max"), key="common_mileage")
    c1, c2, c3 = st.columns(3)
    common["year_min"] = optional_number("From year", common.get("year_min"), key="common_year")
    common["seats_min"] = optional_number("Minimum seats", common.get("seats_min"), key="common_seats")
    common["power_min"] = optional_number("Minimum power (PS)", common.get("power_min"), key="common_power")
    my_list["common_limits"] = common

    if common.get("price_min") is not None and common.get("price_max") is not None and common["price_min"] > common["price_max"]:
        st.error("Common minimum price cannot be higher than common maximum price.")

    targets = list(my_list.get("targets") or [])
    rows = []
    for t in targets:
        ov = t.get("overrides") or {}
        rows.append({
            "id": t.get("id"),
            "enabled": t.get("enabled", True),
            "name": t.get("name"),
            "make": t.get("make"),
            "model": t.get("model"),
            "body_type": t.get("body_type", "Any"),
            "fuel": t.get("fuel", "Any"),
            "transmission": t.get("transmission", "Any"),
            "variant_text": t.get("variant_text", ""),
            "override_price_min": ov.get("price_min") if "price_min" in ov else None,
            "override_price_max": ov.get("price_max") if "price_max" in ov else None,
            "override_mileage_max": ov.get("mileage_max") if "mileage_max" in ov else None,
            "override_year_min": ov.get("year_min") if "year_min" in ov else None,
            "override_seats_min": ov.get("seats_min") if "seats_min" in ov else None,
            "override_power_min": ov.get("power_min") if "power_min" in ov else None,
        })

    columns = [
        "id", "enabled", "name", "make", "model", "body_type", "fuel",
        "transmission", "variant_text", "override_price_min", "override_price_max",
        "override_mileage_max", "override_year_min", "override_seats_min", "override_power_min",
    ]
    df = pd.DataFrame(rows, columns=columns)
    edited = st.data_editor(
        df,
        num_rows="dynamic",
        hide_index=True,
        use_container_width=True,
        column_order=[c for c in columns if c != "id"],
        column_config={
            "enabled": st.column_config.CheckboxColumn("On", default=True),
            "name": st.column_config.TextColumn("Name"),
            "make": st.column_config.SelectboxColumn("Make", options=makes, required=True),
            "model": st.column_config.TextColumn("Model", required=True),
            "body_type": st.column_config.SelectboxColumn("Body", options=BODY_TYPE_OPTIONS),
            "fuel": st.column_config.SelectboxColumn("Fuel", options=FUEL_OPTIONS),
            "transmission": st.column_config.SelectboxColumn("Gearbox", options=TRANSMISSION_OPTIONS),
            "variant_text": st.column_config.TextColumn(
                "Variant contains",
                help="Optional advanced filter, e.g. M Sport. Blank means any variant.",
            ),
            "override_price_min": st.column_config.NumberColumn("Min £ override", min_value=0, step=500),
            "override_price_max": st.column_config.NumberColumn("Max £ override", min_value=0, step=500),
            "override_mileage_max": st.column_config.NumberColumn("Miles override", min_value=0, step=5000),
            "override_year_min": st.column_config.NumberColumn("Year override", min_value=1990, max_value=2100),
            "override_seats_min": st.column_config.NumberColumn("Seats override", min_value=1, max_value=9),
            "override_power_min": st.column_config.NumberColumn("PS override", min_value=0, step=5),
        },
    )

    new_targets = []
    used_ids: set[str] = set()
    for record in edited.to_dict("records"):
        if not (text_or_blank(record.get("model")) or text_or_blank(record.get("name"))):
            continue
        overrides = {}
        for field in LIMIT_FIELDS:
            column = f"override_{field}"
            value = record.get(column)
            try:
                missing = value is None or value != value
            except Exception:
                missing = value is None
            if not missing:
                overrides[field] = int_or_none(value)

        target = normalise_target({
            "id": record.get("id"),
            "enabled": record.get("enabled", True),
            "name": record.get("name"),
            "make": record.get("make"),
            "model": record.get("model"),
            "body_type": record.get("body_type"),
            "fuel": record.get("fuel"),
            "transmission": record.get("transmission"),
            "variant_text": record.get("variant_text"),
            "overrides": overrides,
        }, current_username(), used_ids)
        if target:
            new_targets.append(target)

    my_list["targets"] = new_targets
    person["my_car_list"] = my_list

    c1, c2, _ = st.columns([1, 1, 2])
    if c1.button("Save My Car List", type="primary", use_container_width=True):
        save_target_list(person)
        st.success("My Car List saved.")

    if c2.button(
        "Run My Car List",
        use_container_width=True,
        disabled=not any(t.get("enabled") for t in new_targets),
    ):
        save_target_list(person)
        with st.spinner("Searching My Car List..."):
            ok, msg = run_mode("targets", current_username())
        if ok:
            st.success(msg)
        else:
            st.error("My Car List search had a problem.")
            st.code(msg)

    st.markdown("#### Remove a target")
    if new_targets:
        label_map = {
            f"{t.get('name')} · {t.get('make')} {t.get('model')}": t.get("id")
            for t in new_targets
        }
        label = st.selectbox("Target to remove", list(label_map), key="remove_target")
        if st.button("Remove from My Car List"):
            target_id = label_map[label]
            person["my_car_list"]["targets"] = [
                t for t in new_targets if t.get("id") != target_id
            ]
            save_target_list(person)
            st.success("Removed from My Car List. Existing vehicle/history data was kept.")
            st.rerun()
    else:
        st.caption("My Car List is empty.")

    st.markdown("#### Clear My Car List")
    confirm = st.checkbox(
        "I want to remove every target from My Car List",
        key="confirm_clear_list",
    )
    if st.button("Clear My Car List", disabled=not confirm or not new_targets):
        person["my_car_list"]["targets"] = []
        save_target_list(person)
        st.success("My Car List cleared. Existing vehicle/history data was kept.")
        st.rerun()


def main() -> None:
    user = current_user()
    if not user:
        st.warning("Please return to CarFinder and log in first.")
        st.stop()

    known_users = {u["username"] for u in list_users()}
    if current_username() not in known_users:
        st.warning("Your login is no longer available. Return to CarFinder and log in again.")
        st.stop()

    st.title("🔎 Find Cars")
    st.caption(
        "Discovery finds possibilities. My Car List keeps watching the car types you decide are interesting."
    )

    settings = load_settings()
    person = person_settings(settings, current_username())
    makes = available_makes()

    render_location(person)

    discovery_tab, my_list_tab = st.tabs(["Discovery", "My Car List"])
    with discovery_tab:
        render_discovery(person, makes)
    with my_list_tab:
        editor_targets(person, makes)

    st.info(
        "The main CarFinder page remains your results/review screen for scoring, price history, "
        "photos, sensors, dealer reachability and Interested / Not suitable."
    )


if __name__ == "__main__":
    main()
