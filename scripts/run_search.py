#!/usr/bin/env python3
"""Run CarFinder v3 searches and merge the results.

Modes:
- targets (default): enabled My Car List targets.
- discovery: the owner's broad Discovery search expanded across the chosen makes.
- all: targets plus Discovery.

Each manufacturer adapter still receives the same effective search shape used
before v3.  Discovery/common-limit/override concepts are resolved before the
adapter is called.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.db import (  # noqa: E402
    add_price_observation,
    add_scrape_result,
    add_vehicle_change,
    connect,
    create_scrape_run,
    finish_scrape_run,
    init_db,
    mark_missing_not_seen,
    upsert_search_link,
    upsert_vehicle,
)
from app.settings import (  # noqa: E402
    discovery_searches,
    enabled_car_searches,
    load_settings,
    person_settings,
    search_settings_for,
    target_searches,
)
from app.sources import (  # noqa: E402
    available_makes,
    fuel_matches,
    source_for_make,
    transmission_matches,
)
from app.users import list_users  # noqa: E402

CACHE_DIR = REPO_ROOT / "data" / "cache"


def new_diagnostics() -> dict[str, Any]:
    return {
        "cars_matched": 0,
        "cars_new": 0,
        "cars_marked_missing": 0,
        "cars_with_dealer_photos": 0,
        "cars_with_stock_photos": 0,
        "registrations_skipped": 0,
        "parsing_errors": 0,
        "runtime_seconds": None,
        "search_fetch_seconds": 0.0,
        "parsing_seconds": 0.0,
        "detail_fetch_count": 0,
        "detail_fetch_seconds": 0.0,
        "detail_fetch_errors": 0,
        "db_write_seconds": 0.0,
        "missing_mark_seconds": 0.0,
        "vehicle_changes": [],
        "photo_decisions": [],
        "photo_status_changes": [],
        "pages": [],
        "searches": [],
        "timing_json": None,
    }


def record_change(diagnostics: dict[str, Any], pending: list[dict[str, Any]], change: dict[str, Any]) -> None:
    diagnostics["vehicle_changes"].append(change)
    pending.append(change)


def _number(value: Any) -> int | None:
    try:
        if value is None or value != value:
            return None
    except Exception:
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def verified_match(row: dict[str, Any], car: dict[str, Any]) -> bool:
    """Final v3 hard-filter check after a manufacturer adapter returns a row.

    If the user explicitly requires a property, unknown data does not count as
    a match.  This is particularly important for body type and minimum seats.
    Manufacturer adapters may still do richer/native filtering first.
    """
    wanted_body = str(car.get("body_type") or "Any")
    if wanted_body != "Any":
        body = str(row.get("body_type") or "").strip()
        if not body or body != wanted_body:
            return False

    wanted_fuel = str(car.get("fuel") or "Any")
    if wanted_fuel != "Any":
        fuel = str(row.get("fuel") or "").strip()
        if not fuel or not fuel_matches(wanted_fuel, fuel):
            return False

    wanted_gear = str(car.get("transmission") or "Any")
    if wanted_gear != "Any":
        gear = str(row.get("transmission") or "").strip()
        if not gear or not transmission_matches(wanted_gear, gear):
            return False

    seats_min = _number(car.get("seats_min"))
    if seats_min is not None:
        seats = _number(row.get("seats"))
        if seats is None or seats < seats_min:
            return False

    for key, row_key, relation in (
        ("price_min", "price", "min"),
        ("price_max", "price", "max"),
        ("mileage_max", "mileage", "max"),
        ("year_min", "year", "min"),
    ):
        wanted = _number(car.get(key))
        if wanted is None:
            continue
        actual = _number(row.get(row_key))
        if actual is None:
            return False
        if relation == "min" and actual < wanted:
            return False
        if relation == "max" and actual > wanted:
            return False

    # power_min is left to the source adapter because power is not yet one of
    # CarFinder's universal result fields.
    return True


def save_rows(
    conn,
    run_id: int,
    car: dict[str, Any],
    rows: list[dict[str, Any]],
    diagnostics: dict[str, Any],
    radius: int = 30,
) -> None:
    """Write one effective search's standard rows to the database."""
    site = car.get("make") or "search"
    for row in rows:
        row.pop("status", None)
        row["car_search_id"] = car["id"]
        row["car_search_name"] = car["name"]
        add_scrape_result(conn, run_id, row)
        pending: list[dict[str, Any]] = []
        reg = row["registration"]

        existing = conn.execute(
            """
            SELECT v.mileage, v.price_current, v.photo_status,
                   (SELECT vs.status FROM vehicle_searches vs
                    WHERE vs.car_search_id = ? AND vs.registration = v.registration) AS status
            FROM vehicles v WHERE v.registration = ?
            """,
            (car["id"], reg),
        ).fetchone()
        if existing is None:
            diagnostics["cars_new"] += 1
            record_change(diagnostics, pending, {
                "registration": reg,
                "change_type": "new",
                "old_value": "",
                "new_value": "active",
                "reason": f"New registration found in {car['name']} search",
            })
        else:
            diagnostics["cars_matched"] += 1
            if (existing["status"] or "active") == "missing":
                record_change(diagnostics, pending, {
                    "registration": reg,
                    "change_type": "returned",
                    "old_value": "missing",
                    "new_value": "active",
                    "reason": f"Registration found again in {car['name']} search",
                })
            old_price, new_price = existing["price_current"], row.get("price")
            if old_price is not None and new_price is not None and int(old_price) != int(new_price):
                record_change(diagnostics, pending, {
                    "registration": reg,
                    "change_type": "price",
                    "old_value": int(old_price),
                    "new_value": int(new_price),
                    "reason": f"{site} price changed",
                })
            old_miles, new_miles = existing["mileage"], row.get("mileage")
            if old_miles is not None and new_miles is not None and int(old_miles) != int(new_miles):
                record_change(diagnostics, pending, {
                    "registration": reg,
                    "change_type": "mileage",
                    "old_value": int(old_miles),
                    "new_value": int(new_miles),
                    "reason": f"{site} mileage changed",
                })
            old_photo = existing["photo_status"] or "unknown"
            new_photo = row.get("photo_status") or "unknown"
            if old_photo != new_photo:
                record_change(diagnostics, pending, {
                    "registration": reg,
                    "change_type": "photo_status",
                    "old_value": old_photo,
                    "new_value": new_photo,
                    "reason": row.get("photo_reason") or f"{site} photo status changed",
                })
                diagnostics["photo_status_changes"].append({
                    "registration": reg,
                    "from": old_photo,
                    "to": new_photo,
                    "reason": row.get("photo_reason") or "",
                })

        upsert_vehicle(conn, row, source="search")
        upsert_search_link(conn, car, row, radius)

        for change in pending:
            add_vehicle_change(
                conn, run_id, change["registration"], change["change_type"],
                change.get("old_value"), change.get("new_value"), change.get("reason"),
            )

        previous_price, current_price = row.get("previous_price"), row.get("price")
        if previous_price is not None and current_price is not None and previous_price != current_price:
            add_price_observation(
                conn, reg, int(previous_price),
                source="previous_price", observed_at=row["last_seen"],
            )
        conn.commit()


def _selected_searches(settings: dict[str, Any], mode: str, owner: str | None) -> list[dict[str, Any]]:
    users = {u["username"] for u in list_users()}
    if owner and owner not in users:
        raise ValueError(f"Unknown user: {owner}")

    searches: list[dict[str, Any]] = []
    if mode in {"targets", "all"}:
        searches.extend(enabled_car_searches(settings, owner))
    if mode in {"discovery", "all"}:
        discovery_owners = [owner] if owner else sorted(users)
        for username in discovery_owners:
            searches.extend(discovery_searches(settings, username, available_makes()))

    return [s for s in searches if s.get("owner") in users]


def _retire_unselected_discovery_links(conn, owner: str, active_ids: set[str]) -> int:
    """Detach old Discovery results for makes no longer in the current scope.

    Existing vehicle rows, price history and reviews are preserved.  Only the
    obsolete Discovery search links are removed.
    """
    prefix = f"{owner}-discovery-%"
    rows = conn.execute(
        "SELECT DISTINCT car_search_id FROM vehicle_searches WHERE owner = ? AND car_search_id LIKE ?",
        (owner, prefix),
    ).fetchall()
    stale = [r["car_search_id"] for r in rows if r["car_search_id"] not in active_ids]
    if not stale:
        return 0
    placeholders = ",".join("?" for _ in stale)
    cur = conn.execute(
        f"DELETE FROM vehicle_searches WHERE owner = ? AND car_search_id IN ({placeholders})",
        (owner, *stale),
    )
    conn.commit()
    return int(cur.rowcount or 0)




def _retire_removed_target_links(conn, settings: dict[str, Any], owner: str | None) -> int:
    """Detach links for My Car List targets that no longer exist.

    Disabled targets are kept because disabling is temporary; removed targets
    are detached. Vehicle/history/review rows are never deleted here.
    """
    users = [owner] if owner else sorted((settings.get("people") or {}).keys())
    removed = 0
    for username in users:
        keep = {c["id"] for c in target_searches(settings, username)}
        rows = conn.execute(
            """
            SELECT DISTINCT car_search_id
            FROM vehicle_searches
            WHERE owner = ? AND car_search_id NOT LIKE ?
            """,
            (username, f"{username}-discovery-%"),
        ).fetchall()
        stale = [r["car_search_id"] for r in rows if r["car_search_id"] not in keep]
        if not stale:
            continue
        placeholders = ",".join("?" for _ in stale)
        cur = conn.execute(
            f"DELETE FROM vehicle_searches WHERE owner = ? AND car_search_id IN ({placeholders})",
            (username, *stale),
        )
        removed += int(cur.rowcount or 0)
    if removed:
        conn.commit()
    return removed



def search_source(module, car: dict[str, Any], settings: dict[str, Any],
                  diagnostics: dict[str, Any]):
    """Run one manufacturer search, with a robust fallback for Fuel=Any.

    A few manufacturer sites return no stock (or reject the request) when the
    fuel filter is omitted even though their fuel-specific searches work.
    For Fuel=Any we therefore try the normal broad request first, then, only
    if it is empty/failed, merge Petrol, Diesel, Hybrid and Electric searches.
    """
    wanted_fuel = str(car.get("fuel") or "Any")
    broad_result = None
    broad_error: Exception | None = None

    try:
        broad_result = module.search(car, settings, diagnostics)
        broad_result.rows = [
            row for row in broad_result.rows
            if verified_match(row, car)
        ]
    except Exception as exc:
        broad_error = exc
        if wanted_fuel != "Any":
            raise

    if wanted_fuel != "Any":
        return broad_result

    if broad_result is not None and broad_result.rows:
        return broad_result

    merged_rows: dict[str, dict[str, Any]] = {}
    merged_raw: set[str] = set()
    debug_html: list[str] = []
    debug_text: list[str] = []
    search_urls: list[str] = []
    successful = 0

    for fuel in ("Petrol", "Diesel", "Hybrid", "Electric"):
        specific = dict(car)
        specific["fuel"] = fuel
        try:
            result = module.search(specific, settings, diagnostics)
        except Exception:
            continue

        successful += 1
        merged_raw.update(result.raw_regs or set())
        if result.debug_html:
            debug_html.append(f"<!-- {fuel} -->\n{result.debug_html}")
        if result.debug_text:
            debug_text.append(f"=== {fuel} ===\n{result.debug_text}")
        if result.search_url:
            search_urls.append(result.search_url)

        for row in result.rows:
            # Check against the original Fuel=Any effective search so every
            # fuel is accepted while the other hard limits still apply.
            if not verified_match(row, car):
                continue
            key = str(row.get("registration") or row.get("url") or "").strip()
            if not key:
                continue
            merged_rows[key] = row

    if successful:
        if broad_result is None:
            # Reuse a successful result object shape without importing or
            # coupling the runner to the SearchResult dataclass constructor.
            specific = dict(car)
            specific["fuel"] = "Petrol"
            try:
                broad_result = module.search(specific, settings, diagnostics)
            except Exception:
                specific["fuel"] = "Diesel"
                broad_result = module.search(specific, settings, diagnostics)

        broad_result.rows = list(merged_rows.values())
        broad_result.raw_regs = merged_raw
        broad_result.debug_html = "\n".join(debug_html)
        broad_result.debug_text = "\n".join(debug_text)
        broad_result.search_url = " | ".join(dict.fromkeys(search_urls))
        return broad_result

    if broad_result is not None:
        return broad_result
    if broad_error is not None:
        raise broad_error
    raise RuntimeError(f"{car.get('make')}: Fuel=Any search returned no usable response")

def run_search(mode: str = "targets", owner: str | None = None) -> dict[str, Any]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    settings = load_settings()
    cars = _selected_searches(settings, mode, owner)

    conn = connect()
    init_db(conn)

    if mode in {"targets", "all"}:
        _retire_removed_target_links(conn, settings, owner)

    if mode in {"discovery", "all"}:
        owners = {c["owner"] for c in cars if c.get("search_kind") == "discovery"}
        if owner:
            owners.add(owner)
        for username in owners:
            active_ids = {
                c["id"] for c in cars
                if c.get("owner") == username and c.get("search_kind") == "discovery"
            }
            _retire_unselected_discovery_links(conn, username, active_ids)

    summary_url = "; ".join(
        f"{c['owner']}: {c['name']} ({c['make']})" for c in cars
    ) or f"No {mode} searches enabled"
    run_id = create_scrape_run(conn, summary_url)
    diagnostics = new_diagnostics()
    total_rows = 0
    failures: list[str] = []

    try:
        for car in cars:
            search_info: dict[str, Any] = {
                "search": car["name"],
                "owner": car.get("owner"),
                "make": car["make"],
                "kind": car.get("search_kind") or "target",
                "status": "complete",
            }
            diagnostics["searches"].append(search_info)
            module = source_for_make(car.get("make"))
            if module is None:
                search_info.update(status="skipped", message=f"No search module for {car.get('make')} yet")
                failures.append(f"{car['name']}: no module for {car.get('make')}")
                continue

            try:
                result = search_source(
                    module,
                    car,
                    search_settings_for(settings, car.get("owner") or ""),
                    diagnostics,
                )
            except Exception as exc:
                # One failing site must not stop the others. Its previous
                # active links are left alone because mark_missing_not_seen
                # is only called after a successful source response.
                search_info.update(status="failed", message=str(exc))
                failures.append(f"{car['name']}: {exc}")
                continue

            search_info["url"] = result.search_url
            search_info["cars_found"] = len(result.rows)
            total_rows += len(result.rows)

            cache_name = f"{module.SOURCE_KEY}_{car['id']}_last"
            (CACHE_DIR / f"{cache_name}.html").write_text(result.debug_html, encoding="utf-8")
            (CACHE_DIR / f"{cache_name}.txt").write_text(result.debug_text, encoding="utf-8")

            parsed_regs = {row["registration"] for row in result.rows}
            diagnostics["registrations_skipped"] += len(result.raw_regs - parsed_regs)
            diagnostics["cars_with_dealer_photos"] += sum(
                1 for r in result.rows if r.get("photo_status") == "photos"
            )
            diagnostics["cars_with_stock_photos"] += sum(
                1 for r in result.rows if r.get("photo_status") == "awaiting"
            )
            diagnostics["photo_decisions"].extend(
                {
                    "registration": r.get("registration"),
                    "search": car["name"],
                    "photo_status": r.get("photo_status"),
                    "photo_count": r.get("photo_count"),
                    "reason": r.get("photo_reason") or "",
                }
                for r in sorted(
                    result.rows,
                    key=lambda r: (
                        str(r.get("photo_status") or ""),
                        str(r.get("registration") or ""),
                    ),
                )
            )

            db_started = time.perf_counter()
            radius = int(
                person_settings(settings, car.get("owner") or "").get("local_radius_miles") or 30
            )
            save_rows(conn, run_id, car, result.rows, diagnostics, radius)
            diagnostics["db_write_seconds"] = round(
                diagnostics["db_write_seconds"] + time.perf_counter() - db_started, 3
            )

            missing_started = time.perf_counter()
            # raw_regs represents registrations returned by the source before
            # parsing/filtering.  For a v3 hard filter, rows filtered out by
            # verified_match should count as not seen for this effective search.
            seen = parsed_regs
            if result.raw_regs or parsed_regs:
                marked = mark_missing_not_seen(conn, sorted(seen), car["id"])
                diagnostics["cars_marked_missing"] += len(marked)
                search_info["marked_missing"] = len(marked)
                for reg in marked:
                    reason = f"Registration not seen in latest {car['name']} search"
                    diagnostics["vehicle_changes"].append({
                        "registration": reg,
                        "change_type": "missing",
                        "old_value": "active",
                        "new_value": "missing",
                        "reason": reason,
                    })
                    add_vehicle_change(
                        conn, run_id, reg, "missing", "active", "missing", reason
                    )
                conn.commit()
            diagnostics["missing_mark_seconds"] = round(
                diagnostics["missing_mark_seconds"] + time.perf_counter() - missing_started, 3
            )

        reachability_counts = {"LOCAL": 0, "TRANSFERABLE": 0, "REMOTE": 0}
        for link in conn.execute(
            "SELECT reachability_status FROM vehicle_searches WHERE status = 'active'"
        ):
            status = str(link["reachability_status"] or "REMOTE").upper()
            reachability_counts[status if status in reachability_counts else "REMOTE"] += 1
        diagnostics["reachability_counts"] = reachability_counts

        diagnostics["runtime_seconds"] = round(time.perf_counter() - started, 2)
        diagnostics["timing_json"] = json.dumps({
            key: diagnostics.get(key)
            for key in (
                "search_fetch_seconds", "parsing_seconds", "detail_fetch_count",
                "detail_fetch_seconds", "detail_fetch_errors", "db_write_seconds",
                "missing_mark_seconds", "pages", "photo_status_changes",
                "photo_decisions", "vehicle_changes", "reachability_counts", "searches",
            )
        }, ensure_ascii=False)

        if not cars:
            message = (
                "No My Car List targets are enabled."
                if mode == "targets"
                else "Discovery has no manufacturers selected."
            )
        else:
            message = (
                f"Ran {len(cars)} {mode} search(es). Found {total_rows} car(s). "
                f"Matched {diagnostics['cars_matched']}, new {diagnostics['cars_new']}, "
                f"missing {diagnostics['cars_marked_missing']}, "
                f"skipped {diagnostics['registrations_skipped']}."
            )
        if failures:
            message += " Problems: " + " | ".join(failures)
        status = "complete" if not failures else ("failed" if cars and len(failures) == len(cars) else "partial")
        finish_scrape_run(conn, run_id, total_rows, status, message, diagnostics)
        diagnostics.update(cars_found=total_rows, message=message, status=status)
        return diagnostics

    except Exception as exc:
        diagnostics["runtime_seconds"] = round(time.perf_counter() - started, 2)
        finish_scrape_run(conn, run_id, total_rows, "failed", str(exc), diagnostics)
        raise
    finally:
        conn.close()


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--mode",
        choices=("targets", "discovery", "all"),
        default="targets",
        help="Which v3 search set to run.",
    )
    parser.add_argument(
        "--owner",
        default=None,
        help="Optional username. Required by the Find Cars page for a personal Discovery run.",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    diagnostics = run_search(args.mode, args.owner)
    print("CarFinder search complete.")
    print(diagnostics.get("message") or "")
    print(f"Dealer photos: {diagnostics.get('cars_with_dealer_photos', 0)}")
    print(f"Stock/awaiting photos: {diagnostics.get('cars_with_stock_photos', 0)}")
    counts = diagnostics.get("reachability_counts") or {}
    if counts:
        print(
            "Reachability: "
            f"local {counts.get('LOCAL', 0)}, "
            f"transferable {counts.get('TRANSFERABLE', 0)}, "
            f"remote {counts.get('REMOTE', 0)}"
        )
    print(f"Runtime: {diagnostics.get('runtime_seconds', 0)} seconds")
    return 1 if diagnostics.get("status") == "failed" else 0


if __name__ == "__main__":
    sys.exit(main())
