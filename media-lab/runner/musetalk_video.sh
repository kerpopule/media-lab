#!/usr/bin/env bash
# MuseTalk 1.5 exact-audio talking-head render under the canonical Spark GPU lease.
# Usage: musetalk_video.sh CONFIG_YAML WORK_DIR RESULT_DIR JOB_LABEL
set -Eeuo pipefail
CONFIG=${1:?config yaml}
WORK=${2:?work directory}
RESULT=${3:?result directory}
LABEL=${4:?job label}
# Per-host paths come from config/local.env (see config/local.env.example).
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/local_env.sh"
LOCK=/run/user/1000/spark-gpu.lock
POOL=media-lab-pool.service
SEG=media-lab-segment.service
LTX=media-lab-ltx-engine
H3=media-lab-h3-engine
POOL_LOCK="$MEDIA_LAB_HOME/runner/pool_lock.sh"
IMAGE=media-lab-musetalk:v1.5-0a89dec
MODELS="$MEDIA_LAB_MODELS_ROOT/musetalk-v15"
PILOT="$MEDIA_LAB_HOME/pilot-status.json"
LOG="$RESULT/$LABEL.log"
mkdir -p "$RESULT"
# The talking-head source clip: MUSETALK_SOURCE, else WORK/pip-source.mp4.
SOURCE=${MUSETALK_SOURCE:-$WORK/pip-source.mp4}
for p in "$CONFIG" "$SOURCE" "$MODELS/model-manifest.json"; do
  [[ -s "$p" ]] || { echo "missing required MuseTalk input: $p" >&2; exit 2; }
done
docker image inspect "$IMAGE" >/dev/null
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
    if [[ "$pool_was" == active ]]; then bash "$POOL_LOCK" acquire >/dev/null 2>&1 || true; fi
    if [[ "$ltx_was" == true ]]; then docker start "$LTX" >/dev/null 2>&1 || true; fi
    if [[ "$h3_was" == true ]]; then docker start "$H3" >/dev/null 2>&1 || true; fi
    if [[ "$seg_was" == active ]]; then systemctl --user start "$SEG" >/dev/null 2>&1 || true; fi
  fi
  python3 - "$PILOT" "$LABEL" "$rc" <<'PY' || true
from pathlib import Path
import json,sys,time
p=Path(sys.argv[1]); p.write_text(json.dumps({"name":sys.argv[2],"status":"done" if sys.argv[3]=="0" else "error","exit_code":int(sys.argv[3]),"updated_at":time.time()})+"\n")
PY
  exit "$rc"
}
trap restore EXIT INT TERM
if [[ "$ltx_was" == true ]]; then docker stop --time 30 "$LTX" >/dev/null; fi
if [[ "$h3_was" == true ]]; then docker stop --time 30 "$H3" >/dev/null; fi
if [[ "$seg_was" == active ]]; then systemctl --user stop "$SEG"; fi
if [[ "$pool_was" == active ]]; then
  systemctl --user stop "$POOL"
  for _ in $(seq 1 50); do systemctl --user is-active --quiet "$POOL" || break; sleep 0.1; done
fi
exec 9>"$LOCK"
for _ in $(seq 1 50); do
  if flock -n -x 9; then locked=1; break; fi
  if [[ "$pool_was" == active ]] && systemctl --user is-active --quiet "$POOL"; then systemctl --user stop "$POOL"; fi
  sleep 0.1
done
[[ "$locked" == 1 ]] || { echo "MuseTalk could not acquire the canonical GPU lock after pool handoff" >&2; exit 7; }
if [[ "$seg_was" == active ]] && systemctl --user is-active --quiet "$SEG"; then systemctl --user stop "$SEG"; fi
python3 - "$PILOT" "$LABEL" <<'PY'
from pathlib import Path
import json,sys,time
Path(sys.argv[1]).write_text(json.dumps({"name":sys.argv[2],"status":"running","engine":"MuseTalk 1.5","started_at":time.time()})+"\n")
PY
printf 'MUSETALK15_START label=%s config=%s\n' "$LABEL" "$(basename "$CONFIG")" | tee "$LOG"
/usr/bin/time -v docker run --rm --gpus all --ipc=host \
  --network none \
  -v "$MODELS:/opt/MuseTalk/models:ro" \
  -v "$WORK:/work:ro" \
  -v "$RESULT:/results" \
  "$IMAGE" \
  --ffmpeg_path /usr/bin \
  --inference_config "/work/$(basename "$CONFIG")" \
  --result_dir /results \
  --unet_model_path /opt/MuseTalk/models/musetalkV15/unet.pth \
  --unet_config /opt/MuseTalk/models/musetalkV15/musetalk.json \
  --whisper_dir /opt/MuseTalk/models/whisper \
  --version v15 --use_float16 --batch_size 8 2>&1 | tee -a "$LOG"
exec 9>&-
locked=0
if [[ "$pool_was" == active ]]; then bash "$POOL_LOCK" acquire >/dev/null; pool_was=inactive; fi
if [[ "$ltx_was" == true ]]; then docker start "$LTX" >/dev/null; ltx_was=false; fi
if [[ "$h3_was" == true ]]; then docker start "$H3" >/dev/null; h3_was=false; fi
if [[ "$seg_was" == active ]]; then systemctl --user start "$SEG"; seg_was=inactive; fi
restored=1
trap - EXIT INT TERM
python3 - "$PILOT" "$LABEL" <<'PY'
from pathlib import Path
import json,sys,time
Path(sys.argv[1]).write_text(json.dumps({"name":sys.argv[2],"status":"done","engine":"MuseTalk 1.5","updated_at":time.time()})+"\n")
PY
printf 'MUSETALK15_DONE label=%s\n' "$LABEL" | tee -a "$LOG"
