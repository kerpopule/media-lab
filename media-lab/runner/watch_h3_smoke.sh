#!/usr/bin/env bash
# Robust canary-render watchdog (host-side, survives client disconnect).
# Polls :8292 until idle, then copies /work/out -> media-export + verifies.
set -u
OUT_DIR=~/media-lab-simple/h3-smoke-exports
mkdir -p "$OUT_DIR"
LOG=~/media-lab-simple/h3-smoke-exports/watch.log
echo "[$(date -u +%H:%M:%S)] watchdog start; max 240 polls x 45s ~3h" >> "$LOG"
CN=media-lab-h3-canary-v19
for i in $(seq 1 240); do
  B=$(curl -sS -m 5 http://127.0.0.1:8292/health 2>/dev/null || echo '{}')
  echo "[$(date -u +%H:%M:%S)] poll $i busy=$(echo "$B" | grep -oE '"busy":\s*(true|false)' | head -1)" >> "$LOG"
  if echo "$B" | grep -q '"busy": false'; then
    echo "[$(date -u +%H:%M:%S)] RENDER_IDLE at poll $i" >> "$LOG"
    CN=media-lab-h3-canary-v19
    SRC=$(docker exec "$CN" ls -t /work/out 2>/dev/null | head -1)
    if [ -n "$SRC" ]; then
      docker exec "$CN" tar -cf - -C /work/out "$SRC" 2>/dev/null | tar -xf - -C "$OUT_DIR" 2>/dev/null
      echo "[$(date -u +%H:%M:%S)] COLLECTED $SRC -> $OUT_DIR" >> "$LOG"
      # verify
      ffprobe -v error -show_entries format=duration,format_name,size -of json "$OUT_DIR/$SRC" 2>/dev/null > "$OUT_DIR/_verify.json" || echo "[collect] probe failed" >> "$LOG"
      echo READY:$(find "$OUT_DIR" -newer "$LOG" -name '*.mp4' 2>/dev/null | head -1) >> "$LOG"
    else
      echo "[$(date -u +%H:%M:%S)] idle but /work/out empty - render may have failed" >> "$LOG"
    fi
    exit 0
  fi
  sleep 25
done
echo "[$(date -u +%H:%M:%S)] TIMEOUT after 240 polls - render still busy" >> "$LOG"