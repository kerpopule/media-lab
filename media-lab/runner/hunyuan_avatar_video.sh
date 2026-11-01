#!/usr/bin/env bash
# One-shot, visible-queue HunyuanVideo-Avatar canary on the DGX Spark.
# Usage: hunyuan_avatar_video.sh SOURCE_VIDEO VOCALS_DRIVE DEST_VIDEO SEED STEPS PROMPT JOB_DIR [FRAMES]
set -Eeuo pipefail
SRC=${1:?source video}
DRIVE=${2:?vocals drive}
DST=${3:?destination video}
SEED=${4:?seed}
STEPS=${5:?steps}
PROMPT=${6:?prompt}
JOBDIR=${7:?job directory}
FRAMES=${8:-129}
# Per-host paths come from config/local.env (see config/local.env.example).
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/local_env.sh"
LOCK=/run/user/1000/spark-gpu.lock
POOL=media-lab-pool.service
LTX=media-lab-ltx-engine
H3=media-lab-h3-engine
UNIT_RESIDENTS=(media-lab-segment.service media-lab-image.service media-lab-comfy-image.service media-lab-comfy-music.service)
CONTAINER_RESIDENTS=("$LTX" "$H3")
POOL_LOCK="$MEDIA_LAB_HOME/runner/pool_lock.sh"
RUNPY="$MEDIA_LAB_HOME/runner/hunyuan_avatar_once.py"
MODEL_DIR="$MEDIA_LAB_MODELS_ROOT/maestro-hunyuan-avatar"
MANIFEST="$MEDIA_LAB_HOME/research/hunyuan-avatar/model-manifest.json"
IMAGE_TAG=media-lab-hunyuan-avatar:v1.9.0-r3
IMAGE_ID=sha256:78961d13f4b4634f74eb28b9e078e85967e3f9456d9199a0b813d616b48e33c9
FPS=25
WIDTH=1120
HEIGHT=640
START="$JOBDIR/hva-start.png"
VISUAL="$JOBDIR/hva-visual.mp4"
AUDIO_PREFIX="$JOBDIR/hva-source-prefix.m4a"
SRC_AAC="$JOBDIR/hva-source-prefix.aac"
DST_AAC="$JOBDIR/hva-delivery.aac"
METRICS="$JOBDIR/hva-metrics.tsv"
CONTAINER="media-lab-hva-${JOBDIR##*/}"

[[ "$STEPS" == 30 ]] || { echo "Hunyuan Avatar canary is pinned to exactly 30 steps" >&2; exit 2; }
[[ "$FRAMES" == 17 || "$FRAMES" == 129 ]] || {
  echo "Hunyuan Avatar frames must be 17 (qualification) or 129 (full canary)" >&2; exit 2;
}
[[ "$SEED" =~ ^[0-9]+$ ]] && (( SEED <= 2147483647 )) || {
  echo "Hunyuan Avatar seed must be 0..2147483647" >&2; exit 2;
}
for p in "$SRC" "$DRIVE" "$RUNPY" "$POOL_LOCK" "$MANIFEST"; do
  [[ -s "$p" ]] || { echo "missing Hunyuan Avatar input/runtime file: $p" >&2; exit 2; }
done

# Verify every offline model/config file against the pinned manifest before any
# healthy service is stopped. A route-level size check is not sufficient here.
python3 - "$MANIFEST" "$MODEL_DIR" "$IMAGE_TAG" "$IMAGE_ID" <<'PY'
import hashlib, json, sys
from pathlib import Path
manifest_path, model_root, image_tag, image_id = sys.argv[1:]
manifest = json.loads(Path(manifest_path).read_text())
runtime = manifest.get("runtime") or {}
if runtime.get("image") != image_tag or runtime.get("image_id") != image_id:
    raise SystemExit("Hunyuan Avatar manifest runtime pin mismatch")
defaults = manifest.get("canary_defaults") or {}
expected = {"resolution": "1120x640", "fps": 25, "frames": 129, "steps": 30}
if any(defaults.get(k) != v for k, v in expected.items()):
    raise SystemExit("Hunyuan Avatar manifest canary defaults drift")
entries = (manifest.get("weights") or {}).get("files") or []
required = {
    "hunyuan_video_720_quanto_int8_map.json",
    "hunyuan_video_VAE_config.json",
    "hunyuan_video_VAE_fp32.safetensors",
    "hunyuan_video_avatar_720_quanto_bf16_int8.safetensors",
    "hunyuan_video_custom_VAE_config.json",
    "hunyuan_video_custom_VAE_fp32.safetensors",
    "llava-llama-3-8b/config.json",
    "llava-llama-3-8b/llava-llama-3-8b-v1_1_quanto_int8_map.json",
    "llava-llama-3-8b/llava-llama-3-8b-v1_1_vlm_quanto_int8.safetensors",
    "llava-llama-3-8b/preprocessor_config.json",
    "llava-llama-3-8b/special_tokens_map.json",
    "llava-llama-3-8b/tokenizer.json",
    "llava-llama-3-8b/tokenizer_config.json",
    "clip_vit_large_patch14/text_config.json",
    "clip_vit_large_patch14/merges.txt",
    "clip_vit_large_patch14/model.safetensors",
    "clip_vit_large_patch14/preprocessor_config.json",
    "clip_vit_large_patch14/special_tokens_map.json",
    "clip_vit_large_patch14/tokenizer.json",
    "clip_vit_large_patch14/tokenizer_config.json",
    "clip_vit_large_patch14/vocab.json",
    "whisper-tiny/config.json",
    "whisper-tiny/model.safetensors",
    "whisper-tiny/preprocessor_config.json",
    "whisper-tiny/special_tokens_map.json",
    "whisper-tiny/tokenizer_config.json",
    "det_align/detface.pt",
}
if {str(entry.get("path")) for entry in entries} != required:
    raise SystemExit("Hunyuan Avatar manifest does not exactly cover the offline loader contract")
seen = set()
root = Path(model_root).resolve()
for entry in entries:
    rel = entry.get("path")
    if not isinstance(rel, str) or rel in seen:
        raise SystemExit(f"invalid or duplicate Hunyuan Avatar manifest path: {rel!r}")
    seen.add(rel)
    path = (root / rel).resolve()
    if root not in path.parents or not path.is_file():
        raise SystemExit(f"missing Hunyuan Avatar manifest file: {rel}")
    if path.stat().st_size != entry.get("bytes"):
        raise SystemExit(f"Hunyuan Avatar size drift: {rel}")
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(16 * 1024 * 1024), b""):
            digest.update(block)
    if digest.hexdigest() != entry.get("sha256"):
        raise SystemExit(f"Hunyuan Avatar hash drift: {rel}")
print(f"HVA_MANIFEST_OK files={len(entries)} image_id={image_id}")
PY
actual_image_id=$(docker image inspect -f '{{.Id}}' "$IMAGE_TAG" 2>/dev/null || true)
[[ "$actual_image_id" == "$IMAGE_ID" ]] || {
  echo "Hunyuan Avatar runtime image drift: expected=$IMAGE_ID actual=$actual_image_id" >&2; exit 2;
}

SRC_AUDIO_CODEC=$(ffprobe -v error -select_streams a:0 -show_entries stream=codec_name -of default=nk=1:nw=1 "$SRC")
SRC_AUDIO_DUR=$(ffprobe -v error -select_streams a:0 -show_entries stream=duration -of default=nk=1:nw=1 "$SRC")
DRIVE_AUDIO_DUR=$(ffprobe -v error -select_streams a:0 -show_entries stream=duration -of default=nk=1:nw=1 "$DRIVE")
[[ "$SRC_AUDIO_CODEC" == aac && -n "$SRC_AUDIO_DUR" && "$SRC_AUDIO_DUR" != N/A ]] || {
  echo "Hunyuan Avatar canary requires a source video with readable AAC delivery audio" >&2; exit 2;
}
[[ -n "$DRIVE_AUDIO_DUR" && "$DRIVE_AUDIO_DUR" != N/A ]] || {
  echo "Hunyuan Avatar vocals-only drive is unreadable" >&2; exit 2;
}
mkdir -p "$JOBDIR" "$(dirname "$DST")"
rm -f "$DST" "$VISUAL" "$AUDIO_PREFIX" "$SRC_AAC" "$DST_AAC"
ffmpeg -nostdin -v error -y -i "$SRC" -frames:v 1 "$START"
[[ -s "$START" ]] || { echo "could not extract Hunyuan Avatar start frame" >&2; exit 2; }

pool_was=$(systemctl --user is-active "$POOL" 2>/dev/null || true)
declare -A UNIT_WAS=()
declare -A CONTAINER_WAS=()
resident_seen=0
for unit in "${UNIT_RESIDENTS[@]}"; do
  UNIT_WAS["$unit"]=$(systemctl --user is-active "$unit" 2>/dev/null || true)
  [[ "${UNIT_WAS[$unit]}" == active ]] && resident_seen=1
done
for container in "${CONTAINER_RESIDENTS[@]}"; do
  CONTAINER_WAS["$container"]=$(docker inspect -f '{{.State.Running}}' "$container" 2>/dev/null || printf false)
  [[ "${CONTAINER_WAS[$container]}" == true ]] && resident_seen=1
done
if [[ "$pool_was" != active && "$resident_seen" == 1 ]]; then
  echo "Hunyuan Avatar refuses an unsafe pre-state: GPU resident exists without the canonical pool lease" >&2
  exit 9
fi
locked=0
restored=0
restore_attempted=0
restore_result=0
monitor_pid=

restore_prior() {
  if [[ "$restore_attempted" == 1 ]]; then
    return "$restore_result"
  fi
  restore_attempted=1
  local rc=0 acquired=0
  if [[ "$locked" == 1 ]]; then
    exec 9>&-
    locked=0
  fi
  if [[ "$pool_was" == active ]]; then
    # A stopped transient unit can linger briefly in systemd. Retry the exact
    # canonical lease handoff here, once, rather than re-entering cleanup.
    for _ in $(seq 1 5); do
      systemctl --user reset-failed "$POOL" >/dev/null 2>&1 || true
      systemctl --user daemon-reload >/dev/null 2>&1 || true
      if "$POOL_LOCK" acquire >/dev/null 2>&1 && systemctl --user is-active --quiet "$POOL"; then
        acquired=1
        break
      fi
      sleep 2
    done
    [[ "$acquired" == 1 ]] || rc=90
  fi
  if (( rc != 0 )); then
    echo "Hunyuan Avatar restoration failed: canonical pool lease was not reacquired; GPU residents remain stopped" >&2
    restore_result=$rc
    return "$rc"
  fi
  for container in "${CONTAINER_RESIDENTS[@]}"; do
    if [[ "${CONTAINER_WAS[$container]}" == true ]]; then
      docker start "$container" >/dev/null 2>&1 || rc=91
      [[ $(docker inspect -f '{{.State.Running}}' "$container" 2>/dev/null || true) == true ]] || rc=91
    fi
  done
  for unit in "${UNIT_RESIDENTS[@]}"; do
    if [[ "${UNIT_WAS[$unit]}" == active ]]; then
      systemctl --user start "$unit" >/dev/null 2>&1 || rc=92
      systemctl --user is-active --quiet "$unit" || rc=92
    fi
  done
  (( rc == 0 )) || echo "Hunyuan Avatar restoration verification failed rc=$rc" >&2
  restore_result=$rc
  return "$rc"
}

cleanup() {
  local original_rc=$?
  local restore_rc=0
  trap - EXIT INT TERM
  set +e
  if [[ -n "${monitor_pid:-}" ]]; then
    kill "$monitor_pid" >/dev/null 2>&1
    wait "$monitor_pid" >/dev/null 2>&1
    monitor_pid=
  fi
  docker rm -f "$CONTAINER" >/dev/null 2>&1
  if [[ "$restored" != 1 ]]; then
    restore_prior
    restore_rc=$?
  fi
  if (( original_rc == 0 && restore_rc != 0 )); then original_rc=$restore_rc; fi
  exit "$original_rc"
}
on_int() { exit 130; }
on_term() { exit 143; }
trap cleanup EXIT
trap on_int INT
trap on_term TERM

# Stop every exact pool resident captured above, then release the captured pool
# lease and take the one canonical flock. A second sweep closes startup races;
# residents that were originally inactive remain inactive on restoration.
for container in "${CONTAINER_RESIDENTS[@]}"; do
  [[ "${CONTAINER_WAS[$container]}" == true ]] && docker stop --time 30 "$container" >/dev/null
done
for unit in "${UNIT_RESIDENTS[@]}"; do
  [[ "${UNIT_WAS[$unit]}" == active ]] && systemctl --user stop "$unit"
done
if [[ "$pool_was" == active ]]; then systemctl --user stop "$POOL"; fi
exec 9>"$LOCK"
for _ in $(seq 1 50); do
  if flock -n -x 9; then locked=1; break; fi
  if [[ "$pool_was" == active ]] && systemctl --user is-active --quiet "$POOL"; then
    systemctl --user stop "$POOL"
  fi
  sleep 0.1
done
[[ "$locked" == 1 ]] || { echo "Hunyuan Avatar could not acquire canonical GPU lock" >&2; exit 7; }
for container in "${CONTAINER_RESIDENTS[@]}"; do
  if [[ $(docker inspect -f '{{.State.Running}}' "$container" 2>/dev/null || true) == true ]]; then
    docker stop --time 30 "$container" >/dev/null
  fi
done
for unit in "${UNIT_RESIDENTS[@]}"; do
  systemctl --user is-active --quiet "$unit" && systemctl --user stop "$unit"
done

# Qwen is the one allowed co-resident. Whitelist every host PID in that exact
# container (the vLLM engine is a child of the container init PID), not merely
# Docker's State.Pid. Refuse anything else rather than silently overlapping HVA.
declare -A QWEN_PIDS=()
while read -r qpid _; do
  [[ "$qpid" =~ ^[0-9]+$ ]] && QWEN_PIDS["$qpid"]=1
done < <(docker top qwen38-vllm -eo pid 2>/dev/null || true)
while IFS= read -r gpu_pid; do
  [[ -z "$gpu_pid" ]] && continue
  [[ -n "${QWEN_PIDS[$gpu_pid]+allowed}" ]] && continue
  echo "Hunyuan Avatar found an unapproved GPU resident pid=$gpu_pid after handoff" >&2
  exit 10
done < <(nvidia-smi --query-compute-apps=pid --format=csv,noheader,nounits 2>/dev/null || true)

available_kb=$(python3 - <<'PY'
from pathlib import Path
for line in Path('/proc/meminfo').read_text().splitlines():
    if line.startswith('MemAvailable:'):
        print(line.split()[1]); break
PY
)
minimum_kb=$((70 * 1024 * 1024))
[[ "$available_kb" -ge "$minimum_kb" ]] || {
  echo "Hunyuan Avatar memory floor failed: available_kb=$available_kb need_kb=$minimum_kb" >&2
  exit 8
}

printf 'timestamp\tmem_available_kb\tswap_free_kb\tgpu_util_pct\n' >"$METRICS"
(
  while true; do
    read -r ma sf < <(python3 - <<'PY'
from pathlib import Path
v={}
for line in Path('/proc/meminfo').read_text().splitlines():
    k,*rest=line.replace(':','').split(); v[k]=rest[0]
print(v['MemAvailable'],v['SwapFree'])
PY
)
    gu=$(nvidia-smi --query-gpu=utilization.gpu --format=csv,noheader,nounits 2>/dev/null || printf NA)
    printf '%s\t%s\t%s\t%s\n' "$(date --iso-8601=seconds)" "$ma" "$sf" "$gu" >>"$METRICS"
    sleep 5
  done
) &
monitor_pid=$!

echo "HVA_CANARY_START seed=$SEED steps=$STEPS frames=$FRAMES fps=$FPS size=${WIDTH}x${HEIGHT} drive=$(basename "$DRIVE")"
docker run --name "$CONTAINER" --rm --network none --gpus all \
  -e HF_HUB_OFFLINE=1 -e TRANSFORMERS_OFFLINE=1 -e PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True \
  -v "$MODEL_DIR:/models:ro" \
  -v "$MEDIA_LAB_HOME/media:/media:ro" \
  -v "$RUNPY:/work/run.py:ro" \
  -v "$JOBDIR:/work/job" \
  --entrypoint python3 "$IMAGE_ID" /work/run.py \
  --image "/work/job/$(basename "$START")" \
  --audio "/media/$(basename "$DRIVE")" \
  --output "/work/job/$(basename "$VISUAL")" \
  --prompt "$PROMPT" --seed "$SEED" --steps "$STEPS" \
  --frames "$FRAMES" --width "$WIDTH" --height "$HEIGHT" &
docker_pid=$!
# Waiting on a background child lets Bash run INT/TERM traps immediately. A
# foreground docker client defers the shell trap and can leave inference alive.
set +e
wait "$docker_pid"
docker_rc=$?
set -e
docker_pid=
(( docker_rc == 0 )) || exit "$docker_rc"
[[ -s "$VISUAL" ]] || { echo "Hunyuan Avatar produced no visual" >&2; exit 3; }

IFS=, read -r OUT_WIDTH OUT_HEIGHT OUT_FPS OUT_FRAMES < <(
  ffprobe -v error -count_frames -select_streams v:0 \
    -show_entries stream=width,height,avg_frame_rate,nb_read_frames \
    -of csv=p=0 "$VISUAL"
)
[[ "$OUT_WIDTH" == "$WIDTH" && "$OUT_HEIGHT" == "$HEIGHT" && "$OUT_FPS" == "$FPS/1" && "$OUT_FRAMES" == "$FRAMES" ]] || {
  echo "Hunyuan Avatar visual contract drift: got=${OUT_WIDTH}x${OUT_HEIGHT}/${OUT_FPS}/${OUT_FRAMES} expected=${WIDTH}x${HEIGHT}/${FPS}/1/${FRAMES}" >&2
  exit 5
}
VIDEO_DUR=$(ffprobe -v error -select_streams v:0 -show_entries stream=duration -of default=nk=1:nw=1 "$VISUAL")
[[ -n "$VIDEO_DUR" && "$VIDEO_DUR" != N/A ]] || { echo "Hunyuan Avatar visual duration is unreadable" >&2; exit 5; }
ffmpeg -nostdin -v error -y -i "$SRC" -map 0:a:0 -t "$VIDEO_DUR" -c:a copy "$AUDIO_PREFIX"
ffmpeg -nostdin -v error -y -i "$VISUAL" -i "$AUDIO_PREFIX" \
  -map 0:v:0 -map 1:a:0 -c copy -movflags +faststart "$DST"
ffmpeg -nostdin -v error -i "$DST" -f null -
DST_FRAMES=$(ffprobe -v error -count_frames -select_streams v:0 -show_entries stream=nb_read_frames -of default=nk=1:nw=1 "$DST")
[[ "$DST_FRAMES" == "$FRAMES" ]] || {
  echo "Hunyuan Avatar frame count drift: expected=$FRAMES delivery=$DST_FRAMES" >&2; exit 5;
}
ffmpeg -nostdin -v error -y -i "$AUDIO_PREFIX" -map 0:a:0 -c copy -f adts "$SRC_AAC"
ffmpeg -nostdin -v error -y -i "$DST" -map 0:a:0 -c copy -f adts "$DST_AAC"
cmp "$SRC_AAC" "$DST_AAC" || {
  echo "Hunyuan Avatar altered the delivery-master audio packets" >&2; exit 6;
}

kill "$monitor_pid" >/dev/null 2>&1 || true
wait "$monitor_pid" >/dev/null 2>&1 || true
monitor_pid=
restore_prior
restored=1
trap - EXIT INT TERM
echo "HVA_CANARY_DONE output=$DST"
