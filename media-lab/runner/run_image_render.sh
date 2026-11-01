#!/usr/bin/env bash
# Media Lab image render transaction (qwen-image-edit 4-step Lightning).
# Env: JOB_ID. Reads $JOB/payloads/*.json (filename_prefix lab-img/LAB_<job>_<name>),
# optional $JOB/input.png (uploaded to ComfyUI as lab_input_<job>.png), writes $JOB/output/<name>.png
set -Eeuo pipefail
umask 022
JOB_ID=${JOB_ID:?}
ROOT="$HOME/media-lab-simple"
JOB="$ROOT/jobs/$JOB_ID"
LOCK=/run/user/1000/spark-gpu.lock
OVERRIDE=$HOME/.config/systemd/user/media-lab-gpu-reservation.service.d/override.conf
D=$HOME/runtime/comfy-ltx25
mkdir -p "$JOB/output"
exec > >(tee -a "$JOB/render.log") 2>&1
echo "STAGE=preflight"
RESV=false CPID=""
cleanup(){
  rc=$?; trap - EXIT INT TERM; set +e
  [ -n "$CPID" ] && kill "$CPID" 2>/dev/null
  flock -u 9 2>/dev/null || true; exec 9>&- 2>/dev/null || true
  if [ "$RESV" = true ]; then
    [ -e "$OVERRIDE" ] && mv "$OVERRIDE" "$OVERRIDE.ilab"; systemctl --user daemon-reload >/dev/null 2>&1
    systemctl --user reset-failed media-lab-gpu-reservation.service >/dev/null 2>&1
    systemd-run --user --unit=media-lab-gpu-reservation.service --property=Restart=always --property=RestartSec=2s /usr/bin/flock "$LOCK" /usr/bin/sleep infinity >/dev/null 2>&1
    [ -e "$OVERRIDE.ilab" ] && mv "$OVERRIDE.ilab" "$OVERRIDE"; systemctl --user daemon-reload >/dev/null 2>&1
  fi
  echo "STAGE=done rc=$rc"
  exit $rc
}
trap cleanup EXIT
trap 'exit 143' INT TERM
if systemctl --user is-active --quiet media-lab-gpu-reservation.service; then
  RESV=true
  mv "$OVERRIDE" "$OVERRIDE.ilab"; systemctl --user daemon-reload
  systemctl --user stop media-lab-gpu-reservation.service
  mv "$OVERRIDE.ilab" "$OVERRIDE"; systemctl --user daemon-reload
else
  flock -n "$LOCK" -c true || { echo "FAIL=gpu_lock_busy"; exit 62; }
fi
exec 9>"$LOCK"
flock -w 20 9 || { echo "FAIL=gpu_lock_busy"; exit 62; }
echo "STAGE=starting"
cd "$D/ComfyUI"
.venv/bin/python main.py --disable-api-nodes --listen 127.0.0.1 --port 8195 --disable-auto-launch > "$JOB/comfy-server.log" 2>&1 &
CPID=$!
UP=false
for i in $(seq 1 90); do
  curl -fsS -m 3 http://127.0.0.1:8195/system_stats >/dev/null 2>&1 && { UP=true; break; }
  kill -0 "$CPID" 2>/dev/null || break
  sleep 2
done
[ "$UP" = true ] || { echo "FAIL=comfy_boot"; exit 63; }
if [ -s "$JOB/input.png" ]; then
  curl -fsS -F "image=@$JOB/input.png;filename=lab_input_${JOB_ID}.png" -F "overwrite=true" \
    http://127.0.0.1:8195/upload/image >/dev/null || { echo "FAIL=upload"; exit 66; }
fi
echo "STAGE=generating"
for PAYLOAD in "$JOB"/payloads/*.json; do
  NAME=$(basename "$PAYLOAD" .json)
  R=$(curl -sS -H "Content-Type: application/json" --data-binary "{\"prompt\": $(cat "$PAYLOAD")}" http://127.0.0.1:8195/prompt)
  echo "SUBMIT[$NAME]=$R"
  PID=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["prompt_id"])' "$R")
  OK=false
  for i in $(seq 1 240); do
    H=$(curl -fsS -m 5 "http://127.0.0.1:8195/history/$PID" 2>/dev/null || echo "{}")
    if [ -n "$H" ] && [ "$H" != "{}" ]; then
      ST=$(python3 -c 'import json,sys; j=json.loads(sys.argv[1]); print(next(iter(j.values())).get("status",{}).get("status_str","?") if j else "?")' "$H" 2>/dev/null || echo "?")
      if [ "$ST" = success ]; then OK=true; break; fi
      if [ "$ST" = error ]; then echo "$H" > "$JOB/comfy-error-$NAME.json"; break; fi
    fi
    sleep 3
  done
  [ "$OK" = true ] || { echo "FAIL=render_failed"; exit 64; }
  F=$(ls -t "$D/ComfyUI/output/lab-img/LAB_${JOB_ID}_${NAME}"*.png 2>/dev/null | head -1)
  [ -n "$F" ] || { echo "FAIL=no_output"; exit 65; }
  cp "$F" "$JOB/output/$NAME.png"
  echo "DONE=$NAME"
done
echo "STAGE=encoding"
