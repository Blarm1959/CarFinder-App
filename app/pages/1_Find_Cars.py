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
    DISCOVERY_DEFAULTS,
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

st.set_page_config(page_title="Find Cars · CarFinder", layout="wide")


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
        placeholder="Any",
    )
    return int_or_none(text)


def save_person(person: dict[str, Any]) -> None:
    settings = load_settings()
    updated = set_person_settings(settings, current_username(), person)
    save_settings(updated)


def sync_target_links(owner: str, targets: list[dict[str, Any]]) -> None:
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


def clear_discovery_results(owner: str) -> int:
    """Clear this user's Discovery results without touching My Car List."""
    conn = connect()
    try:
        init_db(conn)
        rows = conn.execute(
            """
            SELECT registration
            FROM vehicle_searches
            WHERE owner = ? AND car_search_id LIKE ?
            """,
            (owner, f"{owner}-discovery-%"),
        ).fetchall()
        regs = {str(r["registration"]) for r in rows}

        cur = conn.execute(
            """
            DELETE FROM vehicle_searches
            WHERE owner = ? AND car_search_id LIKE ?
            """,
            (owner, f"{owner}-discovery-%"),
        )
        removed_links = int(cur.rowcount or 0)

        for reg in regs:
            linked = conn.execute(
                "SELECT 1 FROM vehicle_searches WHERE registration = ? LIMIT 1",
                (reg,),
            ).fetchone()
            if not linked:
                conn.execute("DELETE FROM vehicles WHERE registration = ?", (reg,))

        conn.commit()
        return removed_links
    finally:
        conn.close()


def reset_discovery(person: dict[str, Any]) -> None:
    """Clear Discovery results and reset criteria, preserving user/location/list."""
    clear_discovery_results(current_username())
    reset = dict(DISCOVERY_DEFAULTS)
    reset["make_scope"] = "selected"
    reset["selected_makes"] = []
    person["discovery"] = reset
    save_person(person)
    for key in (
        "disc_price_min", "disc_price_max", "disc_mileage", "disc_year",
        "disc_seats", "disc_power", "disc_scope",
        "disc_fuel_choice", "disc_body_choice", "disc_gear_choice",
    ):
        st.session_state.pop(key, None)


def add_result_to_list(person: dict[str, Any], row: dict[str, Any]) -> tuple[bool, str]:
    d = person.get("discovery") or {}
    model = text_or_blank(row.get("model"))
    if not model:
        return False, "This result has no usable model name."

    body = d.get("body_type") if d.get("body_type") != "Any" else "Any"
    fuel = d.get("fuel") if d.get("fuel") != "Any" else "Any"
    gear = d.get("transmission") if d.get("transmission") != "Any" else "Any"

    name = model
    if body != "Any" and body.casefold() not in model.casefold():
        name = f"{model} {body}"

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
    postcode = person.get("home_postcode") or ""
    radius = int(person.get("local_radius_miles") or 30)
    with st.expander(f"Search area · {postcode or 'postcode not set'} · {radius} miles", expanded=False):
        c1, c2, c3 = st.columns([2, 1, 1])
        postcode_new = c1.text_input(
            "Home postcode",
            value=postcode,
            help="Stored only in your private local settings file.",
        )
        radius_new = c2.number_input(
            "Local radius (miles)",
            min_value=0,
            max_value=200,
            value=radius,
            step=5,
        )
        c3.write("")
        c3.write("")
        if c3.button("Save area", use_container_width=True):
            person["home_postcode"] = postcode_new.strip().upper()
            person["local_radius_miles"] = int(radius_new)
            save_person(person)
            st.success("Search area saved.")


def render_discovery(person: dict[str, Any], makes: list[str]) -> None:
    d = dict(person.get("discovery") or {})

    st.markdown(
        """
        <div class="cf-page-heading">
          <div class="cf-page-title">Discovery</div>
          <div class="cf-page-subtitle">Choose what matters, search broadly, then keep only the car types worth following.</div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    scope_labels = {
        "All manufacturers": "all",
        "Preferred": "preferred",
        "Choose": "selected",
    }
    current_scope = d.get("make_scope") or "selected"
    reverse = {v: k for k, v in scope_labels.items()}

    with st.container(border=True):
        st.markdown("<div class='cf-panel-title'>Manufacturers</div>", unsafe_allow_html=True)
        scope_label = st.radio(
            "Manufacturer scope",
            list(scope_labels),
            index=list(scope_labels).index(reverse.get(current_scope, "Choose")),
            horizontal=True,
            label_visibility="collapsed",
            key="disc_scope",
        )
        scope = scope_labels[scope_label]
        d["make_scope"] = scope

        preferred = list(person.get("preferred_makes") or [])
        selected = list(d.get("selected_makes") or [])

        if scope == "preferred":
            preferred = st.multiselect(
                "Preferred manufacturers",
                makes,
                default=[m for m in preferred if m in makes],
                placeholder="Choose your preferred manufacturers",
            )
            person["preferred_makes"] = preferred
        elif scope == "selected":
            selected = st.multiselect(
                "Manufacturers",
                makes,
                default=[m for m in selected if m in makes],
                placeholder="Choose one or more manufacturers",
                label_visibility="collapsed",
            )
            d["selected_makes"] = selected
        else:
            st.markdown(
                f"<div class='cf-inline-note'>All <strong>{len(makes)}</strong> supported manufacturers will be searched.</div>",
                unsafe_allow_html=True,
            )

    st.markdown("<div class='cf-spacer'></div>", unsafe_allow_html=True)

    with st.container(border=True):
        st.markdown("<div class='cf-panel-title'>Car type</div>", unsafe_allow_html=True)
        c1, c2, c3 = st.columns(3)
        with c1:
            st.caption("Fuel")
            d["fuel"] = st.radio(
                "Fuel",
                FUEL_OPTIONS,
                index=FUEL_OPTIONS.index(d.get("fuel") if d.get("fuel") in FUEL_OPTIONS else "Any"),
                horizontal=True,
                label_visibility="collapsed",
                key="disc_fuel_choice",
            )
        with c2:
            st.caption("Body")
            d["body_type"] = st.radio(
                "Body",
                BODY_TYPE_OPTIONS,
                index=BODY_TYPE_OPTIONS.index(d.get("body_type") if d.get("body_type") in BODY_TYPE_OPTIONS else "Any"),
                horizontal=True,
                label_visibility="collapsed",
                key="disc_body_choice",
            )
        with c3:
            st.caption("Gearbox")
            d["transmission"] = st.radio(
                "Gearbox",
                TRANSMISSION_OPTIONS,
                index=TRANSMISSION_OPTIONS.index(
                    d.get("transmission") if d.get("transmission") in TRANSMISSION_OPTIONS else "Any"
                ),
                horizontal=True,
                label_visibility="collapsed",
                key="disc_gear_choice",
            )

    st.markdown("<div class='cf-spacer'></div>", unsafe_allow_html=True)

    with st.container(border=True):
        st.markdown("<div class='cf-panel-title'>Buying limits</div>", unsafe_allow_html=True)
        c1, c2, c3, c4 = st.columns(4)
        with c1:
            d["price_min"] = optional_number("Minimum price", d.get("price_min"), key="disc_price_min")
        with c2:
            d["price_max"] = optional_number("Maximum price", d.get("price_max"), key="disc_price_max")
        with c3:
            d["mileage_max"] = optional_number("Maximum mileage", d.get("mileage_max"), key="disc_mileage")
        with c4:
            d["year_min"] = optional_number("From year", d.get("year_min"), key="disc_year")

        with st.expander("More filters", expanded=False):
            c1, c2 = st.columns(2)
            with c1:
                d["seats_min"] = optional_number(
                    "Minimum seats",
                    d.get("seats_min"),
                    key="disc_seats",
                    help="Unknown seat counts are excluded when this is set.",
                )
            with c2:
                d["power_min"] = optional_number(
                    "Minimum power (PS)",
                    d.get("power_min"),
                    key="disc_power",
                )

    person["discovery"] = d

    valid = True
    if d.get("price_min") is not None and d.get("price_max") is not None and d["price_min"] > d["price_max"]:
        st.error("Minimum price cannot be higher than maximum price.")
        valid = False

    if scope == "preferred":
        chosen = preferred
    elif scope == "selected":
        chosen = selected
    else:
        chosen = makes

    if not chosen:
        st.info("Choose at least one manufacturer to begin.")

    c1, c2, c3 = st.columns([1.35, 1, 3.3])
    if c1.button(
        "Search cars",
        type="primary",
        use_container_width=True,
        disabled=not valid or not chosen,
        key="disc_search",
    ):
        save_person(person)
        with st.spinner(f"Searching {len(chosen)} manufacturer{'s' if len(chosen) != 1 else ''}..."):
            ok, msg = run_mode("discovery", current_username())
        if ok:
            st.success(msg)
            st.rerun()
        else:
            st.error("Discovery search had a problem.")
            st.code(msg)

    if c2.button("Save", use_container_width=True, disabled=not valid, key="disc_save"):
        save_person(person)
        st.success("Discovery saved.")

    rows = discovery_rows(current_username())
    st.markdown(
        f"""
        <div class="cf-results-bar">
          <div><strong>Discovery results</strong><span>{len(rows)} car{'s' if len(rows) != 1 else ''}</span></div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    if rows:
        a1, a2, a3 = st.columns([1.1, 1.1, 3.6])
        if a1.button("Clear results", use_container_width=True, key="disc_clear_results"):
            removed = clear_discovery_results(current_username())
            st.success(f"Cleared Discovery results ({removed} result link{'s' if removed != 1 else ''}).")
            st.rerun()
        if a2.button("Start again", use_container_width=True, key="disc_start_again"):
            reset_discovery(person)
            st.success("Discovery cleared and reset.")
            st.rerun()

        display = pd.DataFrame(rows)
        display["Price"] = display["price_current"].apply(
            lambda x: "" if pd.isna(x) else f"£{int(x):,}"
        )
        display["Mileage"] = display["mileage"].apply(
            lambda x: "" if pd.isna(x) else f"{int(x):,}"
        )
        display["Car"] = display.apply(
            lambda r: " ".join(
                x for x in [str(r.get("make") or ""), str(r.get("model") or ""), str(r.get("trim") or "")]
                if x
            ).strip(),
            axis=1,
        )
        display["Details"] = display.apply(
            lambda r: " · ".join(
                x for x in [
                    str(r.get("fuel") or ""),
                    str(r.get("transmission") or ""),
                    str(r.get("body_type") or ""),
                ] if x
            ),
            axis=1,
        )

        st.dataframe(
            display[["registration", "Car", "Details", "year", "Mileage", "Price", "dealer", "distance_miles"]],
            hide_index=True,
            use_container_width=True,
            column_config={
                "registration": "Registration",
                "year": "Year",
                "dealer": "Dealer",
                "distance_miles": "Miles away",
            },
        )

        labels = {}
        for row in rows:
            price = "" if row.get("price_current") is None else f"£{int(row['price_current']):,}"
            label = f"{row['registration']} · {row.get('make') or ''} {row.get('model') or ''} · {price}".strip()
            labels[label] = row
        c1, c2 = st.columns([3, 1])
        chosen_label = c1.selectbox(
            "Add a result to My Car List",
            list(labels),
            label_visibility="collapsed",
        )
        if c2.button("Add to My Car List", type="primary", use_container_width=True):
            ok, msg = add_result_to_list(person, labels[chosen_label])
            (st.success if ok else st.info)(msg)
            if ok:
                st.rerun()
    else:
        st.markdown(
            """
            <div class="cf-empty-lite">
              No Discovery results yet. Choose your manufacturers and criteria, then search.
            </div>
            """,
            unsafe_allow_html=True,
        )



def _target_summary(target: dict[str, Any], common: dict[str, Any]) -> str:
    bits = [
        target.get("make"),
        target.get("model"),
    ]
    for key in ("body_type", "fuel", "transmission"):
        value = target.get(key)
        if value and value != "Any":
            bits.append(value)
    return " · ".join(str(x) for x in bits if x)


def _effective_limit_summary(target: dict[str, Any], common: dict[str, Any]) -> str:
    ov = target.get("overrides") or {}
    effective = dict(common)
    effective.update(ov)
    bits=[]
    if effective.get("price_min") is not None or effective.get("price_max") is not None:
        low = f"£{int(effective['price_min']):,}" if effective.get("price_min") is not None else "any"
        high = f"£{int(effective['price_max']):,}" if effective.get("price_max") is not None else "any"
        bits.append(f"{low}–{high}")
    if effective.get("mileage_max") is not None:
        bits.append(f"≤ {int(effective['mileage_max']):,} miles")
    if effective.get("year_min") is not None:
        bits.append(f"{int(effective['year_min'])}+")
    return " · ".join(bits) if bits else "Using open buying limits"


def render_target_editor(person: dict[str, Any], target: dict[str, Any], common: dict[str, Any], makes: list[str], index: int) -> dict[str, Any] | None:
    target_id = target.get("id") or f"target-{index}"
    with st.container(border=True):
        top1, top2, top3 = st.columns([4, 1, 1])
        with top1:
            st.markdown(f"### {target.get('name') or target.get('model') or 'Car target'}")
            st.caption(_target_summary(target, common))
            st.caption(_effective_limit_summary(target, common))
        enabled = top2.toggle("Watching", value=bool(target.get("enabled", True)), key=f"target_on_{target_id}")
        remove = top3.button("Remove", key=f"target_remove_{target_id}", use_container_width=True)

        if remove:
            return None

        with st.expander("Edit target", expanded=False):
            c1, c2, c3 = st.columns(3)
            make = c1.selectbox(
                "Make",
                makes,
                index=makes.index(target.get("make")) if target.get("make") in makes else 0,
                key=f"target_make_{target_id}",
            )
            model = c2.text_input("Model", value=target.get("model") or "", key=f"target_model_{target_id}")
            name = c3.text_input("Display name", value=target.get("name") or "", key=f"target_name_{target_id}")

            c1, c2, c3 = st.columns(3)
            body = c1.selectbox(
                "Body", BODY_TYPE_OPTIONS,
                index=BODY_TYPE_OPTIONS.index(target.get("body_type") if target.get("body_type") in BODY_TYPE_OPTIONS else "Any"),
                key=f"target_body_{target_id}",
            )
            fuel = c2.selectbox(
                "Fuel", FUEL_OPTIONS,
                index=FUEL_OPTIONS.index(target.get("fuel") if target.get("fuel") in FUEL_OPTIONS else "Any"),
                key=f"target_fuel_{target_id}",
            )
            gearbox = c3.selectbox(
                "Gearbox", TRANSMISSION_OPTIONS,
                index=TRANSMISSION_OPTIONS.index(target.get("transmission") if target.get("transmission") in TRANSMISSION_OPTIONS else "Any"),
                key=f"target_gear_{target_id}",
            )

            variant = st.text_input(
                "Variant contains",
                value=target.get("variant_text") or "",
                key=f"target_variant_{target_id}",
                placeholder="Optional",
            )

            st.caption("Overrides — leave blank to use the common limit")
            ov = target.get("overrides") or {}
            c1, c2, c3 = st.columns(3)
            with c1:
                pmin = optional_number("Minimum price override", ov.get("price_min"), key=f"target_pmin_{target_id}")
                year = optional_number("From year override", ov.get("year_min"), key=f"target_year_{target_id}")
            with c2:
                pmax = optional_number("Maximum price override", ov.get("price_max"), key=f"target_pmax_{target_id}")
                seats = optional_number("Minimum seats override", ov.get("seats_min"), key=f"target_seats_{target_id}")
            with c3:
                miles = optional_number("Mileage override", ov.get("mileage_max"), key=f"target_miles_{target_id}")
                power = optional_number("Minimum PS override", ov.get("power_min"), key=f"target_power_{target_id}")

        new_overrides = {}
        for key, value in {
            "price_min": pmin if 'pmin' in locals() else ov.get("price_min"),
            "price_max": pmax if 'pmax' in locals() else ov.get("price_max"),
            "mileage_max": miles if 'miles' in locals() else ov.get("mileage_max"),
            "year_min": year if 'year' in locals() else ov.get("year_min"),
            "seats_min": seats if 'seats' in locals() else ov.get("seats_min"),
            "power_min": power if 'power' in locals() else ov.get("power_min"),
        }.items():
            if value is not None:
                new_overrides[key] = value

        updated = dict(target)
        updated.update({
            "enabled": enabled,
            "make": make if 'make' in locals() else target.get("make"),
            "model": model if 'model' in locals() else target.get("model"),
            "name": name if 'name' in locals() else target.get("name"),
            "body_type": body if 'body' in locals() else target.get("body_type"),
            "fuel": fuel if 'fuel' in locals() else target.get("fuel"),
            "transmission": gearbox if 'gearbox' in locals() else target.get("transmission"),
            "variant_text": variant if 'variant' in locals() else target.get("variant_text"),
            "overrides": new_overrides,
        })
        return updated


def render_my_car_list(person: dict[str, Any], makes: list[str]) -> None:
    st.markdown(
        """
        <div class="cf-search-heading">
          <div>
            <div class="cf-search-title">My Car List</div>
            <div class="cf-search-subtitle">The car types you have decided are worth watching.</div>
          </div>
        </div>
        """,
        unsafe_allow_html=True,
    )

    my_list = person.setdefault("my_car_list", {"common_limits": {}, "targets": []})
    common = dict(my_list.get("common_limits") or {})

    with st.expander("Common buying limits", expanded=False):
        st.caption("These apply to every target unless that target has an override.")
        c1, c2, c3 = st.columns(3)
        with c1:
            common["price_min"] = optional_number("Minimum price", common.get("price_min"), key="common_price_min")
            common["year_min"] = optional_number("From year", common.get("year_min"), key="common_year")
        with c2:
            common["price_max"] = optional_number("Maximum price", common.get("price_max"), key="common_price_max")
            common["seats_min"] = optional_number("Minimum seats", common.get("seats_min"), key="common_seats")
        with c3:
            common["mileage_max"] = optional_number("Maximum mileage", common.get("mileage_max"), key="common_mileage")
            common["power_min"] = optional_number("Minimum power (PS)", common.get("power_min"), key="common_power")

    my_list["common_limits"] = common

    targets = list(my_list.get("targets") or [])
    edited_targets = []
    if targets:
        for i, target in enumerate(targets):
            edited = render_target_editor(person, target, common, makes, i)
            if edited is not None:
                edited_targets.append(edited)
    else:
        st.markdown(
            """
            <div class="cf-empty-lite">
              My Car List is empty. Add promising car types from Discovery, or add one manually below.
            </div>
            """,
            unsafe_allow_html=True,
        )

    with st.expander("Add a car type manually", expanded=False):
        c1, c2, c3 = st.columns(3)
        new_make = c1.selectbox("Make", makes, key="new_target_make")
        new_model = c2.text_input("Model", key="new_target_model")
        new_name = c3.text_input("Display name", key="new_target_name", placeholder="Optional")
        c1, c2, c3 = st.columns(3)
        new_body = c1.selectbox("Body", BODY_TYPE_OPTIONS, key="new_target_body")
        new_fuel = c2.selectbox("Fuel", FUEL_OPTIONS, key="new_target_fuel")
        new_gear = c3.selectbox("Gearbox", TRANSMISSION_OPTIONS, key="new_target_gear")
        if st.button("Add target", type="primary", disabled=not text_or_blank(new_model)):
            used_ids = {str(t.get("id") or "") for t in edited_targets}
            target = normalise_target({
                "enabled": True,
                "name": new_name,
                "make": new_make,
                "model": new_model,
                "body_type": new_body,
                "fuel": new_fuel,
                "transmission": new_gear,
                "variant_text": "",
                "overrides": {},
            }, current_username(), used_ids)
            if target:
                edited_targets.append(target)
                my_list["targets"] = edited_targets
                person["my_car_list"] = my_list
                save_target_list(person)
                st.rerun()

    my_list["targets"] = edited_targets
    person["my_car_list"] = my_list

    c1, c2, c3 = st.columns([1.25, 1.15, 2.6])
    if c1.button("Save My Car List", type="primary", use_container_width=True):
        save_target_list(person)
        st.success("My Car List saved.")
    if c2.button(
        "Search My Car List",
        use_container_width=True,
        disabled=not any(t.get("enabled") for t in edited_targets),
    ):
        save_target_list(person)
        with st.spinner("Searching My Car List..."):
            ok, msg = run_mode("targets", current_username())
        if ok:
            st.success(msg)
        else:
            st.error("My Car List search had a problem.")
            st.code(msg)

    with st.expander("List options", expanded=False):
        st.caption("Removing targets does not delete stored vehicle/history data.")
        confirm = st.checkbox("Clear every target from My Car List", key="confirm_clear_list")
        if st.button("Clear My Car List", disabled=not confirm or not edited_targets):
            person["my_car_list"]["targets"] = []
            save_target_list(person)
            st.success("My Car List cleared.")
            st.rerun()


def render_section_navigation() -> str:
    requested = st.session_state.pop("find_cars_section", None)
    if requested in {"Discovery", "My Car List"}:
        st.session_state["find_cars_nav"] = requested
    current = st.session_state.get("find_cars_nav") or "Discovery"

    c1, c2, c3 = st.columns([1, 1, 4])
    if c1.button(
        "Discovery",
        type="primary" if current == "Discovery" else "secondary",
        use_container_width=True,
        key="nav_discovery",
    ):
        current = "Discovery"
        st.session_state["find_cars_nav"] = current
    if c2.button(
        "My Car List",
        type="primary" if current == "My Car List" else "secondary",
        use_container_width=True,
        key="nav_my_list",
    ):
        current = "My Car List"
        st.session_state["find_cars_nav"] = current
    return current


def main() -> None:
    user = current_user()
    if not user:
        st.warning("Please return to CarFinder and log in first.")
        st.stop()

    known_users = {u["username"] for u in list_users()}
    if current_username() not in known_users:
        st.warning("Your login is no longer available. Return to CarFinder and log in again.")
        st.stop()

    st.markdown(
        """
        <div class="cf-hero cf-hero-compact">
          <div class="cf-eyebrow">CarFinder</div>
          <div class="cf-hero-title">Find a car worth buying.</div>
          <p class="cf-hero-copy">
            Search broadly, shortlist the car types that suit you, then let CarFinder keep watching them.
          </p>
        </div>
        """,
        unsafe_allow_html=True,
    )

    settings = load_settings()
    person = person_settings(settings, current_username())
    makes = available_makes()

    section = render_section_navigation()
    render_location(person)

    if section == "Discovery":
        render_discovery(person, makes)
    else:
        render_my_car_list(person, makes)


if __name__ == "__main__":
    main()
