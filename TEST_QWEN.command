#!/usr/bin/env bash
set -euo pipefail

# Use the canonical checkout and runtime-only Together environment. The key
# must be supplied by the shell or the user launchd session; never put it in
# this launcher, shops_config.json, command arguments, or logs.
ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT_DIR"

exec "$ROOT_DIR/dashboard_launchd_env.command" \
  "${ETSY_DASHBOARD_PYTHON:-python3}" \
  "$ROOT_DIR/together_qwen.py"
