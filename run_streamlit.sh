#!/usr/bin/env bash
set -e

cd /opt/carfinder

echo "Updating from GitHub..."
git pull

echo "Restarting CarFinder service..."
systemctl restart carfinder-streamlit.service

echo "Status:"
systemctl status carfinder-streamlit.service --no-pager