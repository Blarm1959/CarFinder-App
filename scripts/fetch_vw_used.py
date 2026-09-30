#!/usr/bin/env python3
"""Backwards-compatible entry point.

The VW scraper now lives in ``app/sources/vw.py`` and all searches are run by
``scripts/run_search.py``.  This wrapper keeps existing LXC scripts, cron jobs
and systemd units that call ``fetch_vw_used.py`` working.
"""
from __future__ import annotations

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.run_search import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
