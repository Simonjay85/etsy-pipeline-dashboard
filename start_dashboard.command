#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
APP_PATH="$ROOT_DIR/dashboard_app.py"
LOG_FILE="${ETSY_DASHBOARD_LOG_FILE:-$ROOT_DIR/dashboard_user_launch.log}"
ERROR_LOG_FILE="${ETSY_DASHBOARD_ERROR_LOG_FILE:-$ROOT_DIR/dashboard_user_launch.err.log}"
PID_FILE="${ETSY_DASHBOARD_PID_FILE:-$ROOT_DIR/.dashboard_app.pid}"
PORT="${ETSY_DASHBOARD_PORT:-8090}"
LAUNCHD_LABEL="${ETSY_DASHBOARD_LAUNCHD_LABEL:-com.user.etsy-dashboard}"
LAUNCHD_DIR="${ETSY_DASHBOARD_LAUNCHD_DIR:-$HOME/Library/LaunchAgents}"
LAUNCHD_PLIST="${ETSY_DASHBOARD_LAUNCHD_PLIST:-$LAUNCHD_DIR/$LAUNCHD_LABEL.plist}"
UID_VALUE="$(id -u)"
LAUNCHD_JOB="gui/$UID_VALUE/$LAUNCHD_LABEL"
LAUNCHD_BIN="/bin/launchctl"

pick_python() {
  local candidate
  local candidates=(
    "${ETSY_DASHBOARD_PYTHON:-}"
    "$HOME/.cache/etsy-dashboard-runtime-312/bin/python"
    /opt/homebrew/bin/python3
    "$ROOT_DIR/.etsy_venv/bin/python"
    "$ROOT_DIR/.venv/bin/python"
    /usr/bin/python3
  )

  for candidate in "${candidates[@]}"; do
    [[ -z "$candidate" ]] && continue
    if [[ -x "$candidate" ]]; then
      if "$candidate" -c 'import sys; from sys import version_info as v; exit(0) if (v.major, v.minor) >= (3, 10) else exit(1)' >/dev/null 2>&1; then
        echo "$candidate"
        return 0
      fi
    fi
  done

  return 1
}

if ! PYTHON="$(pick_python)"; then
  echo "Không tìm thấy python3 >= 3.10." >&2
  exit 1
fi

export PYDANTIC_DISABLE_PLUGINS=1

is_running() {
  local pid="$1"
  if [[ -n "$pid" ]] && kill -0 "$pid" >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

load_pid() {
  if [[ -f "$PID_FILE" ]]; then
    cat "$PID_FILE"
  else
    echo ""
  fi
}

is_listening() {
  if lsof -nP -iTCP:"$PORT" -sTCP:LISTEN >/dev/null 2>&1; then
    return 0
  fi
  return 1
}

cleanup_stale_pid() {
  local pid
  pid="$(load_pid)"
  if [[ -n "$pid" ]] && ! is_running "$pid"; then
    rm -f "$PID_FILE"
  fi
}

find_running_process() {
  local found_pid
  found_pid="$(pgrep -f "$APP_PATH" || true)"
  echo "${found_pid%%$'\n'*}"
}

stop_dashboard_launchd_jobs_for_pid() {
  local target_pid="$1"
  if [[ -z "$target_pid" ]]; then
    return 0
  fi

  while IFS= read -r list_pid _ list_label; do
    if [[ "$list_pid" == "$target_pid" ]]; then
      "$LAUNCHD_BIN" bootout "gui/$UID_VALUE/$list_label" 2>/dev/null || true
    fi
  done < <("$LAUNCHD_BIN" list)

  # Keep legacy/known labels from prior Etsy launcher iterations.
  "$LAUNCHD_BIN" bootout "gui/$UID_VALUE/com.etsy.dashboard" 2>/dev/null || true
  "$LAUNCHD_BIN" bootout "gui/$UID_VALUE/com.etsy-dashboard" 2>/dev/null || true
}

is_launchd_loaded() {
  "$LAUNCHD_BIN" print "$LAUNCHD_JOB" >/dev/null 2>&1
}

is_launchd_running() {
  if is_launchd_loaded; then
    if "$LAUNCHD_BIN" print "$LAUNCHD_JOB" 2>/dev/null | grep -q "state = running"; then
      return 0
    fi
  fi
  return 1
}

write_launchd_plist() {
  mkdir -p "$LAUNCHD_DIR"

  cat > "$LAUNCHD_PLIST" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>$LAUNCHD_LABEL</string>
  <key>ProgramArguments</key>
  <array>
    <string>$PYTHON</string>
    <string>$APP_PATH</string>
  </array>
  <key>WorkingDirectory</key>
  <string>$ROOT_DIR</string>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PYDANTIC_DISABLE_PLUGINS</key>
    <string>1</string>
    <key>PATH</key>
    <string>/opt/homebrew/bin:/usr/bin:/bin:/usr/local/bin</string>
  </dict>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>$LOG_FILE</string>
  <key>StandardErrorPath</key>
  <string>$ERROR_LOG_FILE</string>
</dict>
</plist>
EOF

  if command -v plutil >/dev/null 2>&1; then
    plutil -lint "$LAUNCHD_PLIST" >/dev/null
  fi
}

load_launchd_job() {
  write_launchd_plist
  "$LAUNCHD_BIN" bootout "$LAUNCHD_JOB" 2>/dev/null || true
  "$LAUNCHD_BIN" bootstrap "gui/$UID_VALUE" "$LAUNCHD_PLIST"
  "$LAUNCHD_BIN" enable "$LAUNCHD_JOB" 2>/dev/null || true
}

stop_server() {
  echo "Stopping dashboard..."
  local found_pid
  found_pid="$(find_running_process)"
  if [[ -n "$found_pid" ]]; then
    stop_dashboard_launchd_jobs_for_pid "$found_pid"
  fi

  if is_launchd_loaded; then
    "$LAUNCHD_BIN" bootout "$LAUNCHD_JOB" 2>/dev/null || true
  fi

  if [[ -n "$found_pid" ]]; then
    kill "$found_pid" >/dev/null 2>&1 || true
    sleep 1
    if is_running "$found_pid"; then
      kill -9 "$found_pid" >/dev/null 2>&1 || true
      sleep 1
    fi
  fi
  if is_listening; then
    pkill -f "$APP_PATH" >/dev/null 2>&1 || true
    sleep 1
  fi
  rm -f "$PID_FILE"
  echo "Đã dừng dashboard."
}

start_server() {
  cleanup_stale_pid
  local running_pid
  running_pid="$(find_running_process)"
  if is_launchd_running && is_listening; then
    echo "Dashboard đang chạy dưới launchd (port $PORT), bạn có thể dùng restart."
    return 0
  fi
  if [[ -n "$running_pid" ]] && is_running "$running_pid"; then
    echo "Dashboard đã đang chạy (PID: $running_pid), bạn có thể dùng restart."
    return 0
  fi

  rm -f "$PID_FILE"
  load_launchd_job

  for i in {1..12}; do
    if is_listening; then
      running_pid="$(find_running_process)"
      if [[ -n "$running_pid" ]]; then
        echo "Dashboard đã bật (PID: $running_pid, log: $LOG_FILE, port: $PORT)"
      else
        echo "Dashboard đã bật (log: $LOG_FILE, port: $PORT)"
      fi
      echo "Mở: http://localhost:$PORT/"
      return 0
    fi
    sleep 1
  done

  echo "Không phát hiện dashboard lắng nghe port $PORT. Kiểm tra log: $LOG_FILE" >&2
  return 1
}

status_server() {
  cleanup_stale_pid
  local running_pid
  running_pid="$(find_running_process)"
  if is_listening; then
    if is_launchd_running; then
      if [[ -n "$running_pid" ]] && is_running "$running_pid"; then
        echo "Dashboard đang chạy với PID: $running_pid dưới launchd (port $PORT)"
      else
        echo "Dashboard đang chạy trên port $PORT dưới launchd (PID không rõ, đang lắng nghe)."
      fi
      return 0
    fi
    if [[ -n "$running_pid" ]] && is_running "$running_pid"; then
      echo "Dashboard đang chạy với PID: $running_pid (port $PORT)"
    else
      echo "Dashboard đang chạy trên port $PORT (PID không rõ, đang lắng nghe)."
    fi
  else
    echo "Dashboard chưa chạy."
  fi
}

case "${1:-restart}" in
  start)
    start_server
    ;;
  stop)
    stop_server
    ;;
  restart)
    stop_server
    start_server
    ;;
  status)
    status_server
    ;;
  log)
    tail -n 50 "$LOG_FILE"
    ;;
  *)
    echo "Usage: $0 [start|stop|restart|status|log]"
    echo "Default: restart"
    exit 1
    ;;
esac
