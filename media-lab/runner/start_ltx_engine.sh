#!/usr/bin/env bash
# Start the persistent warm LTX-2.5 engine container (always-warm policy).
# NOTE: --network none is not possible (the shim serves HTTP); the port is
# published on 127.0.0.1 only, all other hardening kept from run_lab_render.sh.
set -Eeuo pipefail
NAME=media-lab-ltx-engine
: "${MEDIA_LAB_GPU_LEASE_DB:?controller GPU lease DB is required}"
: "${MEDIA_LAB_GPU_LOCK:?controller GPU lock is required}"
: "${MEDIA_LAB_GPU_TASK:?controller GPU task is required}"
[[ "$MEDIA_LAB_GPU_TASK" == t2va ]] || { echo "LTX requires t2va lease" >&2; exit 77; }
PYTHONPATH="$HOME/media-lab-simple" python3 -c \
  'from media_lab_core.gpu_lease_runtime import authorize_environment,open_protocol; raise SystemExit(0 if authorize_environment(open_protocol(),engine="ltx",task="t2va",phases=("load",)) else 77)'
PIPELINE_STATE=$HOME/media-lab-simple/pool/residency/ltx-pipeline
if [[ -n ${LTX_PIPELINE:-} ]]; then
  PIPELINE=$LTX_PIPELINE
elif [[ -f "$PIPELINE_STATE" ]]; then
  IFS= read -r PIPELINE < "$PIPELINE_STATE"
else
  PIPELINE=distilled
fi
case "$PIPELINE" in
  distilled|dev) ;;
  *) echo "unsupported LTX_PIPELINE=$PIPELINE" >&2; exit 64 ;;
esac
if [[ "$PIPELINE" == dev ]]; then
  L=${LTX_MODEL_DIR:-$HOME/.local/share/media-lab-p2-models/maestro-ltx25-dev}
  TRANSFORMER=${LTX_TRANSFORMER:-ltx-2.5-22b-dev-transformer-bf16.safetensors}
else
  L=${LTX_MODEL_DIR:-$HOME/.local/share/media-lab-p2-models/maestro-ltx25}
  TRANSFORMER=${LTX_TRANSFORMER:-ltx-2.5-22b-distilled_diffusion_model_int8_convrot.safetensors}
fi
OUT=$HOME/media-lab-simple/pool/ltx-out
mkdir -p "$OUT"
[[ -f "$L/$TRANSFORMER" ]] || { echo "missing LTX transformer: $L/$TRANSFORMER" >&2; exit 66; }
docker rm -f "$NAME" >/dev/null 2>&1 || true
docker run -d --name "$NAME" --restart no --gpus all --ipc host --user 1000:1000 \
  --cap-drop ALL --security-opt no-new-privileges --pids-limit 4096 --read-only \
  -p 127.0.0.1:8290:8290 \
  --tmpfs /tmp:rw,exec,nosuid,nodev,size=16g \
  --env HOME=/tmp --env PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \
  --env HF_HUB_OFFLINE=1 --env TRANSFORMERS_OFFLINE=1 \
  --env ENGINE=ltx25 --env PORT=8290 \
  --env MEDIA_LAB_GPU_LEASE_DB=/lease/gpu-lease.sqlite3 \
  --env MEDIA_LAB_GPU_LOCK=/lease/spark-gpu.lock \
  --env LTX_PIPELINE="$PIPELINE" --env LTX_TRANSFORMER="$TRANSFORMER" \
  --mount "type=bind,src=$L,dst=/models,readonly" \
  --mount "type=bind,src=$HOME/media-lab-simple/runner/engine_server.py,dst=/work/server.py,readonly" \
  --mount "type=bind,src=$HOME/media-lab-simple/media_lab_core,dst=/work/media_lab_core,readonly" \
  --mount "type=bind,src=$(dirname "$MEDIA_LAB_GPU_LEASE_DB"),dst=/lease" \
  --mount "type=bind,src=$MEDIA_LAB_GPU_LOCK,dst=/lease/spark-gpu.lock" \
  --mount "type=bind,src=$OUT,dst=/work/out" \
  --entrypoint python3 maestro-current-ltx25:t_1d1610d /work/server.py >/dev/null
echo STARTED
