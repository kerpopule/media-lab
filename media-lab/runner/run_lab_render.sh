#!/usr/bin/env bash
# Media Lab simple render transaction. Env: JOB_ID, ENGINE(ltx25|h3), LAB_FRAMES, LAB_WIDTH, LAB_HEIGHT
set -Eeuo pipefail
umask 022

# Retired: this cold launcher owns neither the durable lease row nor its fence.
# Keeping it executable would preserve a direct GPU-start bypass around the
# canonical controller. Callers must use the queued warm-engine route instead.
echo "FAIL=retired_unfenced_cold_path" >&2
exit 77

JOB_ID=${JOB_ID:?}
ENGINE=${ENGINE:-ltx25}
ROOT="$HOME/media-lab-simple"
JOB="$ROOT/jobs/$JOB_ID"
LOCK=/run/user/1000/spark-gpu.lock
OVERRIDE=$HOME/.config/systemd/user/media-lab-gpu-reservation.service.d/override.conf
NAME="lab-render-$JOB_ID"
mkdir -p "$JOB/output" "$JOB/receipts"
exec > >(tee -a "$JOB/render.log") 2>&1
echo "STAGE=preflight"
if docker inspect maestro-gui >/dev/null 2>&1 && [ "$(docker inspect maestro-gui --format '{{.State.Running}}')" = "true" ]; then
  echo "FAIL=advanced_mode_busy"; exit 61
fi
RUN_RC=1 RESERVATION_WAS_ACTIVE=false
cleanup(){
  rc=$?; trap - EXIT INT TERM; set +e
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  flock -u 9 2>/dev/null || true; exec 9>&- 2>/dev/null || true
  if [ "$RESERVATION_WAS_ACTIVE" = true ]; then
    [ -e "$OVERRIDE" ] && mv "$OVERRIDE" "$OVERRIDE.lab"; systemctl --user daemon-reload >/dev/null 2>&1
    systemctl --user reset-failed media-lab-gpu-reservation.service >/dev/null 2>&1
    systemd-run --user --unit=media-lab-gpu-reservation.service --property=Restart=always --property=RestartSec=2s /usr/bin/flock "$LOCK" /usr/bin/sleep infinity >/dev/null 2>&1
    [ -e "$OVERRIDE.lab" ] && mv "$OVERRIDE.lab" "$OVERRIDE"; systemctl --user daemon-reload >/dev/null 2>&1
  fi
  echo "STAGE=done RUN_RC=$RUN_RC"
  [ "$RUN_RC" = 0 ] || exit 1
  exit $rc
}
trap cleanup EXIT
trap 'exit 143' INT TERM
if systemctl --user is-active --quiet media-lab-gpu-reservation.service; then
  RESERVATION_WAS_ACTIVE=true
  mv "$OVERRIDE" "$OVERRIDE.lab"; systemctl --user daemon-reload
  systemctl --user stop media-lab-gpu-reservation.service
  mv "$OVERRIDE.lab" "$OVERRIDE"; systemctl --user daemon-reload
fi
exec 9>"$LOCK"
flock -w 20 9 || { echo "FAIL=gpu_lock_busy"; exit 62; }
echo "STAGE=starting"
A=$HOME/.local/share/h3-authorized/models
S=$HOME/maestro-h3-native-proof/t_596ce2dd/assets/minimax_h3
C=$HOME/.local/share/maestro-r13-default-t_b05cae84/models
L=$HOME/.local/share/media-lab-p2-models/maestro-ltx25
if [ "$ENGINE" = h3 ]; then
  RUNNER=$ROOT/runner/render_lab_h3.py
  MOUNTS=(
    --mount "type=bind,src=$C/minimax_h3_fl2va_pruned_fp8_scaled.safetensors,dst=/models/transformer/minimax_h3_fl2va_pruned_fp8_scaled.safetensors,readonly"
    --mount "type=bind,src=$A/text_encoders/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors,dst=/models/qwen/qwen3vl_32b_minimax_h3_nvfp4_awq.safetensors,readonly"
    --mount "type=bind,src=$A/vae,dst=/models/minimax_h3/vae,readonly"
    --mount "type=bind,src=$S/processor,dst=/models/minimax_h3/processor,readonly"
    --mount "type=bind,src=$S/text_encoder,dst=/models/minimax_h3/text_encoder,readonly"
  )
else
  RUNNER=$ROOT/runner/render_lab_ltx25.py
  MOUNTS=( --mount "type=bind,src=$L,dst=/models,readonly" )
fi
docker create --name "$NAME" --restart no --gpus all --network none --ipc host --user 1000:1000 \
  --cap-drop ALL --security-opt no-new-privileges --pids-limit 4096 --read-only \
  --tmpfs /tmp:rw,exec,nosuid,nodev,size=16g \
  --env HOME=/tmp --env PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \
  --env HF_HUB_OFFLINE=1 --env TRANSFORMERS_OFFLINE=1 \
  --env LAB_FRAMES="${LAB_FRAMES:-121}" --env LAB_WIDTH="${LAB_WIDTH:-1280}" --env LAB_HEIGHT="${LAB_HEIGHT:-704}" \
  --mount "type=bind,src=$RUNNER,dst=/work/runner.py,readonly" \
  --mount "type=bind,src=$JOB/prompt.txt,dst=/work/prompt.txt,readonly" \
  --mount "type=bind,src=$JOB/output,dst=/work/output" \
  --mount "type=bind,src=$JOB/receipts,dst=/work/receipts" \
  "${MOUNTS[@]}" \
  --entrypoint python3 maestro-v1.8.0-h3-r13:t_b05cae84 /work/runner.py \
  --prompt-file /work/prompt.txt --output /work/output/result.mp4 \
  --payload /work/receipts/payload.json --receipt /work/receipts/generation.json >/dev/null
docker start "$NAME" >/dev/null
echo "STAGE=generating"
docker logs -f "$NAME" 2>&1 | grep --line-buffered -E "EXACT_MODEL_LOADS_COMPLETE|DEFAULT_SEED" | while read -r l; do echo "MARK=$l"; done &
docker wait "$NAME" > "$JOB/exit-code.txt"
EXIT_CODE=$(cat "$JOB/exit-code.txt")
docker logs "$NAME" > "$JOB/container.log" 2>&1 || true
if [ "$EXIT_CODE" = 0 ] && [ -s "$JOB/output/result.mp4" ]; then
  echo "STAGE=encoding"
  RUN_RC=0
else
  echo "FAIL=render_exit_$EXIT_CODE"
fi
