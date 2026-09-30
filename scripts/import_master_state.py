#!/usr/bin/env python3
from pathlib import Path
import sys
REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
from app.db import connect, init_db, import_master_state

if __name__ == "__main__":
    conn = connect()
    init_db(conn)
    count = import_master_state(conn)
    conn.close()
    print(f"Imported {count} master vehicle record(s).")
