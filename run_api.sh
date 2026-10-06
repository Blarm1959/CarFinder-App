#!/bin/bash
set -e
cd "$(dirname "$0")"
source .venv/bin/activate
exec uvicorn app.v4_api:app --host 127.0.0.1 --port 8502
