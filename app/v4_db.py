"""CarFinder v4 application database.

v4 deliberately starts with a fresh application database.  Existing user
accounts remain in data/users.json, but vehicle/search/history data is not
migrated from v3.
"""
from __future__ import annotations

import json
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

APP_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = APP_ROOT / "data"
DB_PATH = DATA_DIR / "carfinder_v4.db"


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
        CREATE TABLE IF NOT EXISTS user_settings (
            username TEXT PRIMARY KEY,
            postcode TEXT,
            radius_miles INTEGER,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS discovery_profiles (
            username TEXT PRIMARY KEY,
            manufacturers_json TEXT NOT NULL DEFAULT '[]',
            manufacturer_mode TEXT NOT NULL DEFAULT 'all',
            fuel TEXT,
            body_type TEXT,
            transmission TEXT,
            min_price INTEGER,
            max_price INTEGER,
            max_mileage INTEGER,
            min_year INTEGER,
            seats INTEGER,
            min_power_bhp INTEGER,
            updated_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS car_targets (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            username TEXT NOT NULL,
            make TEXT NOT NULL,
            model TEXT NOT NULL,
            body_type TEXT,
            enabled INTEGER NOT NULL DEFAULT 1,
            overrides_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_car_targets_user
        ON car_targets(username, enabled);

        CREATE TABLE IF NOT EXISTS vehicles (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            registration TEXT,
            make TEXT NOT NULL,
            model TEXT NOT NULL,
            fuel TEXT,
            transmission TEXT,
            body_type TEXT,
            trim TEXT,
            variant TEXT,
            engine_size TEXT,
            seats INTEGER,
            power_bhp INTEGER,
            first_registered TEXT,
            raw_attributes_json TEXT NOT NULL DEFAULT '{}',
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            UNIQUE(registration)
        );

        CREATE TABLE IF NOT EXISTS listings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            vehicle_id INTEGER NOT NULL,
            source TEXT NOT NULL,
            source_listing_id TEXT,
            url TEXT,
            dealer_name TEXT,
            dealer_postcode TEXT,
            price INTEGER,
            mileage INTEGER,
            photo_status TEXT,
            active INTEGER NOT NULL DEFAULT 1,
            first_seen TEXT NOT NULL,
            last_seen TEXT NOT NULL,
            FOREIGN KEY(vehicle_id) REFERENCES vehicles(id) ON DELETE CASCADE,
            UNIQUE(source, source_listing_id)
        );

        CREATE TABLE IF NOT EXISTS price_history (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            listing_id INTEGER NOT NULL,
            observed_at TEXT NOT NULL,
            price INTEGER NOT NULL,
            FOREIGN KEY(listing_id) REFERENCES listings(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS user_vehicle_state (
            username TEXT NOT NULL,
            vehicle_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'neutral',
            notes TEXT,
            updated_at TEXT NOT NULL,
            PRIMARY KEY(username, vehicle_id),
            FOREIGN KEY(vehicle_id) REFERENCES vehicles(id) ON DELETE CASCADE
        );

        CREATE TABLE IF NOT EXISTS search_jobs (
            id TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            search_type TEXT NOT NULL,
            status TEXT NOT NULL,
            progress_current INTEGER NOT NULL DEFAULT 0,
            progress_total INTEGER NOT NULL DEFAULT 0,
            matches_found INTEGER NOT NULL DEFAULT 0,
            message TEXT,
            criteria_json TEXT NOT NULL DEFAULT '{}',
            created_at TEXT NOT NULL,
            started_at TEXT,
            completed_at TEXT
        );

        CREATE TABLE IF NOT EXISTS api_sessions (
            token TEXT PRIMARY KEY,
            username TEXT NOT NULL,
            created_at TEXT NOT NULL,
            expires_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_api_sessions_expiry
        ON api_sessions(expires_at);
        """
    )
    conn.commit()


def ensure_db() -> None:
    with connect() as conn:
        init_db(conn)


def create_session(username: str, hours: int = 24 * 14) -> str:
    token = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc)
    expires = now + timedelta(hours=hours)
    with connect() as conn:
        init_db(conn)
        conn.execute(
            "INSERT INTO api_sessions(token, username, created_at, expires_at) VALUES (?, ?, ?, ?)",
            (
                token,
                username,
                now.replace(microsecond=0).isoformat(),
                expires.replace(microsecond=0).isoformat(),
            ),
        )
        conn.commit()
    return token


def session_username(token: str | None) -> str | None:
    if not token:
        return None
    now = now_iso()
    with connect() as conn:
        init_db(conn)
        conn.execute("DELETE FROM api_sessions WHERE expires_at <= ?", (now,))
        row = conn.execute(
            "SELECT username FROM api_sessions WHERE token = ? AND expires_at > ?",
            (token, now),
        ).fetchone()
        conn.commit()
    return str(row["username"]) if row else None


def delete_session(token: str | None) -> None:
    if not token:
        return
    with connect() as conn:
        init_db(conn)
        conn.execute("DELETE FROM api_sessions WHERE token = ?", (token,))
        conn.commit()


def get_user_settings(username: str) -> dict[str, Any]:
    with connect() as conn:
        init_db(conn)
        row = conn.execute(
            "SELECT postcode, radius_miles FROM user_settings WHERE username = ?",
            (username,),
        ).fetchone()
    if not row:
        return {"postcode": "", "radius_miles": 50}
    return {
        "postcode": row["postcode"] or "",
        "radius_miles": row["radius_miles"] if row["radius_miles"] is not None else 50,
    }


def save_user_settings(username: str, postcode: str, radius_miles: int) -> dict[str, Any]:
    postcode = (postcode or "").strip().upper()
    radius_miles = max(1, min(int(radius_miles), 500))
    with connect() as conn:
        init_db(conn)
        conn.execute(
            """
            INSERT INTO user_settings(username, postcode, radius_miles, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT(username) DO UPDATE SET
                postcode = excluded.postcode,
                radius_miles = excluded.radius_miles,
                updated_at = excluded.updated_at
            """,
            (username, postcode, radius_miles, now_iso()),
        )
        conn.commit()
    return {"postcode": postcode, "radius_miles": radius_miles}


def list_targets(username: str) -> list[dict[str, Any]]:
    with connect() as conn:
        init_db(conn)
        rows = conn.execute(
            """
            SELECT id, make, model, body_type, enabled, overrides_json
            FROM car_targets
            WHERE username = ?
            ORDER BY make, model, id
            """,
            (username,),
        ).fetchall()
    return [
        {
            "id": row["id"],
            "make": row["make"],
            "model": row["model"],
            "body_type": row["body_type"],
            "enabled": bool(row["enabled"]),
            "overrides": json.loads(row["overrides_json"] or "{}"),
        }
        for row in rows
    ]
