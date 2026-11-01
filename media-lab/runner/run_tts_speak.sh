#!/usr/bin/env bash
# Chatterbox speak transaction. Env: JOB_ID, optional VOICE_WAV (reference for cloning).
# Reads $JOB/line.txt, writes $JOB/out/vo-1.wav (24k PCM16).
# GPU etiquette: proceed when WE own the lock (pool/reservation) or it is free;
# fail fast with the friendly busy path when an external batch owns the GPU.
set -Eeuo pipefail
JOB_ID=${JOB_ID:?}
ROOT="$HOME/media-lab-simple"
JOB="$ROOT/jobs/$JOB_ID"
LOCK=/run/user/1000/spark-gpu.lock
D=$HOME/.local/share/media-lab-tts/chatterbox
mkdir -p "$JOB/out"
exec > >(tee -a "$JOB/render.log") 2>&1
echo "STAGE=preflight"
if systemctl --user is-active --quiet media-lab-pool.service \
   || systemctl --user is-active --quiet media-lab-gpu-reservation.service; then
  : # we own the GPU lock — TTS is small and serialized through the app queue
elif ! flock -n "$LOCK" -c true; then
  echo "FAIL=gpu_lock_busy"; exit 62
fi
REF_MOUNT=()
GUIDE_ENV=()
if [ -n "${VOICE_WAV:-}" ] && [ -s "$VOICE_WAV" ]; then
  REF_MOUNT=(--mount "type=bind,src=$VOICE_WAV,dst=/work/ref.wav,readonly")
  GUIDE_ENV=(--env TTS_AUDIO_GUIDE=/work/ref.wav)
fi
echo "STAGE=generating"
docker run --rm --name "lab-tts-$JOB_ID" --gpus all --network none --ipc host \
  --user 1000:1000 --cap-drop ALL --security-opt no-new-privileges --pids-limit 4096 \
  --tmpfs /tmp:rw,exec,nosuid,nodev,size=8g \
  --env HOME=/tmp --env HF_HUB_OFFLINE=1 --env TRANSFORMERS_OFFLINE=1 \
  --env TTS_EXAGGERATION="${TTS_EXAGGERATION:-0.45}" --env TTS_PACE="${TTS_PACE:-0.35}" \
  "${GUIDE_ENV[@]}" \
  --mount "type=bind,src=$D,dst=/models/chatterbox,readonly" \
  --mount "type=bind,src=$D,dst=/opt/maestro/app/app/chatterbox,readonly" \
  --mount "type=bind,src=$ROOT/runner/tts_chatterbox_oneshot.py,dst=/work/tts.py,readonly" \
  --mount "type=bind,src=$ROOT/runner/wheels,dst=/work/wheels,readonly" \
  --mount "type=bind,src=$JOB/line.txt,dst=/work/copy.txt,readonly" \
  --mount "type=bind,src=$JOB/out,dst=/work/out" \
  "${REF_MOUNT[@]}" \
  --entrypoint python3 maestro-v1.8.0-h3-r13:t_b05cae84 /work/tts.py
[ -s "$JOB/out/vo-1.wav" ] || { echo "FAIL=no_output"; exit 65; }
echo "STAGE=encoding"
