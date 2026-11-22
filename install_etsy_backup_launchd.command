#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd -P)"
UID_VALUE="$(id -u)"
DOMAIN="gui/$UID_VALUE"
DEST_DIR="$HOME/Library/LaunchAgents"
tmp=""
trap 'if [[ -n "$tmp" ]]; then rm -f "$tmp"; fi' EXIT

die() { echo "Error: $*" >&2; exit 1; }
# Restrict checks to the user's home and the directories this installer uses.
check_dir() {
  local path="$1" owner mode
  [[ ! -L "$path" && -d "$path" ]] || die "Unsafe directory or symlink: $path"
  owner="$(stat -f '%u' "$path")"; mode="$(stat -f '%Lp' "$path")"
  [[ "$owner" == "$UID_VALUE" ]] || die "Directory is not owned by current user: $path"
  (( (8#$mode & 0022) == 0 )) || die "Directory is group/world writable: $path"
}
check_dir "$HOME"
for dir in "$HOME/Library" "$DEST_DIR"; do
  if [[ -e "$dir" || -L "$dir" ]]; then check_dir "$dir"; fi
done

# Validate both inputs and both destinations before changing anything.
for kind in daily weekly; do
  src="$ROOT_DIR/com.user.etsy-backup.$kind.plist"
  dest="$DEST_DIR/$(basename "$src")"
  [[ ! -L "$src" && -f "$src" ]] || die "Source is missing or a symlink: $src"
  plutil -lint "$src" >/dev/null || die "Invalid plist: $src"
  [[ "$(plutil -extract Label raw -o - "$src")" == "com.user.etsy-backup.$kind" ]] || die "Unexpected label: $src"
  [[ "$(plutil -extract RunAtLoad raw -o - "$src")" == false ]] || die "RunAtLoad must be false: $src"
  [[ "$(plutil -extract ProgramArguments.0 raw -o - "$src")" == /usr/bin/python3 && "$(plutil -extract ProgramArguments.1 raw -o - "$src")" == /Users/aaronnguyen/Developer/Etsy/backup_etsy_to_drive.py && "$(plutil -extract ProgramArguments.2 raw -o - "$src")" == "$kind" ]] || die "Unexpected command: $src"
  # Reject extra program arguments as well.
  if plutil -extract ProgramArguments.3 raw -o - "$src" >/dev/null 2>&1; then die "Unexpected extra argument: $src"; fi
  if [[ -e "$dest" || -L "$dest" ]]; then
    [[ ! -L "$dest" && -f "$dest" ]] || die "Unsafe destination: $dest"
    cmp -s "$src" "$dest" || die "Destination differs: $dest; review it manually before retrying"
    [[ "$(stat -f '%u' "$dest")" == "$UID_VALUE" && "$(stat -f '%Lp' "$dest")" == 644 ]] || die "Destination ownership or mode differs from current user/0644: $dest"
  fi
done

for dir in "$HOME/Library" "$DEST_DIR"; do
  if [[ ! -e "$dir" && ! -L "$dir" ]]; then mkdir -m 700 "$dir" || die "Cannot create $dir"; fi
  check_dir "$dir"
done

for kind in daily weekly; do
  src="$ROOT_DIR/com.user.etsy-backup.$kind.plist"
  dest="$DEST_DIR/$(basename "$src")"
  label="com.user.etsy-backup.$kind"
  if [[ ! -e "$dest" && ! -L "$dest" ]]; then
    tmp="$(mktemp "$DEST_DIR/.etsy-backup.XXXXXXXX")"
    cp "$src" "$tmp"; chmod 644 "$tmp"
    # A hard link publishes atomically without ever replacing another file.
    ln "$tmp" "$dest" || die "Cannot safely publish $dest; review existing file"
    rm -f "$tmp"; tmp=""
  fi
  [[ ! -L "$dest" && -f "$dest" ]] && cmp -s "$src" "$dest" || die "Destination changed: $dest"
  echo "Persistent plist: $dest"
  if status="$(launchctl print "$DOMAIN/$label" 2>&1)"; then
    path="$(printf '%s\n' "$status" | sed -n 's/^[[:space:]]*path = //p' | head -n 1)"
    if [[ "$path" != "$dest" ]]; then
      [[ "$path" == "$src" || "$path" == "/Users/aaronnguyen/Developer/Etsy/$(basename "$src")" ]] || die "$label is loaded from an unexpected path: ${path:-unknown}; review manually"
      if printf '%s\n' "$status" | grep -Eq '^[[:space:]]*pid = [0-9]+'; then die "$label is running; refusing to stop it"; fi
      launchctl bootout "$DOMAIN/$label" || die "Could not unload obsolete $label"
      launchctl bootstrap "$DOMAIN" "$dest" || die "Bootstrap failed for $label; persistent plist remains at $dest"
    fi
  else
    print_exit=$?
    # The domain can list enabled overrides for services that are not loaded.
    domain_status="$(launchctl print "$DOMAIN" 2>&1)" || die "Cannot inspect $DOMAIN: $domain_status"
    if [[ "$print_exit" -ne 113 ]] || ! printf '%s\n' "$status" | grep -Fxq "Could not find service \"$label\" in domain for user gui: $UID_VALUE"; then
      die "Could not inspect $label (exit $print_exit): $status"
    fi
    launchctl bootstrap "$DOMAIN" "$dest" || die "Bootstrap failed for $label; persistent plist remains at $dest"
  fi
  launchctl enable "$DOMAIN/$label" || die "Enable failed for $label; persistent plist remains at $dest"
  status="$(launchctl print "$DOMAIN/$label" 2>&1)" || die "Could not verify $label: $status; persistent plist remains at $dest"
  path="$(printf '%s\n' "$status" | sed -n 's/^[[:space:]]*path = //p' | head -n 1)"
  [[ "$path" == "$dest" ]] || die "Verification found unexpected path for $label: ${path:-unknown}"
  echo "Verified $label loaded from $dest"
done

echo "Daily 03:15; weekly Sunday 03:45. User jobs require login and a running Mac."
