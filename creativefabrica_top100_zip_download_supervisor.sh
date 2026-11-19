#!/bin/bash
# Durable browser-download supervisor for Creative Fabrica top-100 POD sources.
set -u
ROOT="/Users/aaronnguyen/Developer/Etsy"
PYTHON="$ROOT/.venv/bin/python"
SCRIPT="$ROOT/creativefabrica_top100_zip_download.py"
OUT="$ROOT/output/creativeca_catalog/top100_verified"
CHECKPOINT="$OUT/download_checkpoint.jsonl"
LOG="$OUT/download_supervisor.log"
DONE="$OUT/.top100-download.done"
LOCK="$OUT/.download-supervisor.lock"
mkdir -p "$OUT"
if ! mkdir "$LOCK" 2>/dev/null; then exit 0; fi
trap 'rmdir "$LOCK" 2>/dev/null || true' EXIT INT TERM
count_done() {
  "$PYTHON" - "$CHECKPOINT" <<'PY'
import json,sys
from pathlib import Path
p=Path(sys.argv[1]); s=set()
if p.exists():
 for line in p.read_text(encoding='utf-8').splitlines():
  if line.strip():
   try:
    r=json.loads(line)
    if r.get('status') in ('downloaded','downloaded_valid_zip'): s.add(int(r['rank']))
   except: pass
print(len(s))
PY
}
while :; do
  n="$(count_done)"
  printf '%s downloaded=%s/100\n' "$(date -u +%FT%TZ)" "$n" >> "$LOG"
  if [ "$n" -ge 100 ]; then touch "$DONE"; printf '%s complete\n' "$(date -u +%FT%TZ)" >> "$LOG"; exit 0; fi
  if ! "$PYTHON" "$SCRIPT" --delay 5 --pause-every 5 --pause-seconds 60 >>"$LOG" 2>&1; then
    printf '%s downloader failed; cooling down 300s\n' "$(date -u +%FT%TZ)" >> "$LOG"
    sleep 300
  else
    n="$(count_done)"
    if [ "$n" -lt 100 ]; then sleep 60; fi
  fi
done
