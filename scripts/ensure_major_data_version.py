#!/usr/bin/env python3
"""Reset CarFinder application data automatically on a major-version change.

Policy:
- v3.x -> v3.y keeps the current v3 application database.
- v3.x -> v4.0 starts a fresh v4 application database.
- v4.x -> v5.0 starts a fresh v5 application database, and so on.
- User accounts and private settings are preserved.

The current data-major is stored in data/.carfinder-data-major.
If the marker is absent, the current major is treated as a new major and a
clean database is created.  This gives v3 its intended clean start.
"""
from __future__ import annotations

import json
import shutil
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
DB_PATH = DATA_DIR / "carfinder.db"
MASTER_STATE_PATH = DATA_DIR / "master_state.json"
DATA_MAJOR_PATH = DATA_DIR / ".carfinder-data-major"
RELEASE_PATH = REPO_ROOT / "release.json"


def current_major() -> int:
    try:
        release = json.loads(RELEASE_PATH.read_text(encoding="utf-8"))
        version = str(release.get("version") or "").strip()
        major_text = version.split(".", 1)[0]
        major = int(major_text)
    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Cannot determine CarFinder major version from {RELEASE_PATH}") from exc
    if major < 1:
        raise RuntimeError(f"Invalid CarFinder major version: {major}")
    return major


def stored_major() -> int | None:
    try:
        text = DATA_MAJOR_PATH.read_text(encoding="utf-8").strip()
        return int(text)
    except (OSError, ValueError):
        return None


def remove_sqlite_database(path: Path) -> list[str]:
    removed: list[str] = []
    for candidate in (
        path,
        Path(str(path) + "-wal"),
        Path(str(path) + "-shm"),
        Path(str(path) + "-journal"),
    ):
        if candidate.exists():
            candidate.unlink()
            removed.append(candidate.name)
    return removed


def clear_cache() -> None:
    cache_dir = DATA_DIR / "cache"
    if not cache_dir.exists():
        return
    for item in cache_dir.iterdir():
        if item.is_file() or item.is_symlink():
            item.unlink()
        elif item.is_dir():
            shutil.rmtree(item)


def create_fresh_database() -> None:
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0, str(REPO_ROOT))
    from app.db import connect, init_db

    conn = connect()
    try:
        init_db(conn)
    finally:
        conn.close()


def verify_empty_database() -> None:
    conn = sqlite3.connect(DB_PATH)
    try:
        counts = {
            "vehicles": conn.execute("SELECT COUNT(*) FROM vehicles").fetchone()[0],
            "price_history": conn.execute("SELECT COUNT(*) FROM price_history").fetchone()[0],
            "vehicle_searches": conn.execute("SELECT COUNT(*) FROM vehicle_searches").fetchone()[0],
            "scrape_runs": conn.execute("SELECT COUNT(*) FROM scrape_runs").fetchone()[0],
        }
    finally:
        conn.close()

    if any(counts.values()):
        detail = ", ".join(f"{k}={v}" for k, v in counts.items())
        raise RuntimeError(f"Fresh CarFinder database was not empty: {detail}")


def reset_for_major(major: int, previous: int | None) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)

    removed = remove_sqlite_database(DB_PATH)

    # Legacy v2-era master state must never repopulate a new-major database.
    if MASTER_STATE_PATH.exists():
        MASTER_STATE_PATH.unlink()
        removed.append(MASTER_STATE_PATH.name)

    clear_cache()
    create_fresh_database()
    verify_empty_database()

    DATA_MAJOR_PATH.write_text(f"{major}\n", encoding="utf-8")

    if previous is None:
        print(f"CarFinder v{major} clean data start complete.")
    else:
        print(f"CarFinder major upgrade v{previous} -> v{major}: application data reset complete.")

    if removed:
        print("Removed previous application data: " + ", ".join(removed))
    else:
        print("No previous active database files were present.")
    print("Preserved: user accounts and private settings.")
    print("Fresh database: 0 vehicles, 0 price history, 0 search links, 0 scrape runs.")


def main() -> int:
    major = current_major()
    previous = stored_major()

    if previous == major:
        print(f"CarFinder data already belongs to major version v{major}; keeping current data.")
        return 0

    reset_for_major(major, previous)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
