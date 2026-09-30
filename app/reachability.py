from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any

REACHABILITY_LOCAL = "LOCAL"
REACHABILITY_TRANSFERABLE = "TRANSFERABLE"
REACHABILITY_REMOTE = "REMOTE"
VALID_REACHABILITY = {REACHABILITY_LOCAL, REACHABILITY_TRANSFERABLE, REACHABILITY_REMOTE}


@dataclass(frozen=True)
class ReachabilityResult:
    status: str
    dealer_group_id: int | None
    dealer_group_name: str | None
    nearest_branch_name: str | None
    nearest_branch_distance_miles: int | None
    reason: str


def normalise_dealer_text(value: str | None) -> str:
    text = (value or "").strip().lower()
    text = text.replace("&", " and ")
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def int_or_none(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(round(float(value)))
    except (TypeError, ValueError):
        return None


def load_reachability_config() -> dict[str, Any]:
    """Return the reachability part of the user's Settings."""
    from app.settings import load_settings

    settings = load_settings()
    return {
        "local_radius_miles": int_or_none(settings.get("local_radius_miles")) or 30,
        "dealer_groups": settings.get("dealer_groups") or [],
    }


def local_radius_miles(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT setting_value FROM dealer_reachability_settings WHERE setting_key = 'local_radius_miles'"
    ).fetchone()
    value = int_or_none(row["setting_value"] if row else None)
    return value if value is not None else 30


def seed_dealer_reachability(conn: sqlite3.Connection, config: dict[str, Any] | None = None) -> None:
    """Copy dealer groups/branches from Settings into the database tables.

    Records are updated by name rather than deleted.  Groups or branches that
    are no longer in Settings are marked inactive, so they stop affecting
    reachability without losing their history.
    """
    config = config or load_reachability_config()
    settings = {
        "local_radius_miles": str(int_or_none(config.get("local_radius_miles")) or 30),
    }
    for key, value in settings.items():
        conn.execute(
            """
            INSERT INTO dealer_reachability_settings (setting_key, setting_value, updated_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(setting_key) DO UPDATE SET
                setting_value = excluded.setting_value,
                updated_at = excluded.updated_at
            """,
            (key, value),
        )

    groups = config.get("dealer_groups") or []
    if not isinstance(groups, list):
        groups = []

    active_group_ids: list[int] = []
    for group in groups:
        if not isinstance(group, dict):
            continue
        name = str(group.get("name") or "").strip()
        if not name:
            continue
        aliases = group.get("aliases") or []
        if isinstance(aliases, str):
            aliases = aliases.split(",")
        alias_text = json.dumps([str(a).strip() for a in aliases if str(a).strip()], ensure_ascii=False)
        conn.execute(
            """
            INSERT INTO dealer_groups (name, aliases_json, notes, is_active, updated_at)
            VALUES (?, ?, ?, 1, datetime('now'))
            ON CONFLICT(name) DO UPDATE SET
                aliases_json = excluded.aliases_json,
                notes = excluded.notes,
                is_active = 1,
                updated_at = excluded.updated_at
            """,
            (name, alias_text, group.get("notes")),
        )
        group_row = conn.execute("SELECT id FROM dealer_groups WHERE name = ?", (name,)).fetchone()
        if group_row is None:
            continue
        group_id = int(group_row["id"])
        active_group_ids.append(group_id)

        branch_name = str(group.get("branch_name") or "").strip()
        if branch_name:
            conn.execute(
                """
                INSERT INTO dealer_branches (
                    dealer_group_id, name, town, postcode, distance_miles,
                    notes, is_active, updated_at
                ) VALUES (?, ?, ?, NULL, ?, NULL, 1, datetime('now'))
                ON CONFLICT(dealer_group_id, name) DO UPDATE SET
                    town = excluded.town,
                    distance_miles = excluded.distance_miles,
                    is_active = 1,
                    updated_at = excluded.updated_at
                """,
                (group_id, branch_name, group.get("branch_town"), int_or_none(group.get("branch_distance_miles"))),
            )
        conn.execute(
            "UPDATE dealer_branches SET is_active = 0 WHERE dealer_group_id = ? AND name <> ?",
            (group_id, branch_name),
        )

    if active_group_ids:
        placeholders = ",".join("?" for _ in active_group_ids)
        conn.execute(f"UPDATE dealer_groups SET is_active = 0 WHERE id NOT IN ({placeholders})", active_group_ids)
    else:
        conn.execute("UPDATE dealer_groups SET is_active = 0")


def aliases_for_group(row: sqlite3.Row) -> list[str]:
    aliases: list[str] = []
    raw = row["aliases_json"]
    if raw:
        try:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                aliases.extend(str(v) for v in parsed if str(v).strip())
        except json.JSONDecodeError:
            pass
    aliases.append(str(row["name"]))
    return aliases


def match_dealer_group(conn: sqlite3.Connection, dealer: str | None) -> sqlite3.Row | None:
    dealer_norm = normalise_dealer_text(dealer)
    if not dealer_norm:
        return None

    rows = conn.execute(
        "SELECT id, name, aliases_json FROM dealer_groups WHERE is_active = 1 ORDER BY length(name) DESC, name"
    ).fetchall()
    best_row = None
    best_score = -1
    for row in rows:
        for alias in aliases_for_group(row):
            alias_norm = normalise_dealer_text(alias)
            if not alias_norm:
                continue
            if alias_norm == dealer_norm or alias_norm in dealer_norm or dealer_norm in alias_norm:
                score = len(alias_norm)
                if score > best_score:
                    best_row = row
                    best_score = score
    return best_row


def nearest_active_branch(conn: sqlite3.Connection, dealer_group_id: int, radius_miles: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT name, town, postcode, distance_miles
        FROM dealer_branches
        WHERE dealer_group_id = ?
          AND is_active = 1
          AND distance_miles IS NOT NULL
          AND distance_miles <= ?
        ORDER BY distance_miles ASC, name ASC
        LIMIT 1
        """,
        (dealer_group_id, radius_miles),
    ).fetchone()


def calculate_reachability(
    conn: sqlite3.Connection,
    vehicle: dict[str, Any] | sqlite3.Row,
    radius_miles: int | None = None,
) -> ReachabilityResult:
    """Classify a car as LOCAL / TRANSFERABLE / REMOTE.

    ``radius_miles`` is the viewing person's local radius; ``vehicle`` needs
    ``distance_miles`` (from that person's postcode) and ``dealer``.
    """
    radius = radius_miles if radius_miles is not None else local_radius_miles(conn)
    distance = int_or_none(vehicle["distance_miles"] if "distance_miles" in vehicle.keys() else None)
    if distance is not None and distance <= radius:
        return ReachabilityResult(
            status=REACHABILITY_LOCAL,
            dealer_group_id=None,
            dealer_group_name=None,
            nearest_branch_name=None,
            nearest_branch_distance_miles=None,
            reason=f"Vehicle is {distance} miles away, within the {radius}-mile local radius.",
        )

    dealer = vehicle["dealer"] if "dealer" in vehicle.keys() else None
    group = match_dealer_group(conn, dealer)
    if group is not None:
        branch = nearest_active_branch(conn, int(group["id"]), radius)
        if branch is not None:
            branch_bits = [str(branch["name"])]
            if branch["town"]:
                branch_bits.append(str(branch["town"]))
            return ReachabilityResult(
                status=REACHABILITY_TRANSFERABLE,
                dealer_group_id=int(group["id"]),
                dealer_group_name=str(group["name"]),
                nearest_branch_name=" - ".join(branch_bits),
                nearest_branch_distance_miles=int_or_none(branch["distance_miles"]),
                reason=f"Dealer matches {group['name']} and that group has a branch within {radius} miles.",
            )

        return ReachabilityResult(
            status=REACHABILITY_REMOTE,
            dealer_group_id=int(group["id"]),
            dealer_group_name=str(group["name"]),
            nearest_branch_name=None,
            nearest_branch_distance_miles=None,
            reason=f"Dealer matches {group['name']}, but no active branch within {radius} miles is configured.",
        )

    if distance is None:
        reason = f"No distance and no matching dealer group with a branch within {radius} miles."
    else:
        reason = f"Vehicle is {distance} miles away and no matching dealer group with a nearby branch is configured."
    return ReachabilityResult(
        status=REACHABILITY_REMOTE,
        dealer_group_id=None,
        dealer_group_name=None,
        nearest_branch_name=None,
        nearest_branch_distance_miles=None,
        reason=reason,
    )
