#!/usr/bin/env bash
# Media Lab music render transaction. Env: JOB_ID. Reads $JOB/payload.json, writes $JOB/output/song.flac
set -Eeuo pipefail
umask 022
JOB_ID=${JOB_ID:?}
ROOT="$HOME/media-lab-simple"
JOB="$ROOT/jobs/$JOB_ID"
LOCK=/run/user/1000/spark-gpu.lock
OVERRIDE=$HOME/.config/systemd/user/media-lab-gpu-reservation.service.d/override.conf
D=$HOME/runtime/music3-iso
mkdir -p "$JOB/output"
exec > >(tee -a "$JOB/render.log") 2>&1
echo "STAGE=preflight"
RESV=false CPID=""
cleanup(){
  rc=$?; trap - EXIT INT TERM; set +e
  [ -n "$CPID" ] && kill "$CPID" 2>/dev/null
  flock -u 9 2>/dev/null || true; exec 9>&- 2>/dev/null || true
  if [ "$RESV" = true ]; then
    [ -e "$OVERRIDE" ] && mv "$OVERRIDE" "$OVERRIDE.mlab"; systemctl --user daemon-reload >/dev/null 2>&1
    systemctl --user reset-failed media-lab-gpu-reservation.service >/dev/null 2>&1
    systemd-run --user --unit=media-lab-gpu-reservation.service --property=Restart=always --property=RestartSec=2s /usr/bin/flock "$LOCK" /usr/bin/sleep infinity >/dev/null 2>&1
    [ -e "$OVERRIDE.mlab" ] && mv "$OVERRIDE.mlab" "$OVERRIDE"; systemctl --user daemon-reload >/dev/null 2>&1
  fi
  echo "STAGE=done rc=$rc"
  exit $rc
}
trap cleanup EXIT
trap 'exit 143' INT TERM
if systemctl --user is-active --quiet media-lab-gpu-reservation.service; then
  RESV=true
  mv "$OVERRIDE" "$OVERRIDE.mlab"; systemctl --user daemon-reload
  systemctl --user stop media-lab-gpu-reservation.service
  mv "$OVERRIDE.mlab" "$OVERRIDE"; systemctl --user daemon-reload
else
  # No reservation: if someone else (e.g. a production batch) holds the GPU lock, fail fast.
  flock -n "$LOCK" -c true || { echo "FAIL=gpu_lock_busy"; exit 62; }
fi
exec 9>"$LOCK"
flock -w 20 9 || { echo "FAIL=gpu_lock_busy"; exit 62; }
echo "STAGE=starting"
cd "$D/ComfyUI"
.venv/bin/python main.py --disable-api-nodes --listen 127.0.0.1 --port 8196 --disable-auto-launch --extra-model-paths-config extra_model_paths.yaml > "$JOB/comfy-server.log" 2>&1 &
CPID=$!
UP=false
for i in $(seq 1 90); do
  curl -fsS -m 3 http://127.0.0.1:8196/system_stats >/dev/null 2>&1 && { UP=true; break; }
  kill -0 "$CPID" 2>/dev/null || break
  sleep 2
done
[ "$UP" = true ] || { echo "FAIL=comfy_boot"; exit 63; }
R=$(curl -sS -H "Content-Type: application/json" --data-binary "{\"prompt\": $(cat "$JOB/payload.json")}" http://127.0.0.1:8196/prompt)
echo "SUBMIT=$R"
PID=$(python3 -c 'import json,sys; print(json.loads(sys.argv[1])["prompt_id"])' "$R")
echo "STAGE=generating"
OK=false
for i in $(seq 1 720); do
  H=$(curl -fsS -m 5 "http://127.0.0.1:8196/history/$PID" 2>/dev/null || echo "{}")
  if [ -n "$H" ] && [ "$H" != "{}" ]; then
    ST=$(python3 -c 'import json,sys; j=json.loads(sys.argv[1]); print(next(iter(j.values())).get("status",{}).get("status_str","?") if j else "?")' "$H" 2>/dev/null || echo "?")
    echo "STATUS=$ST"
    if [ "$ST" = success ]; then OK=true; break; fi
    if [ "$ST" = error ]; then echo "$H" > "$JOB/comfy-error.json"; break; fi
  fi
  sleep 5
done
[ "$OK" = true ] || { echo "FAIL=render_failed"; exit 64; }
F=$(ls -t "$D/ComfyUI/output/music3-lab/LAB_${JOB_ID}"*.flac 2>/dev/null | head -1)
[ -n "$F" ] || { echo "FAIL=no_output"; exit 65; }
cp "$F" "$JOB/output/song.flac"
echo "STAGE=encoding"
