from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from app.reachability import calculate_reachability, seed_dealer_reachability

APP_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = APP_ROOT / "data"
# CarFinder v2 starts a clean database. The old Polo database
# (vw_polo_tracker.db) is left untouched as an archive.
DB_PATH = DATA_DIR / "carfinder.db"
MASTER_STATE_PATH = DATA_DIR / "master_state.json"

VALID_SENSOR_STATUS = {"unknown", "none", "single", "front_rear"}
VALID_SENSOR_DETAIL = {"unknown", "front_only", "rear_only", "front_rear", "none"}
VALID_VEHICLE_STATUS = {"active", "sold", "missing", "rejected"}
VALID_PHOTO_STATUS = {"unknown", "photos", "awaiting"}
VALID_INTEREST_STATUS = {"interested", "rejected"}
VALID_REACHABILITY_STATUS = {"LOCAL", "TRANSFERABLE", "REMOTE"}


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS vehicles (
            registration TEXT PRIMARY KEY,
            status TEXT NOT NULL DEFAULT 'active',
            sensor_status TEXT NOT NULL DEFAULT 'unknown',
            sensor_detail TEXT NOT NULL DEFAULT 'unknown',
            checked INTEGER NOT NULL DEFAULT 0,

            year INTEGER,
            colour TEXT,
            trim TEXT,
            mileage INTEGER,
            price_current INTEGER,
            dealer TEXT,
            location TEXT,
            distance_miles INTEGER,
            photo_status TEXT NOT NULL DEFAULT 'unknown',
            url TEXT,
            interest_status TEXT,
            interest_date TEXT,
            interest_reason TEXT,
            reachability_status TEXT NOT NULL DEFAULT 'REMOTE',
            dealer_group_id INTEGER,
            dealer_group_name TEXT,
            nearest_branch_name TEXT,
            nearest_branch_distance_miles INTEGER,
            reachability_reason TEXT,
            reachability_updated_at TEXT,
            car_search_id TEXT,
            car_search_name TEXT,
            make TEXT,
            model TEXT,
            fuel TEXT,
            transmission TEXT,
            source TEXT,
            body_type TEXT,
            seats INTEGER,

            notes TEXT,
            first_seen TEXT,
            last_seen TEXT,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            registration TEXT NOT NULL,
            observed_at TEXT NOT NULL,
            price INTEGER NOT NULL,
            source TEXT NOT NULL DEFAULT 'vw_search',
            FOREIGN KEY (registration) REFERENCES vehicles(registration) ON DELETE CASCADE
        );

        CREATE UNIQUE INDEX IF NOT EXISTS idx_price_history_unique
        ON price_history (registration, observed_at, price, source);

        CREATE TABLE IF NOT EXISTS scrape_runs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            started_at TEXT NOT NULL,
            completed_at TEXT,
            search_url TEXT NOT NULL,
            cars_found INTEGER NOT NULL DEFAULT 0,
            cars_matched INTEGER NOT NULL DEFAULT 0,
            cars_new INTEGER NOT NULL DEFAULT 0,
            cars_marked_missing INTEGER NOT NULL DEFAULT 0,
            cars_with_dealer_photos INTEGER NOT NULL DEFAULT 0,
            cars_with_stock_photos INTEGER NOT NULL DEFAULT 0,
            registrations_skipped INTEGER NOT NULL DEFAULT 0,
            parsing_errors INTEGER NOT NULL DEFAULT 0,
            runtime_seconds REAL,
            search_fetch_seconds REAL,
            parsing_seconds REAL,
            detail_fetch_count INTEGER NOT NULL DEFAULT 0,
            detail_fetch_seconds REAL,
            detail_fetch_errors INTEGER NOT NULL DEFAULT 0,
            db_write_seconds REAL,
            missing_mark_seconds REAL,
            timing_json TEXT,
            status TEXT NOT NULL DEFAULT 'running',
            message TEXT
        );

        CREATE TABLE IF NOT EXISTS scrape_results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER NOT NULL,
            registration TEXT NOT NULL,
            raw_text TEXT,
            url TEXT,
            seen_price INTEGER,
            seen_mileage INTEGER,
            seen_colour TEXT,
            seen_dealer TEXT,
            seen_distance_miles INTEGER,
            FOREIGN KEY (run_id) REFERENCES scrape_runs(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS vehicle_change_log (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            run_id INTEGER,
            registration TEXT NOT NULL,
            changed_at TEXT NOT NULL,
            change_type TEXT NOT NULL,
            old_value TEXT,
            new_value TEXT,
            reason TEXT,
            FOREIGN KEY (run_id) REFERENCES scrape_runs(id) ON DELETE SET NULL,
            FOREIGN KEY (registration) REFERENCES vehicles(registration) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS dealer_reachability_settings (
            setting_key TEXT PRIMARY KEY,
            setting_value TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS dealer_groups (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            name TEXT NOT NULL UNIQUE,
            aliases_json TEXT,
            notes TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS dealer_branches (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            dealer_group_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            town TEXT,
            postcode TEXT,
            distance_miles INTEGER,
            notes TEXT,
            is_active INTEGER NOT NULL DEFAULT 1,
            updated_at TEXT NOT NULL,
            FOREIGN KEY (dealer_group_id) REFERENCES dealer_groups(id) ON DELETE CASCADE,
            UNIQUE (dealer_group_id, name)
        );

        CREATE INDEX IF NOT EXISTS idx_dealer_branches_group_distance
        ON dealer_branches (dealer_group_id, is_active, distance_miles);

        CREATE INDEX IF NOT EXISTS idx_vehicle_change_log_run
        ON vehicle_change_log (run_id, registration, change_type);

        CREATE INDEX IF NOT EXISTS idx_vehicle_change_log_reg
        ON vehicle_change_log (registration, changed_at DESC);
        """
    )

    # v2.0.1: which car search (and so which person) found each car, with the
    # distance and dealer route from that person's postcode, plus each
    # person's own review of the car.
    had_links = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'vehicle_searches'"
    ).fetchone() is not None
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS vehicle_searches (
            car_search_id TEXT NOT NULL,
            registration TEXT NOT NULL,
            owner TEXT NOT NULL,
            car_search_name TEXT,
            status TEXT NOT NULL DEFAULT 'active',
            distance_miles INTEGER,
            reachability_status TEXT NOT NULL DEFAULT 'REMOTE',
            dealer_group_id INTEGER,
            dealer_group_name TEXT,
            nearest_branch_name TEXT,
            nearest_branch_distance_miles INTEGER,
            reachability_reason TEXT,
            first_seen TEXT,
            last_seen TEXT,
            PRIMARY KEY (car_search_id, registration),
            FOREIGN KEY (registration) REFERENCES vehicles(registration) ON DELETE CASCADE
        );

        CREATE INDEX IF NOT EXISTS idx_vehicle_searches_owner
        ON vehicle_searches (owner, status);

        CREATE TABLE IF NOT EXISTS vehicle_reviews (
            owner TEXT NOT NULL,
            registration TEXT NOT NULL,
            interest_status TEXT,
            interest_date TEXT,
            interest_reason TEXT,
            notes TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY (owner, registration),
            FOREIGN KEY (registration) REFERENCES vehicles(registration) ON DELETE CASCADE
        );
        """
    )
    if not had_links:
        # v2.0.0 marked missing on the shared car row; that now lives on the
        # per-search link, which the next search rebuilds.
        conn.execute("UPDATE vehicles SET status = 'active' WHERE status = 'missing'")

    # Lightweight migrations for existing databases.
    vehicle_columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(vehicles)").fetchall()
    }
    vehicle_migrations = {
        "photo_status": "TEXT NOT NULL DEFAULT 'unknown'",
        "interest_status": "TEXT",
        "interest_date": "TEXT",
        "interest_reason": "TEXT",
        "reachability_status": "TEXT NOT NULL DEFAULT 'REMOTE'",
        "dealer_group_id": "INTEGER",
        "dealer_group_name": "TEXT",
        "nearest_branch_name": "TEXT",
        "nearest_branch_distance_miles": "INTEGER",
        "reachability_reason": "TEXT",
        "reachability_updated_at": "TEXT",
        "car_search_id": "TEXT",
        "car_search_name": "TEXT",
        "make": "TEXT",
        "model": "TEXT",
        "fuel": "TEXT",
        "transmission": "TEXT",
        "source": "TEXT",
        "body_type": "TEXT",
        "seats": "INTEGER",
    }
    for column_name, column_type in vehicle_migrations.items():
        if column_name not in vehicle_columns:
            conn.execute(f"ALTER TABLE vehicles ADD COLUMN {column_name} {column_type}")

    scrape_run_columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(scrape_runs)").fetchall()
    }
    scrape_run_migrations = {
        "cars_matched": "INTEGER NOT NULL DEFAULT 0",
        "cars_new": "INTEGER NOT NULL DEFAULT 0",
        "cars_marked_missing": "INTEGER NOT NULL DEFAULT 0",
        "cars_with_dealer_photos": "INTEGER NOT NULL DEFAULT 0",
        "cars_with_stock_photos": "INTEGER NOT NULL DEFAULT 0",
        "registrations_skipped": "INTEGER NOT NULL DEFAULT 0",
        "parsing_errors": "INTEGER NOT NULL DEFAULT 0",
        "runtime_seconds": "REAL",
        "search_fetch_seconds": "REAL",
        "parsing_seconds": "REAL",
        "detail_fetch_count": "INTEGER NOT NULL DEFAULT 0",
        "detail_fetch_seconds": "REAL",
        "detail_fetch_errors": "INTEGER NOT NULL DEFAULT 0",
        "db_write_seconds": "REAL",
        "missing_mark_seconds": "REAL",
        "timing_json": "TEXT",
    }
    for column_name, column_type in scrape_run_migrations.items():
        if column_name not in scrape_run_columns:
            conn.execute(f"ALTER TABLE scrape_runs ADD COLUMN {column_name} {column_type}")

    seed_dealer_reachability(conn)
    refresh_search_reachability(conn)

    conn.commit()


def normalise_reg(registration: str) -> str:
    return " ".join((registration or "").upper().replace("-", " ").split())


def normalise_sensor_status(value: str | None) -> str:
    value = (value or "unknown").strip().lower()
    if value in {"front+rear", "front and rear", "both", "full"}:
        return "front_rear"
    if value in {"rear_only", "front_only", "single", "rear", "front"}:
        return "single"
    if value in {"no", "none", "no_sensors"}:
        return "none"
    return value if value in VALID_SENSOR_STATUS else "unknown"


def normalise_sensor_detail(value: str | None, sensor_status: str) -> str:
    value = (value or "unknown").strip().lower()
    if sensor_status == "front_rear":
        return "front_rear"
    if sensor_status == "none":
        return "none"
    if value in VALID_SENSOR_DETAIL:
        return value
    return "unknown"


def normalise_status(value: str | None) -> str:
    value = (value or "active").strip().lower()
    return value if value in VALID_VEHICLE_STATUS else "active"


def normalise_photo_status(value: str | None) -> str:
    value = (value or "unknown").strip().lower()
    if value in {"real", "real_photos", "multiple", "multi", "yes", "photos"}:
        return "photos"
    if value in {"stock", "stock_only", "single", "one", "1", "no_photos", "awaiting"}:
        return "awaiting"
    return value if value in VALID_PHOTO_STATUS else "unknown"


def normalise_interest_status(value: str | None) -> str | None:
    value = (value or "").strip().lower()
    if value in {"interested", "yes", "true", "1", "look", "enquire"}:
        return "interested"
    if value in {"rejected", "not_suitable", "not suitable", "no", "false", "0"}:
        return "rejected"
    return None


def normalise_reachability_status(value: str | None) -> str:
    value = (value or "REMOTE").strip().upper()
    return value if value in VALID_REACHABILITY_STATUS else "REMOTE"


def update_vehicle_reachability(conn: sqlite3.Connection, registration: str) -> None:
    reg = normalise_reg(registration)
    if not reg:
        return
    row = conn.execute("SELECT * FROM vehicles WHERE registration = ?", (reg,)).fetchone()
    if row is None:
        return

    result = calculate_reachability(conn, row)
    conn.execute(
        """
        UPDATE vehicles
        SET reachability_status = ?,
            dealer_group_id = ?,
            dealer_group_name = ?,
            nearest_branch_name = ?,
            nearest_branch_distance_miles = ?,
            reachability_reason = ?,
            reachability_updated_at = ?
        WHERE registration = ?
        """,
        (
            normalise_reachability_status(result.status),
            result.dealer_group_id,
            result.dealer_group_name,
            result.nearest_branch_name,
            result.nearest_branch_distance_miles,
            result.reason,
            now_iso(),
            reg,
        ),
    )


def refresh_all_vehicle_reachability(conn: sqlite3.Connection) -> int:
    """Backwards-compatible name: reachability now lives on each search link."""
    return refresh_search_reachability(conn)


def _owner_radius(owner: str, cache: dict[str, int]) -> int:
    if owner not in cache:
        from app.settings import load_settings, person_settings

        cache[owner] = int(person_settings(load_settings(), owner).get("local_radius_miles") or 30)
    return cache[owner]


def _apply_link_reachability(conn: sqlite3.Connection, car_search_id: str, registration: str, owner: str,
                             distance: Any, dealer: Any, radius: int) -> None:
    result = calculate_reachability(conn, {"distance_miles": distance, "dealer": dealer}, radius)
    conn.execute(
        """
        UPDATE vehicle_searches
        SET reachability_status = ?, dealer_group_id = ?, dealer_group_name = ?,
            nearest_branch_name = ?, nearest_branch_distance_miles = ?, reachability_reason = ?
        WHERE car_search_id = ? AND registration = ?
        """,
        (
            normalise_reachability_status(result.status), result.dealer_group_id, result.dealer_group_name,
            result.nearest_branch_name, result.nearest_branch_distance_miles, result.reason,
            car_search_id, registration,
        ),
    )


def refresh_search_reachability(conn: sqlite3.Connection) -> int:
    """Recalculate L/T/R for every search link using each owner's radius."""
    rows = conn.execute(
        """
        SELECT vs.car_search_id, vs.registration, vs.owner, vs.distance_miles, v.dealer
        FROM vehicle_searches vs JOIN vehicles v ON v.registration = vs.registration
        """
    ).fetchall()
    cache: dict[str, int] = {}
    for row in rows:
        _apply_link_reachability(conn, row["car_search_id"], row["registration"], row["owner"],
                                 row["distance_miles"], row["dealer"], _owner_radius(row["owner"], cache))
    return len(rows)


def upsert_search_link(conn: sqlite3.Connection, car: dict[str, Any], row: dict[str, Any], radius: int) -> bool:
    """Record that ``car`` (a car search) found this car. Returns True if the link is new."""
    reg = normalise_reg(row.get("registration") or "")
    if not reg:
        return False
    t = row.get("last_seen") or now_iso()
    existing = conn.execute(
        "SELECT status FROM vehicle_searches WHERE car_search_id = ? AND registration = ?",
        (car["id"], reg),
    ).fetchone()
    if existing is None:
        conn.execute(
            """
            INSERT INTO vehicle_searches (car_search_id, registration, owner, car_search_name, status,
                                          distance_miles, first_seen, last_seen)
            VALUES (?, ?, ?, ?, 'active', ?, ?, ?)
            """,
            (car["id"], reg, car.get("owner") or "", car.get("name"), row.get("distance_miles"), t, t),
        )
    else:
        conn.execute(
            """
            UPDATE vehicle_searches
            SET status = 'active', owner = ?, car_search_name = ?,
                distance_miles = COALESCE(?, distance_miles), last_seen = ?
            WHERE car_search_id = ? AND registration = ?
            """,
            (car.get("owner") or "", car.get("name"), row.get("distance_miles"), t, car["id"], reg),
        )
    _apply_link_reachability(conn, car["id"], reg, car.get("owner") or "", row.get("distance_miles"),
                             row.get("dealer"), radius)
    return existing is None


def upsert_vehicle(conn: sqlite3.Connection, data: dict[str, Any], source: str = "manual") -> None:
    reg = normalise_reg(data.get("registration") or data.get("reg") or "")
    if not reg:
        return

    t = now_iso()
    sensor_status = normalise_sensor_status(data.get("sensor_status"))
    sensor_detail = normalise_sensor_detail(data.get("sensor_detail"), sensor_status)
    status = normalise_status(data.get("status"))
    photo_status = normalise_photo_status(data.get("photo_status"))
    checked = int(bool(data.get("checked", sensor_status != "unknown")))

    existing = conn.execute("SELECT * FROM vehicles WHERE registration = ?", (reg,)).fetchone()

    if existing is None:
        conn.execute(
            """
            INSERT INTO vehicles (
                registration, status, sensor_status, sensor_detail, checked,
                year, colour, trim, mileage, price_current, dealer, location,
                distance_miles, photo_status, url, interest_status, interest_date,
                interest_reason, reachability_status, car_search_id, car_search_name,
                make, model, fuel, transmission, source, body_type, seats, notes, first_seen, last_seen, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                reg,
                status,
                sensor_status,
                sensor_detail,
                checked,
                data.get("year"),
                data.get("colour"),
                data.get("trim"),
                data.get("mileage"),
                data.get("price_current") or data.get("price"),
                data.get("dealer"),
                data.get("location"),
                data.get("distance_miles"),
                photo_status,
                data.get("url"),
                normalise_interest_status(data.get("interest_status")),
                data.get("interest_date"),
                data.get("interest_reason"),
                normalise_reachability_status(data.get("reachability_status")),
                data.get("car_search_id"),
                data.get("car_search_name"),
                data.get("make"),
                data.get("model"),
                data.get("fuel"),
                data.get("transmission"),
                data.get("source") or source,
                data.get("body_type"),
                data.get("seats"),
                data.get("notes"),
                data.get("first_seen") or t,
                data.get("last_seen") or t,
                t,
            ),
        )
    else:
        # Preserve manually curated sensor/status values unless explicitly supplied.
        new_status = status if data.get("status") is not None else existing["status"]
        if existing["status"] == "sold" and source in {"vw_search", "search"}:
            new_status = "sold"

        # Do not downgrade a useful photo status when VW omits/churns the media
        # field on a later scrape.  Only a definite "photos" value upgrades the
        # record; "unknown" never replaces an existing awaiting/photos value.
        existing_photo_status = existing["photo_status"] or "unknown"
        incoming_photo_status = photo_status if data.get("photo_status") is not None else None
        if incoming_photo_status == "unknown":
            new_photo_status = existing_photo_status
        else:
            # A definite VW result should replace the previous photo state.  In
            # particular, allow photos -> awaiting so cars that only have a
            # stock/1-of-1 image are corrected on the next scrape.
            new_photo_status = incoming_photo_status or existing_photo_status

        conn.execute(
            """
            UPDATE vehicles SET
                status = ?,
                sensor_status = COALESCE(?, sensor_status),
                sensor_detail = COALESCE(?, sensor_detail),
                checked = MAX(checked, ?),
                year = COALESCE(?, year),
                colour = COALESCE(?, colour),
                trim = COALESCE(?, trim),
                mileage = COALESCE(?, mileage),
                price_current = COALESCE(?, price_current),
                dealer = COALESCE(?, dealer),
                location = COALESCE(?, location),
                distance_miles = COALESCE(?, distance_miles),
                photo_status = ?,
                url = COALESCE(?, url),
                car_search_id = COALESCE(?, car_search_id),
                car_search_name = COALESCE(?, car_search_name),
                make = COALESCE(?, make),
                model = COALESCE(?, model),
                fuel = COALESCE(?, fuel),
                transmission = COALESCE(?, transmission),
                source = COALESCE(?, source),
                body_type = COALESCE(?, body_type),
                seats = COALESCE(?, seats),
                notes = CASE
                    WHEN ? IS NULL OR ? = '' THEN notes
                    WHEN notes IS NULL OR notes = '' THEN ?
                    WHEN instr(notes, ?) = 0 THEN notes || '; ' || ?
                    ELSE notes
                END,
                last_seen = COALESCE(?, last_seen),
                updated_at = ?
            WHERE registration = ?
            """,
            (
                new_status,
                sensor_status if data.get("sensor_status") is not None else None,
                sensor_detail if data.get("sensor_detail") is not None else None,
                checked,
                data.get("year"),
                data.get("colour"),
                data.get("trim"),
                data.get("mileage"),
                data.get("price_current") or data.get("price"),
                data.get("dealer"),
                data.get("location"),
                data.get("distance_miles"),
                new_photo_status,
                data.get("url"),
                data.get("car_search_id"),
                data.get("car_search_name"),
                data.get("make"),
                data.get("model"),
                data.get("fuel"),
                data.get("transmission"),
                data.get("source"),
                data.get("body_type"),
                data.get("seats"),
                data.get("notes"),
                data.get("notes"),
                data.get("notes"),
                data.get("notes"),
                data.get("notes"),
                data.get("last_seen"),
                t,
                reg,
            ),
        )

    price = data.get("price_current") or data.get("price")
    if price is not None:
        add_price_observation(conn, reg, int(price), source=source, observed_at=data.get("last_seen") or t)

    conn.commit()


def add_price_observation(
    conn: sqlite3.Connection,
    registration: str,
    price: int,
    source: str,
    observed_at: str | None = None,
) -> None:
    reg = normalise_reg(registration)
    if not reg:
        return

    # Safety guard: price_history has a foreign key to vehicles.
    # If a caller tries to add a price before the vehicle exists, skip it rather than crashing.
    exists = conn.execute(
        "SELECT 1 FROM vehicles WHERE registration = ?",
        (reg,),
    ).fetchone()
    if exists is None:
        return

    observed = observed_at or now_iso()

    last = conn.execute(
        """
        SELECT price
        FROM price_history
        WHERE registration = ?
        ORDER BY observed_at DESC, id DESC
        LIMIT 1
        """,
        (reg,),
    ).fetchone()

    if last is not None and int(last["price"]) == int(price):
        return

    conn.execute(
        """
        INSERT OR IGNORE INTO price_history
            (registration, observed_at, price, source)
        VALUES (?, ?, ?, ?)
        """,
        (reg, observed, int(price), source),
    )


def import_master_state(conn: sqlite3.Connection, path: Path = MASTER_STATE_PATH) -> int:
    if not path.exists():
        return 0
    data = json.loads(path.read_text(encoding="utf-8"))
    count = 0
    for row in data:
        upsert_vehicle(conn, row, source="master_state")
        count += 1
    return count


def create_scrape_run(conn: sqlite3.Connection, search_url: str) -> int:
    cur = conn.execute(
        "INSERT INTO scrape_runs (started_at, search_url, status) VALUES (?, ?, 'running')",
        (now_iso(), search_url),
    )
    conn.commit()
    return int(cur.lastrowid)


def finish_scrape_run(
    conn: sqlite3.Connection,
    run_id: int,
    cars_found: int,
    status: str = "complete",
    message: str | None = None,
    diagnostics: dict[str, Any] | None = None,
) -> None:
    diagnostics = diagnostics or {}
    conn.execute(
        """
        UPDATE scrape_runs SET
            completed_at = ?,
            cars_found = ?,
            cars_matched = ?,
            cars_new = ?,
            cars_marked_missing = ?,
            cars_with_dealer_photos = ?,
            cars_with_stock_photos = ?,
            registrations_skipped = ?,
            parsing_errors = ?,
            runtime_seconds = ?,
            search_fetch_seconds = ?,
            parsing_seconds = ?,
            detail_fetch_count = ?,
            detail_fetch_seconds = ?,
            detail_fetch_errors = ?,
            db_write_seconds = ?,
            missing_mark_seconds = ?,
            timing_json = ?,
            status = ?,
            message = ?
        WHERE id = ?
        """,
        (
            now_iso(),
            cars_found,
            int(diagnostics.get("cars_matched") or 0),
            int(diagnostics.get("cars_new") or 0),
            int(diagnostics.get("cars_marked_missing") or 0),
            int(diagnostics.get("cars_with_dealer_photos") or 0),
            int(diagnostics.get("cars_with_stock_photos") or 0),
            int(diagnostics.get("registrations_skipped") or 0),
            int(diagnostics.get("parsing_errors") or 0),
            diagnostics.get("runtime_seconds"),
            diagnostics.get("search_fetch_seconds"),
            diagnostics.get("parsing_seconds"),
            int(diagnostics.get("detail_fetch_count") or 0),
            diagnostics.get("detail_fetch_seconds"),
            int(diagnostics.get("detail_fetch_errors") or 0),
            diagnostics.get("db_write_seconds"),
            diagnostics.get("missing_mark_seconds"),
            diagnostics.get("timing_json"),
            status,
            message,
            run_id,
        ),
    )
    conn.commit()


def add_scrape_result(conn: sqlite3.Connection, run_id: int, row: dict[str, Any]) -> None:
    reg = normalise_reg(row.get("registration") or "")
    if not reg:
        return

    conn.execute(
        """
        INSERT INTO scrape_results (
            run_id, registration, raw_text, url, seen_price, seen_mileage,
            seen_colour, seen_dealer, seen_distance_miles
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            reg,
            row.get("raw_text"),
            row.get("url"),
            row.get("price"),
            row.get("mileage"),
            row.get("colour"),
            row.get("dealer"),
            row.get("distance_miles"),
        ),
    )


def add_vehicle_change(
    conn: sqlite3.Connection,
    run_id: int | None,
    registration: str,
    change_type: str,
    old_value: Any = None,
    new_value: Any = None,
    reason: str | None = None,
) -> None:
    reg = normalise_reg(registration)
    if not reg or not change_type:
        return

    conn.execute(
        """
        INSERT INTO vehicle_change_log (
            run_id, registration, changed_at, change_type, old_value, new_value, reason
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run_id,
            reg,
            now_iso(),
            str(change_type),
            None if old_value is None else str(old_value),
            None if new_value is None else str(new_value),
            reason,
        ),
    )


def get_vehicle_changes(conn: sqlite3.Connection, run_id: int | None = None, limit: int = 100) -> list[dict[str, Any]]:
    if run_id is None:
        rows = conn.execute(
            """
            SELECT vcl.*, sr.started_at AS run_started_at
            FROM vehicle_change_log vcl
            LEFT JOIN scrape_runs sr ON sr.id = vcl.run_id
            ORDER BY vcl.id DESC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
    else:
        rows = conn.execute(
            """
            SELECT vcl.*, sr.started_at AS run_started_at
            FROM vehicle_change_log vcl
            LEFT JOIN scrape_runs sr ON sr.id = vcl.run_id
            WHERE vcl.run_id = ?
            ORDER BY vcl.id ASC
            LIMIT ?
            """,
            (int(run_id), int(limit)),
        ).fetchall()
    return [dict(r) for r in rows]


def mark_missing_not_seen(
    conn: sqlite3.Connection,
    seen_regs: Iterable[str],
    car_search_id: str,
) -> list[str]:
    """Mark cars missing from one car search when it no longer returns them.

    Only that search's links are touched, so one search (or one person) never
    marks another search's cars missing.
    """
    seen = {normalise_reg(r) for r in seen_regs if normalise_reg(r)}
    rows = conn.execute(
        "SELECT registration FROM vehicle_searches WHERE status = 'active' AND car_search_id = ?",
        (car_search_id,),
    ).fetchall()
    marked_missing: list[str] = []
    for row in rows:
        reg = row["registration"]
        if reg not in seen:
            conn.execute(
                "UPDATE vehicle_searches SET status = 'missing' WHERE car_search_id = ? AND registration = ?",
                (car_search_id, reg),
            )
            marked_missing.append(reg)
    conn.commit()
    return marked_missing


def delete_missing_vehicles(conn: sqlite3.Connection, owner: str | None = None) -> int:
    """Remove missing cars from a person's list (or everyone's when owner is None).

    Cars that are no longer in anybody's list are then deleted completely,
    together with their price history.
    """
    if owner is None:
        cur = conn.execute("DELETE FROM vehicle_searches WHERE status = 'missing'")
    else:
        cur = conn.execute("DELETE FROM vehicle_searches WHERE status = 'missing' AND owner = ?", (owner,))
    removed = int(cur.rowcount or 0)
    conn.execute(
        "DELETE FROM vehicles WHERE registration NOT IN (SELECT registration FROM vehicle_searches)"
    )
    conn.commit()
    return removed


def get_scrape_runs(conn: sqlite3.Connection, limit: int = 10) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            id, started_at, completed_at, search_url, cars_found, cars_matched,
            cars_new, cars_marked_missing, cars_with_dealer_photos,
            cars_with_stock_photos, registrations_skipped, parsing_errors,
            runtime_seconds, search_fetch_seconds, parsing_seconds,
            detail_fetch_count, detail_fetch_seconds, detail_fetch_errors,
            db_write_seconds, missing_mark_seconds, timing_json, status, message
        FROM scrape_runs
        ORDER BY id DESC
        LIMIT ?
        """,
        (int(limit),),
    ).fetchall()
    return [dict(r) for r in rows]

def get_dealer_reachability_settings(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute("SELECT setting_key, setting_value FROM dealer_reachability_settings ORDER BY setting_key").fetchall()
    return {str(row["setting_key"]): str(row["setting_value"]) for row in rows}


def get_dealer_groups(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT dg.id, dg.name, dg.aliases_json, dg.notes, dg.is_active,
               COUNT(db.id) AS branch_count,
               MIN(CASE WHEN db.is_active = 1 THEN db.distance_miles END) AS nearest_branch_distance_miles
        FROM dealer_groups dg
        LEFT JOIN dealer_branches db ON db.dealer_group_id = dg.id
        GROUP BY dg.id
        ORDER BY dg.name
        """
    ).fetchall()
    return [dict(r) for r in rows]


def get_dealer_branches(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT dg.name AS dealer_group, db.name, db.town, db.postcode,
               db.distance_miles, db.is_active, db.notes
        FROM dealer_branches db
        JOIN dealer_groups dg ON dg.id = db.dealer_group_id
        ORDER BY dg.name, db.distance_miles, db.name
        """
    ).fetchall()
    return [dict(r) for r in rows]


def get_vehicles(conn: sqlite3.Connection, owner: str | None = None) -> list[dict[str, Any]]:
    """Cars for one person (their searches only) or, with no owner, every car.

    The per-person values (status, distance, dealer route, interest, notes)
    replace the shared ones on each returned row.  A car found by two of the
    same person's searches is returned once, preferring an active link.
    """
    price_cols = """
        (SELECT ph.price FROM price_history ph WHERE ph.registration = v.registration ORDER BY ph.observed_at DESC, ph.id DESC LIMIT 1) AS latest_price,
        (SELECT ph.price FROM price_history ph WHERE ph.registration = v.registration ORDER BY ph.observed_at DESC, ph.id DESC LIMIT 1 OFFSET 1) AS previous_price,
        (SELECT COUNT(*) FROM price_history ph WHERE ph.registration = v.registration) AS price_history_count
    """
    if owner is None:
        rows = conn.execute(f"SELECT v.*, {price_cols} FROM vehicles v").fetchall()
        return [dict(r) for r in rows]

    rows = conn.execute(
        f"""
        SELECT v.*, {price_cols},
               vs.car_search_id AS link_car_search_id,
               vs.car_search_name AS link_car_search_name,
               vs.owner AS link_owner,
               vs.status AS link_status,
               vs.distance_miles AS link_distance_miles,
               vs.reachability_status AS link_reachability_status,
               vs.dealer_group_id AS link_dealer_group_id,
               vs.dealer_group_name AS link_dealer_group_name,
               vs.nearest_branch_name AS link_nearest_branch_name,
               vs.nearest_branch_distance_miles AS link_nearest_branch_distance_miles,
               vs.reachability_reason AS link_reachability_reason,
               r.interest_status AS review_interest_status,
               r.interest_date AS review_interest_date,
               r.interest_reason AS review_interest_reason,
               r.notes AS review_notes
        FROM vehicle_searches vs
        JOIN vehicles v ON v.registration = vs.registration
        LEFT JOIN vehicle_reviews r ON r.owner = vs.owner AND r.registration = vs.registration
        WHERE vs.owner = ?
        ORDER BY CASE vs.status WHEN 'active' THEN 0 ELSE 1 END, vs.last_seen DESC
        """,
        (owner,),
    ).fetchall()

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in rows:
        item = dict(row)
        if item["registration"] in seen:
            continue
        seen.add(item["registration"])
        manual = item.get("status")
        item["status"] = manual if manual in {"sold", "rejected"} else item["link_status"]
        for key in ("car_search_id", "car_search_name", "distance_miles", "reachability_status",
                    "dealer_group_id", "dealer_group_name", "nearest_branch_name",
                    "nearest_branch_distance_miles", "reachability_reason"):
            item[key] = item.pop(f"link_{key}")
        for key in ("interest_status", "interest_date", "interest_reason", "notes"):
            item[key] = item.pop(f"review_{key}")
        item.pop("link_owner", None)
        item.pop("link_status", None)
        out.append(item)
    return out


def get_price_history(conn: sqlite3.Connection, registration: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT observed_at, price, source
        FROM price_history
        WHERE registration = ?
        ORDER BY observed_at DESC, id DESC
        """,
        (normalise_reg(registration),),
    ).fetchall()
    return [dict(r) for r in rows]


def update_vehicle_manual(
    conn: sqlite3.Connection,
    registration: str,
    status: str,
    sensor_status: str,
    sensor_detail: str,
    checked: bool,
    notes: str | None,
    interest_status: str | None = None,
    interest_date: str | None = None,
    interest_reason: str | None = None,
    owner: str | None = None,
) -> None:
    """Save the popup edits.

    Sensors, photos checked and sold/rejected status are facts about the car,
    shared by everyone.  Interest, review date, reason and notes belong to the
    person (``owner``) who is looking at it.
    """
    reg = normalise_reg(registration)
    ss = normalise_sensor_status(sensor_status)
    sd = normalise_sensor_detail(sensor_detail, ss)
    st = normalise_status(status)
    if st == "missing":
        # Missing is worked out per search; keep the shared row active.
        st = "active"
    interest = normalise_interest_status(interest_status)
    clean_interest_date = (interest_date or "").strip() or None
    clean_interest_reason = (interest_reason or "").strip() or None

    if interest is None:
        clean_interest_date = None
        clean_interest_reason = None

    conn.execute(
        """
        UPDATE vehicles
        SET status = ?, sensor_status = ?, sensor_detail = ?, checked = ?, updated_at = ?
        WHERE registration = ?
        """,
        (st, ss, sd, int(bool(checked)), now_iso(), reg),
    )
    if owner:
        conn.execute(
            """
            INSERT INTO vehicle_reviews (owner, registration, interest_status, interest_date, interest_reason, notes, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(owner, registration) DO UPDATE SET
                interest_status = excluded.interest_status,
                interest_date = excluded.interest_date,
                interest_reason = excluded.interest_reason,
                notes = excluded.notes,
                updated_at = excluded.updated_at
            """,
            (owner, reg, interest, clean_interest_date, clean_interest_reason, notes, now_iso()),
        )
    conn.commit()
