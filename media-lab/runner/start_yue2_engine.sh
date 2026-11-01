#!/usr/bin/env bash
# Exec the YuE2 music engine shim (runner/yue2_engine_server.py) on its isolated
# venv. app.py runs this through `systemd-run --user --unit=media-lab-yue2.service`
# (ENGINES["yue2"]); config/media-lab-yue2.service is the same thing as a unit
# file for hosts that prefer `systemctl --user start`.
#
# Paths come from config/local.env (YUE2_KIT, YUE2_MODELS_ROOT, YUE2_PORT,
# MEDIA_LAB_HOME); see config/local.env.example. Nothing here names a machine.
set -Eeuo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck disable=SC1091
. "$HERE/local_env.sh"
_expand() { local v="$1"; printf '%s' "${v/#\~/$HOME}"; }
export YUE2_KIT="$(_expand "${YUE2_KIT:-$HOME/runtime/yue2-iso}")"
export YUE2_MODELS_ROOT="$(_expand "${YUE2_MODELS_ROOT:-$HOME/.local/share/media-lab-p3-models/yue2}")"
export YUE2_PORT="${YUE2_PORT:-8197}"
export YUE2_OUT_DIR="${YUE2_OUT_DIR:-$MEDIA_LAB_HOME/pool/music-out}"
export YUE2_BUDGET_GIB="${YUE2_BUDGET_GIB:-40}"
export HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1
export PYTORCH_CUDA_ALLOC_CONF=expandable_segments:True
export PYTHONUNBUFFERED=1
PY="$YUE2_KIT/.venv/bin/python"
[[ -x "$PY" ]] || { echo "YuE2 kit not installed: $PY missing (set YUE2_KIT in config/local.env)" >&2; exit 2; }
[[ -d "$YUE2_MODELS_ROOT/YuE2-3B" ]] || { echo "YuE2 weights missing under $YUE2_MODELS_ROOT" >&2; exit 2; }
mkdir -p "$YUE2_OUT_DIR"
cd "$YUE2_KIT"
exec "$PY" "$HERE/yue2_engine_server.py"
