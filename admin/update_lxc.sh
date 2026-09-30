#!/bin/bash

set -e

echo
echo "=== CarFinder update ==="
echo

echo "Stopping Streamlit..."
systemctl stop carfinder-streamlit.service || true

echo
echo "Updating Git repository..."
cd /opt/carfinder
git pull

echo
echo "Activating Python environment..."
source .venv/bin/activate

echo
echo "Installing requirements..."
pip install -r requirements.txt

echo
echo "Updating database..."
if [ -f scripts/import_master_state.py ]; then
    python scripts/import_master_state.py
fi

echo
echo "Starting Streamlit..."
systemctl start carfinder-streamlit.service

echo
echo "Latest commit:"
git log -1 --oneline

echo
echo "Service status:"
systemctl --no-pager --full status carfinder-streamlit.service | head -20

echo
echo "Done."
echo "Open:"
echo "http://10.83.59.181:8501"
echo