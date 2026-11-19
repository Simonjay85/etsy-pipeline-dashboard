#!/bin/bash
# Durable local Creative Fabrica survey supervisor.
set -u

ROOT="/Users/aaronnguyen/Developer/Etsy"
PYTHON="$ROOT/.venv/bin/python"
SCRIPT="$ROOT/creativefabrica_bundle_survey.py"
SOURCE="$ROOT/output/creativeca_catalog/bundles_survey_next9000_source.csv"
OUTDIR="$ROOT/output/creativeca_catalog/bundles_next9000"
CHECKPOINT="$OUTDIR/bundles_survey_fixed.checkpoint.jsonl"
LOG="$OUTDIR/supervisor.log"
DONE="$OUTDIR/.next9000.done"
LOCK="$OUTDIR/.supervisor.lock"
CHROME="/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PROFILE="$HOME/.chatgpt_daisy_worker"
CDP="http://127.0.0.1:9333"
TARGET=9000
BATCH=200

mkdir -p "$OUTDIR"
if ! mkdir "$LOCK" 2>/dev/null; then
  printf '%s supervisor already running\n' "$(date -u +%FT%TZ)" >> "$LOG"
  exit 0
fi
cleanup() { rmdir "$LOCK" 2>/dev/null || true; }
trap cleanup EXIT INT TERM

count_completed() {
  "$PYTHON" - "$CHECKPOINT" <<'PY'
import json, sys
from pathlib import Path
p=Path(sys.argv[1])
seen=set()
if p.exists():
    for line in p.read_text(encoding='utf-8').splitlines():
        if line.strip():
            try:
                r=json.loads(line)
                i=int(r.get('crawl_index',-1))
                if i >= 0: seen.add(i)
            except Exception:
                pass
print(len(seen))
PY
}

ensure_cdp() {
  if ! /usr/bin/curl -fsS --max-time 3 "$CDP/json/version" >/dev/null 2>&1; then
    if ! /usr/bin/pgrep -f -- "--user-data-dir=$PROFILE" >/dev/null 2>&1; then
      "$CHROME" --user-data-dir="$PROFILE" --remote-debugging-port=9333 \
        --no-first-run --no-default-browser-check --disable-background-networking about:blank \
        >>"$OUTDIR/chrome-supervisor.log" 2>&1 &
    fi
    for _ in $(seq 1 30); do
      /usr/bin/curl -fsS --max-time 2 "$CDP/json/version" >/dev/null 2>&1 && break
      sleep 2
    done
  fi
  /usr/bin/curl -fsS --max-time 3 "$CDP/json/version" >/dev/null 2>&1 || return 1
  # Playwright's CDP adapter needs at least one page target.
  "$PYTHON" - <<'PY'
import json, urllib.request
base='http://127.0.0.1:9333'
try:
    targets=json.load(urllib.request.urlopen(base+'/json/list',timeout=3))
    if not any(t.get('type') == 'page' for t in targets):
        req=urllib.request.Request(base+'/json/new?about:blank',method='PUT')
        urllib.request.urlopen(req,timeout=5).read()
except Exception as exc:
    raise SystemExit(str(exc))
PY
}

if [ -f "$DONE" ]; then
  printf '%s already complete\n' "$(date -u +%FT%TZ)" >> "$LOG"
  exit 0
fi

while :; do
  completed="$(count_completed)"
  printf '%s checkpoint=%s/%s\n' "$(date -u +%FT%TZ)" "$completed" "$TARGET" >> "$LOG"
  if [ "$completed" -ge "$TARGET" ]; then
    touch "$DONE"
    printf '%s complete\n' "$(date -u +%FT%TZ)" >> "$LOG"
    exit 0
  fi
  if ! ensure_cdp; then
    printf '%s CDP unavailable; retrying in 60s\n' "$(date -u +%FT%TZ)" >> "$LOG"
    sleep 60
    continue
  fi
  if ! "$PYTHON" "$SCRIPT" \
      --input-csv "$SOURCE" --target "$TARGET" --max-new-records "$BATCH" \
      --concurrency 2 --delay-ms 2500 --pause-every 100 --pause-seconds 60 \
      --scope-label 'next 9,000 unique product URLs (pages 30-367)' \
      --output-prefix bundles_survey_next9000_fixed --output-dir "$OUTDIR" \
      >>"$LOG" 2>&1; then
    printf '%s batch failed; retrying in 120s\n' "$(date -u +%FT%TZ)" >> "$LOG"
    sleep 120
    continue
  fi
  completed="$(count_completed)"
  printf '%s batch finished checkpoint=%s/%s; cooling down 60s\n' "$(date -u +%FT%TZ)" "$completed" "$TARGET" >> "$LOG"
  sleep 60
done
