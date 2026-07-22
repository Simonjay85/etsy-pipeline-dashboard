#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
UID_VALUE="$(id -u)"

mkdir -p "$ROOT_DIR/output/backup"

for plist in "$ROOT_DIR"/com.user.etsy-backup.{daily,weekly}.plist; do
  label="$(/usr/libexec/PlistBuddy -c 'Print :Label' "$plist")"
  launchctl bootout "gui/$UID_VALUE/$label" 2>/dev/null || true
  launchctl bootstrap "gui/$UID_VALUE" "$plist"
  launchctl enable "gui/$UID_VALUE/$label"
  echo "Loaded $label"
done

echo "Daily: 03:15"
echo "Weekly: Sunday 03:45"
echo "Check: launchctl print gui/$UID_VALUE/com.user.etsy-backup.daily"
