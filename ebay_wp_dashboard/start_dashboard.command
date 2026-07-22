#!/bin/bash
cd "$(dirname "$0")/.."
VENV="$HOME/.etsy_venv/bin/python"
if [ ! -f "$VENV" ]; then
  VENV="python3"
fi
exec "$VENV" ebay_wp_dashboard/dashboard_app.py
