#!/usr/bin/env python3
"""One-time corrective clean reset for CarFinder v3.

v3.0.2 introduced the major-version marker, but an existing v3 database may
already have been marked as v3 before it was genuinely empty.  This repair
forces exactly one clean v3 database start.

User accounts and private settings are preserved.
"""
from __future__ import annotations

import shutil
import sqlite3
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
DB_PATH = DATA_DIR / "carfinder.db"
MASTER_STATE_PATH = DATA_DIR / "master_state.json"
REPAIR_MARKER = DATA_DIR / ".v3-clean-repair-complete"
DATA_MAJOR_PATH = DATA_DIR / ".carfinder-data-major"


def remove_database() -> list[str]:
    removed=[]
    for p in (
        DB_PATH,
        Path(str(DB_PATH)+"-wal"),
        Path(str(DB_PATH)+"-shm"),
        Path(str(DB_PATH)+"-journal"),
    ):
        if p.exists():
            p.unlink()
            removed.append(p.name)
    return removed


def clear_cache() -> None:
    cache=DATA_DIR/"cache"
    if not cache.exists():
        return
    for item in cache.iterdir():
        if item.is_file() or item.is_symlink():
            item.unlink()
        elif item.is_dir():
            shutil.rmtree(item)


def create_and_verify_empty() -> None:
    if str(REPO_ROOT) not in sys.path:
        sys.path.insert(0,str(REPO_ROOT))
    from app.db import connect, init_db
    conn=connect()
    try:
        init_db(conn)
    finally:
        conn.close()

    conn=sqlite3.connect(DB_PATH)
    try:
        tables=("vehicles","price_history","vehicle_searches","scrape_runs")
        counts={t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0] for t in tables}
    finally:
        conn.close()
    if any(counts.values()):
        raise RuntimeError("v3 clean repair failed: "+", ".join(f"{k}={v}" for k,v in counts.items()))


def main() -> int:
    DATA_DIR.mkdir(parents=True,exist_ok=True)
    if REPAIR_MARKER.exists():
        print("CarFinder v3 clean repair already completed; keeping current v3 data.")
        return 0

    removed=remove_database()
    if MASTER_STATE_PATH.exists():
        MASTER_STATE_PATH.unlink()
        removed.append(MASTER_STATE_PATH.name)
    clear_cache()
    create_and_verify_empty()

    DATA_MAJOR_PATH.write_text("3\n",encoding="utf-8")
    REPAIR_MARKER.write_text("CarFinder v3 forced clean repair completed.\n",encoding="utf-8")

    print("CarFinder v3 forced clean reset complete.")
    if removed:
        print("Removed: "+", ".join(removed))
    print("Preserved: user accounts and private settings.")
    print("Database now contains 0 vehicles and no pre-v3 history.")
    return 0


if __name__=="__main__":
    raise SystemExit(main())
