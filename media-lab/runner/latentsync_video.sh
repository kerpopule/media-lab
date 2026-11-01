#!/usr/bin/env bash
# Media Lab LatentSync 1.6 post-pass.
# Usage: latentsync_video.sh SOURCE_VIDEO VOCALS_DRIVE DEST_VIDEO SEED GUIDANCE MOUTH_LOCK_UNTIL JOB_DIR
set -Eeuo pipefail
SRC=${1:?source video}
DRIVE=${2:?vocals drive wav}
DST=${3:?destination video}
SEED=${4:?seed}
GUIDANCE=${5:?guidance scale}
MOUTH_LOCK_UNTIL=${6:?mouth lock until}
JOBDIR=${7:?job directory}
# Per-host paths come from config/local.env (see config/local.env.example).
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/local_env.sh"
ROOT="$MEDIA_LAB_RUNTIME_ROOT/LatentSync.stage"
LOCK=/run/user/1000/spark-gpu.lock
POOL=media-lab-pool.service
SEG=media-lab-segment.service
LTX=media-lab-ltx-engine
H3=media-lab-h3-engine
POOL_LOCK="$MEDIA_LAB_HOME/runner/pool_lock.sh"
LOCKER="$MEDIA_LAB_HOME/runner/lock_instrumental_mouth.py"
LOCK_PY="$ROOT/.venv/bin/python"
RAW="$JOBDIR/latentsync-raw.mp4"
NORM="$JOBDIR/latentsync-normalized.mkv"
VISUAL="$JOBDIR/latentsync-visual.mp4"
FRAMES="$JOBDIR/mouth-lock-frames"
PAD_DRIVE="$JOBDIR/latentsync-drive-padded.wav"
SRC_AAC="$JOBDIR/source-master.aac"
DST_AAC="$JOBDIR/delivery-master.aac"

for p in "$SRC" "$DRIVE" "$LOCKER" "$LOCK_PY" "$ROOT/configs/unet/stage2_512.yaml" \
  "$ROOT/checkpoints/latentsync_unet.pt" "$ROOT/checkpoints/whisper/tiny.pt"; do
  [[ -s "$p" ]] || { echo "missing required LatentSync input: $p" >&2; exit 2; }
done
mkdir -p "$JOBDIR" "$(dirname "$DST")"
rm -rf "$JOBDIR/latentsync-temp"
SRC_VIDEO_DUR=$(ffprobe -v error -select_streams v:0 -show_entries stream=duration -of default=nk=1:nw=1 "$SRC")
SRC_AUDIO_DUR=$(ffprobe -v error -select_streams a:0 -show_entries stream=duration -of default=nk=1:nw=1 "$SRC")
SRC_FPS=$(ffprobe -v error -select_streams v:0 -show_entries stream=avg_frame_rate -of default=nk=1:nw=1 "$SRC")
SRC_FRAMES=$(ffprobe -v error -count_frames -select_streams v:0 -show_entries stream=nb_read_frames -of default=nk=1:nw=1 "$SRC")
[[ "$SRC_VIDEO_DUR" != N/A && "$SRC_AUDIO_DUR" != N/A && "$SRC_FPS" != N/A && "$SRC_FRAMES" != N/A ]] || {
  echo "source timeline metadata is incomplete" >&2; exit 2;
}
# H3 pads its delivered soundtrack to the full generated timeline. Give
# LatentSync the same-length drive so it cannot silently truncate the tail.
ffmpeg -nostdin -v error -y -i "$DRIVE" -af apad -t "$SRC_AUDIO_DUR" \
  -ar 48000 -ac 1 -c:a pcm_s16le "$PAD_DRIVE"

pool_was=$(systemctl --user is-active "$POOL" 2>/dev/null || true)
seg_was=$(systemctl --user is-active "$SEG" 2>/dev/null || true)
ltx_was=$(docker inspect -f '{{.State.Running}}' "$LTX" 2>/dev/null || printf false)
h3_was=$(docker inspect -f '{{.State.Running}}' "$H3" 2>/dev/null || printf false)
locked=0
restored=0
restore() {
  rc=$?
  if [[ "$restored" != 1 ]]; then
    if [[ "$locked" == 1 ]]; then exec 9>&- || true; locked=0; fi
    if [[ "$pool_was" == active ]]; then "$POOL_LOCK" acquire >/dev/null 2>&1 || true; fi
    if [[ "$ltx_was" == true ]]; then docker start "$LTX" >/dev/null 2>&1 || true; fi
    if [[ "$h3_was" == true ]]; then docker start "$H3" >/dev/null 2>&1 || true; fi
    if [[ "$seg_was" == active ]]; then systemctl --user start "$SEG" >/dev/null 2>&1 || true; fi
  fi
  exit "$rc"
}
trap restore EXIT INT TERM

# Stop heavyweight residents before releasing the pool's canonical lease. A
# docker stop can legitimately consume its full grace period; releasing the
# pool first gives a residency reconciler time to reacquire it and deadlocks
# this job behind the pool it just released.
if [[ "$ltx_was" == true ]]; then docker stop --time 30 "$LTX" >/dev/null; fi
if [[ "$h3_was" == true ]]; then docker stop --time 30 "$H3" >/dev/null; fi
if [[ "$seg_was" == active ]]; then systemctl --user stop "$SEG"; fi
if [[ "$pool_was" == active ]]; then
  systemctl --user stop "$POOL"
  for _ in $(seq 1 50); do
    systemctl --user is-active --quiet "$POOL" || break
    sleep 0.1
  done
fi
exec 9>"$LOCK"
# The warm-pool reconciler can race the handoff even after an orderly stop.
# Re-stop only that exact captured lease and retry briefly; never wait forever
# or render around the canonical lock.
for _ in $(seq 1 50); do
  if flock -n -x 9; then locked=1; break; fi
  if [[ "$pool_was" == active ]] && systemctl --user is-active --quiet "$POOL"; then
    systemctl --user stop "$POOL"
  fi
  sleep 0.1
done
[[ "$locked" == 1 ]] || {
  echo "LatentSync could not acquire the canonical GPU lock after pool handoff" >&2
  exit 7
}
# A separate health reconciler may also have restarted segmentation during the
# handoff. Keep the captured conflicting service down for inference; restore()
# returns it to its exact prior state on every exit path.
if [[ "$seg_was" == active ]] && systemctl --user is-active --quiet "$SEG"; then
  systemctl --user stop "$SEG"
fi

echo "LATENTSYNC16_START seed=$SEED config=stage2_512 steps=20 guidance=$GUIDANCE drive=$(basename "$DRIVE") padded_to=${SRC_AUDIO_DUR}s source_frames=$SRC_FRAMES source_fps=$SRC_FPS mouth_lock_until=$MOUTH_LOCK_UNTIL"
cd "$ROOT"
HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_DATASETS_OFFLINE=1 \
CUDA_VISIBLE_DEVICES=0 .venv/bin/python -m scripts.inference \
  --unet_config_path configs/unet/stage2_512.yaml \
  --inference_ckpt_path checkpoints/latentsync_unet.pt \
  --inference_steps 20 --guidance_scale "$GUIDANCE" --enable_deepcache \
  --video_path "$SRC" --audio_path "$PAD_DRIVE" --video_out_path "$RAW" \
  --temp_dir "$JOBDIR/latentsync-temp" --seed "$SEED"
[[ -s "$RAW" ]] || { echo "LatentSync produced no video" >&2; exit 3; }

# LatentSync works at 25 fps. Return to the source master's exact frame cadence
# and visual duration in a lossless intermediate.
ffmpeg -nostdin -v error -y -i "$RAW" \
  -filter_complex "[0:v]fps=$SRC_FPS,trim=duration=$SRC_VIDEO_DUR,setpts=PTS-STARTPTS[v]" \
  -map "[v]" -an -c:v ffv1 "$NORM"
if [[ "$MOUTH_LOCK_UNTIL" != 0 && "$MOUTH_LOCK_UNTIL" != 0.0 ]]; then
  LOCK_FADE=$($LOCK_PY -c "print(max(0.001, float('$MOUTH_LOCK_UNTIL') - 0.04))")
  "$LOCK_PY" "$LOCKER" --video "$NORM" --closed-source "$SRC" \
    --frames-dir "$FRAMES" --until "$MOUTH_LOCK_UNTIL" --fade-start "$LOCK_FADE"
  ffmpeg -nostdin -v error -y -framerate "$SRC_FPS" -i "$FRAMES/frame-%06d.png" \
    -an -c:v libx264 -preset slow -crf 15 -pix_fmt yuv420p "$VISUAL"
else
  ffmpeg -nostdin -v error -y -i "$NORM" -an \
    -c:v libx264 -preset slow -crf 15 -pix_fmt yuv420p "$VISUAL"
fi
# Copy every original AAC packet; never ship the drive stem or clip the timeline.
ffmpeg -nostdin -v error -y -i "$VISUAL" -i "$SRC" \
  -map 0:v:0 -map 1:a:0 -c:v copy -c:a copy -movflags +faststart "$DST"
ffmpeg -nostdin -v error -i "$DST" -f null -
[[ -s "$DST" ]] || { echo "LatentSync package failed" >&2; exit 4; }
DST_FRAMES=$(ffprobe -v error -count_frames -select_streams v:0 -show_entries stream=nb_read_frames -of default=nk=1:nw=1 "$DST")
[[ "$DST_FRAMES" == "$SRC_FRAMES" ]] || {
  echo "LatentSync frame count drift: source=$SRC_FRAMES delivery=$DST_FRAMES" >&2; exit 5;
}
ffmpeg -nostdin -v error -y -i "$SRC" -map 0:a:0 -c copy -f adts "$SRC_AAC"
ffmpeg -nostdin -v error -y -i "$DST" -map 0:a:0 -c copy -f adts "$DST_AAC"
cmp "$SRC_AAC" "$DST_AAC" || {
  echo "LatentSync altered or truncated the delivery-master audio packets" >&2; exit 6;
}

exec 9>&-
locked=0
if [[ "$pool_was" == active ]]; then
  "$POOL_LOCK" acquire >/dev/null
  systemctl --user is-active --quiet "$POOL"
  pool_was=inactive
fi
if [[ "$ltx_was" == true ]]; then docker start "$LTX" >/dev/null; ltx_was=false; fi
if [[ "$h3_was" == true ]]; then docker start "$H3" >/dev/null; h3_was=false; fi
if [[ "$seg_was" == active ]]; then systemctl --user start "$SEG"; seg_was=inactive; fi
restored=1
trap - EXIT INT TERM
echo "LATENTSYNC16_DONE output=$DST"
