#!/bin/bash
# Durable Creative Fabrica downloader for the user-selected Backup volume.
set -u
ROOT="/Users/aaronnguyen/Developer/Etsy"
PYTHON="$ROOT/.venv/bin/python"
SCRIPT="$ROOT/creativefabrica_top100_zip_download.py"
OUT="/Volumes/Backup/Product"
MANIFEST="$OUT/top100_verified.csv"
CHECKPOINT="$OUT/download_checkpoint.jsonl"
LOG="$OUT/download_supervisor.log"
DONE="$OUT/.downloads.done"
LOCK="$OUT/.downloads.lock.v2"
MIN_RANK=22
TARGET_COUNT=79
mkdir -p "$OUT"
if ! mkdir "$LOCK" 2>/dev/null; then exit 0; fi
cleanup(){ rmdir "$LOCK" 2>/dev/null || true; }
trap cleanup EXIT INT TERM
count_target_done(){
 "$PYTHON" - "$CHECKPOINT" "$MIN_RANK" <<'PY'
import json,sys
from pathlib import Path
p=Path(sys.argv[1]); minimum=int(sys.argv[2]); s=set()
if p.exists():
 for line in p.read_text(encoding='utf-8').splitlines():
  if line.strip():
   try:
    r=json.loads(line)
    if r.get('status') in ('downloaded','downloaded_valid_zip') and int(r['rank']) >= minimum: s.add(int(r['rank']))
   except Exception: pass
print(len(s))
PY
}
while :; do
 n=$(count_target_done)
 printf '%s downloaded_rank22_plus=%s/%s\n' "$(date -u +%FT%TZ)" "$n" "$TARGET_COUNT" >> "$LOG"
 if [ "$n" -ge "$TARGET_COUNT" ]; then touch "$DONE"; printf '%s complete\n' "$(date -u +%FT%TZ)" >> "$LOG"; exit 0; fi
 if ! "$PYTHON" "$SCRIPT" --manifest "$MANIFEST" --output-dir "$OUT" --products-root "$OUT" --max-new 5 --min-rank "$MIN_RANK" --delay 5 --pause-every 5 --pause-seconds 60 >> "$LOG" 2>&1; then
  if tail -n 30 "$LOG" | grep -q 'disk safety stop'; then
   printf '%s stopped for disk safety; leaving checkpoint intact\n' "$(date -u +%FT%TZ)" >> "$LOG"
   exit 0
  fi
  printf '%s batch failed; retrying in 120s\n' "$(date -u +%FT%TZ)" >> "$LOG"
  sleep 120
 else
  n=$(count_target_done)
  printf '%s batch_end downloaded_rank22_plus=%s/%s; cooling 60s\n' "$(date -u +%FT%TZ)" "$n" "$TARGET_COUNT" >> "$LOG"
  sleep 60
 fi
done
