#!/usr/bin/env python3
"""Run every enabled car search from Settings and merge the results.

Each car search is handled by the source module for its make (see
``app/sources``).  All modules return the same standard listing format, so the
database update below is the same whichever site a car came from.
"""
from __future__ import annotations

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
    get_vehicles,
    init_db,
    mark_missing_not_seen,
    upsert_vehicle,
)
from app.settings import enabled_car_searches, load_settings  # noqa: E402
from app.sources import source_for_make  # noqa: E402

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


def save_rows(conn, run_id: int, car: dict[str, Any], rows: list[dict[str, Any]], diagnostics: dict[str, Any]) -> None:
    """Write one search's standard rows to the database."""
    site = car.get("make") or "search"
    for row in rows:
        row["car_search_id"] = car["id"]
        row["car_search_name"] = car["name"]
        add_scrape_result(conn, run_id, row)
        pending: list[dict[str, Any]] = []
        reg = row["registration"]

        existing = conn.execute(
            "SELECT status, mileage, price_current, photo_status FROM vehicles WHERE registration = ?",
            (reg,),
        ).fetchone()
        if existing is None:
            diagnostics["cars_new"] += 1
            record_change(diagnostics, pending, {
                "registration": reg, "change_type": "new", "old_value": "", "new_value": "active",
                "reason": f"New registration found in {car['name']} search",
            })
        else:
            diagnostics["cars_matched"] += 1
            if (existing["status"] or "active") == "missing":
                record_change(diagnostics, pending, {
                    "registration": reg, "change_type": "returned", "old_value": "missing", "new_value": "active",
                    "reason": f"Registration found again in {car['name']} search",
                })
            old_price, new_price = existing["price_current"], row.get("price")
            if old_price is not None and new_price is not None and int(old_price) != int(new_price):
                record_change(diagnostics, pending, {
                    "registration": reg, "change_type": "price", "old_value": int(old_price), "new_value": int(new_price),
                    "reason": f"{site} price changed",
                })
            old_miles, new_miles = existing["mileage"], row.get("mileage")
            if old_miles is not None and new_miles is not None and int(old_miles) != int(new_miles):
                record_change(diagnostics, pending, {
                    "registration": reg, "change_type": "mileage", "old_value": int(old_miles), "new_value": int(new_miles),
                    "reason": f"{site} mileage changed",
                })
            old_photo = existing["photo_status"] or "unknown"
            new_photo = row.get("photo_status") or "unknown"
            if old_photo != new_photo:
                record_change(diagnostics, pending, {
                    "registration": reg, "change_type": "photo_status", "old_value": old_photo, "new_value": new_photo,
                    "reason": row.get("photo_reason") or f"{site} photo status changed",
                })
                diagnostics["photo_status_changes"].append({
                    "registration": reg, "from": old_photo, "to": new_photo, "reason": row.get("photo_reason") or "",
                })

        # The vehicle must exist before related history rows are inserted
        # (price_history and vehicle_change_log have foreign keys).
        upsert_vehicle(conn, row, source="search")

        for change in pending:
            add_vehicle_change(conn, run_id, change["registration"], change["change_type"],
                               change.get("old_value"), change.get("new_value"), change.get("reason"))

        previous_price, current_price = row.get("previous_price"), row.get("price")
        if previous_price is not None and current_price is not None and previous_price != current_price:
            add_price_observation(conn, reg, int(previous_price), source="previous_price", observed_at=row["last_seen"])

        conn.commit()


def run_search() -> dict[str, Any]:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    started = time.perf_counter()
    settings = load_settings()
    cars = enabled_car_searches(settings)

    conn = connect()
    init_db(conn)
    summary_url = "; ".join(f"{c['name']} ({c['make']})" for c in cars) or "No car searches enabled"
    run_id = create_scrape_run(conn, summary_url)
    diagnostics = new_diagnostics()
    total_rows = 0
    failures: list[str] = []

    try:
        for car in cars:
            search_info: dict[str, Any] = {"search": car["name"], "make": car["make"], "status": "complete"}
            diagnostics["searches"].append(search_info)
            module = source_for_make(car.get("make"))
            if module is None:
                search_info.update(status="skipped", message=f"No search module for {car.get('make')} yet")
                failures.append(f"{car['name']}: no module for {car.get('make')}")
                continue

            try:
                result = module.search(car, settings, diagnostics)
            except Exception as exc:  # one failing site must not stop the others
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
            diagnostics["cars_with_dealer_photos"] += sum(1 for r in result.rows if r.get("photo_status") == "photos")
            diagnostics["cars_with_stock_photos"] += sum(1 for r in result.rows if r.get("photo_status") == "awaiting")
            diagnostics["photo_decisions"].extend(
                {
                    "registration": r.get("registration"),
                    "search": car["name"],
                    "photo_status": r.get("photo_status"),
                    "photo_count": r.get("photo_count"),
                    "reason": r.get("photo_reason") or "",
                }
                for r in sorted(result.rows, key=lambda r: (str(r.get("photo_status") or ""), str(r.get("registration") or "")))
            )

            db_started = time.perf_counter()
            save_rows(conn, run_id, car, result.rows, diagnostics)
            diagnostics["db_write_seconds"] = round(diagnostics["db_write_seconds"] + time.perf_counter() - db_started, 3)

            # Only mark this search's cars missing when the search clearly
            # worked (at least one registration seen in the page data).
            missing_started = time.perf_counter()
            seen = parsed_regs | result.raw_regs
            if seen:
                marked = mark_missing_not_seen(conn, sorted(seen), car["id"])
                diagnostics["cars_marked_missing"] += len(marked)
                search_info["marked_missing"] = len(marked)
                for reg in marked:
                    reason = f"Registration not seen in latest {car['name']} search"
                    diagnostics["vehicle_changes"].append({
                        "registration": reg, "change_type": "missing", "old_value": "active",
                        "new_value": "missing", "reason": reason,
                    })
                    add_vehicle_change(conn, run_id, reg, "missing", "active", "missing", reason)
                conn.commit()
            diagnostics["missing_mark_seconds"] = round(
                diagnostics["missing_mark_seconds"] + time.perf_counter() - missing_started, 3
            )

        reachability_counts = {"LOCAL": 0, "TRANSFERABLE": 0, "REMOTE": 0}
        for vehicle in get_vehicles(conn):
            status = str(vehicle.get("reachability_status") or "REMOTE").upper()
            reachability_counts[status if status in reachability_counts else "REMOTE"] += 1
        diagnostics["reachability_counts"] = reachability_counts

        diagnostics["runtime_seconds"] = round(time.perf_counter() - started, 2)
        diagnostics["timing_json"] = json.dumps({
            key: diagnostics.get(key)
            for key in (
                "search_fetch_seconds", "parsing_seconds", "detail_fetch_count", "detail_fetch_seconds",
                "detail_fetch_errors", "db_write_seconds", "missing_mark_seconds", "pages",
                "photo_status_changes", "photo_decisions", "vehicle_changes", "reachability_counts", "searches",
            )
        }, ensure_ascii=False)

        if not cars:
            message = "No car searches are enabled. Add one in ⚙ Settings."
        else:
            message = (
                f"Searched {len(cars)} car search(es). Found {total_rows} car(s). "
                f"Matched {diagnostics['cars_matched']}, new {diagnostics['cars_new']}, "
                f"missing {diagnostics['cars_marked_missing']}, skipped {diagnostics['registrations_skipped']}."
            )
        if failures:
            message += " Problems: " + " | ".join(failures)
        status = "complete" if not failures else ("failed" if len(failures) == len(cars) else "partial")
        finish_scrape_run(conn, run_id, total_rows, status, message, diagnostics)
        diagnostics.update(cars_found=total_rows, message=message, status=status)
        return diagnostics

    except Exception as exc:
        diagnostics["runtime_seconds"] = round(time.perf_counter() - started, 2)
        finish_scrape_run(conn, run_id, total_rows, "failed", str(exc), diagnostics)
        raise

    finally:
        conn.close()


def main() -> int:
    diagnostics = run_search()
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
