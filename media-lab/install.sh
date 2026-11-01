#!/bin/sh
#
# install.sh — one command from a clone to a paired Media Lab.
#
#   ./install.sh                  Linux: installs + starts a systemd --user
#                                 service; macOS: runs in the foreground
#   ./install.sh --service        macOS: install a launchd user agent instead
#   ./install.sh --port 7870      another port (default 7863)
#   ./install.sh --no-service     foreground on any OS (Ctrl-C stops it)
#   ./install.sh --detach         start in the background, print, exit
#   ./install.sh --yes            never ask (agents, CI)
#   ./install.sh --uninstall      remove the service + .venv; keep models/media
#
# Re-runnable and idempotent: it reuses the venv, never overwrites a code file
# or anything under the data root, and re-renders the service so `git pull &&
# ./install.sh` is the update path. It downloads nothing but Python packages —
# model weights and engines are installed later from the web UI's first-run
# shelf (GPU) or replaced by a fal.ai key (no GPU), by you, under your terms.
#
# What it prints at the end, every run: the URLs, the family code and a QR
# the VibeXStudio phone app scans to pair. `media-lab pair` prints it again.
# Two codes, both minted as everyday words and stored 0600 under the data root:
# the FAMILY code (one shared code; make, edit, Library) and the ADMIN code
# (the owner's: provider keys, engine installs, rotating the family code).
#
set -eu

# ---------------------------------------------------------------------------
# settings
# ---------------------------------------------------------------------------
REPO="$(CDPATH='' cd -- "$(dirname -- "$0")" && pwd)"
PORT=7863
BIND=0.0.0.0
SERVICE=default          # default | yes | no
DETACH=0
YES=0
UNINSTALL=0
MIN_PY_MAJOR=3
MIN_PY_MINOR=11

# app.py pins its data root to ~/media-lab-simple. MEDIA_LAB_HOME is honoured
# by keeping a symlink there, so the server still finds your directory.
APP_ROOT="$HOME/media-lab-simple"
ROOT="${MEDIA_LAB_HOME:-$APP_ROOT}"
VENV="$REPO/.venv"

if [ -t 1 ]; then
  B="$(printf '\033[1m')"; G="$(printf '\033[32m')"; Y="$(printf '\033[33m')"
  R="$(printf '\033[31m')"; N="$(printf '\033[0m')"
else
  B=""; G=""; Y=""; R=""; N=""
fi
say()  { printf '%s\n' "$*"; }
step() { printf '\n%s\n' "${B}== $* ==${N}"; }
ok()   { printf '  %s %s\n' "${G}ok${N}" "$*"; }
warn() { printf '  %s %s\n' "${Y}warn${N}" "$*"; }
die()  { printf '\n%s\n' "${R}install.sh: $*${N}" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }

usage() { sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; }

# ---------------------------------------------------------------------------
# flags
# ---------------------------------------------------------------------------
while [ $# -gt 0 ]; do
  case "$1" in
    --port)        PORT="$2"; shift ;;
    --port=*)      PORT="${1#--port=}" ;;
    --bind)        BIND="$2"; shift ;;
    --bind=*)      BIND="${1#--bind=}" ;;
    --service)     SERVICE=yes ;;
    --no-service)  SERVICE=no ;;
    --detach)      DETACH=1 ;;
    --yes|-y)      YES=1 ;;
    --uninstall)   UNINSTALL=1 ;;
    -h|--help)     usage; exit 0 ;;
    *)             die "unknown flag: $1 (try --help)" ;;
  esac
  shift
done
case "$PORT" in ''|*[!0-9]*) die "--port must be a number, got '$PORT'" ;; esac

# ---------------------------------------------------------------------------
# 1. platform
# ---------------------------------------------------------------------------
step "Media Lab installer"
OS="$(uname -s)"; ARCH="$(uname -m)"
case "$OS/$ARCH" in
  Linux/x86_64|Linux/aarch64) ok "Linux $ARCH" ;;
  Darwin/arm64)               ok "macOS $ARCH (Apple silicon)" ;;
  Darwin/x86_64)              warn "macOS on Intel — works, but nothing here is tested on it" ;;
  *)                          die "unsupported platform $OS/$ARCH (Linux x86_64/aarch64 or macOS arm64)" ;;
esac
[ "$SERVICE" = default ] && { [ "$OS" = Linux ] && SERVICE=yes || SERVICE=no; }
say "  repo      $REPO"
say "  data root $ROOT"
say "  port      $PORT   bind $BIND   service $SERVICE"

case "$REPO" in *[[:space:]]*) die "the repo path contains whitespace; systemd/launchd can't run from it — clone somewhere without spaces" ;; esac

# ---------------------------------------------------------------------------
# --uninstall
# ---------------------------------------------------------------------------
if [ "$UNINSTALL" = 1 ]; then
  step "Uninstall"
  PY="$VENV/bin/python"; [ -x "$PY" ] || PY="$(command -v python3 || true)"
  [ -n "$PY" ] || die "no python3 to run the uninstaller with"
  cd "$REPO"
  if [ "$YES" = 1 ]; then "$PY" -m media_lab_core.cli uninstall --yes
  else "$PY" -m media_lab_core.cli uninstall; fi
  [ -L "$HOME/.local/bin/media-lab" ] && rm -f "$HOME/.local/bin/media-lab" && ok "removed ~/.local/bin/media-lab"
  exit 0
fi

# ---------------------------------------------------------------------------
# 2. python >= 3.11 → .venv (uv when present, else python3 -m venv)
# ---------------------------------------------------------------------------
step "Python"
py_ok() { "$1" -c "import sys; sys.exit(0 if sys.version_info[:2] >= ($MIN_PY_MAJOR,$MIN_PY_MINOR) else 1)" 2>/dev/null; }

if [ -x "$VENV/bin/python" ] && py_ok "$VENV/bin/python"; then
  ok "reusing $VENV ($("$VENV/bin/python" -c 'import platform;print(platform.python_version())'))"
elif have uv; then
  say "  creating $VENV with uv"
  uv venv --quiet --python ">=$MIN_PY_MAJOR.$MIN_PY_MINOR" "$VENV" || die "uv venv failed"
  ok "venv ($("$VENV/bin/python" -c 'import platform;print(platform.python_version())'))"
else
  SYS_PY=""
  for cand in python3.13 python3.12 python3.11 python3; do
    if have "$cand" && py_ok "$cand"; then SYS_PY="$(command -v "$cand")"; break; fi
  done
  [ -n "$SYS_PY" ] || die "need Python >= $MIN_PY_MAJOR.$MIN_PY_MINOR. Install it (Debian/Ubuntu: sudo apt install python3.12 python3.12-venv; macOS: brew install python@3.12) or install uv (https://docs.astral.sh/uv/) and re-run."
  "$SYS_PY" -c 'import venv, ensurepip' >/dev/null 2>&1 || die "$SYS_PY lacks the venv module (Debian/Ubuntu: sudo apt install python3-venv)"
  [ -d "$VENV" ] && rm -rf "$VENV"
  "$SYS_PY" -m venv "$VENV" || die "python -m venv failed"
  ok "venv ($("$VENV/bin/python" -c 'import platform;print(platform.python_version())') via $SYS_PY)"
fi
PY="$VENV/bin/python"

step "Dependencies"
if have uv; then
  uv pip install --quiet --python "$PY" -r "$REPO/requirements.txt" || die "uv pip install failed"
else
  "$PY" -m pip install --quiet --disable-pip-version-check -r "$REPO/requirements.txt" || die "pip install failed"
fi
"$PY" -c 'import fastapi, uvicorn, pydantic, multipart, qrcode' || die "dependencies did not import — see the pip output above"
ok "fastapi, uvicorn, pydantic, python-multipart, qrcode"

for tool in ffmpeg ffprobe; do
  if have "$tool"; then ok "$tool"; else
    warn "$tool not found — uploads and video cutting need it (Debian/Ubuntu: sudo apt install ffmpeg; macOS: brew install ffmpeg). The server still starts."
  fi
done

# ---------------------------------------------------------------------------
# 3. data root, codes
# ---------------------------------------------------------------------------
step "Data root"
mkdir -p "$ROOT/jobs" "$ROOT/media" "$ROOT/logs"
if [ "$ROOT" != "$APP_ROOT" ]; then
  if [ -L "$APP_ROOT" ]; then
    ln -sfn "$ROOT" "$APP_ROOT"; ok "$APP_ROOT -> $ROOT (symlink refreshed)"
  elif [ -e "$APP_ROOT" ]; then
    die "MEDIA_LAB_HOME=$ROOT, but $APP_ROOT already exists as a real directory and app.py reads that path. Move it (mv $APP_ROOT $ROOT) or unset MEDIA_LAB_HOME, then re-run."
  else
    ln -s "$ROOT" "$APP_ROOT"; ok "$APP_ROOT -> $ROOT (symlink; app.py reads the former)"
  fi
else
  ok "$ROOT"
fi
# app.py resolves its code-shaped assets (static/, config/,
# runner/, the chat prompt) under the DATA root — that is how a deployed tree
# works. A clone links them to the repo so `git pull` updates them in place;
# a real directory (a hand-deployed copy) is left alone.
for item in static config runner chat-system-prompt.md; do
  if [ -L "$ROOT/$item" ]; then
    ln -sfn "$REPO/$item" "$ROOT/$item"
  elif [ -e "$ROOT/$item" ]; then
    ok "$item (a deployed copy under the data root — left alone)"
  else
    ln -s "$REPO/$item" "$ROOT/$item"
  fi
done
ok "static, config, runner, chat-system-prompt.md -> $REPO"

# Family code + admin code: word codes, created 0600, never overwritten (an
# existing install keeps its codes until you run `media-lab code --rotate`).
ACCESS_FILE="$ROOT/access-code.txt"; ADMIN_FILE="$ROOT/admin-pin.txt"
( cd "$REPO" && MEDIA_LAB_HOME="$ROOT" "$PY" -m media_lab_core.cli code --ensure >/dev/null ) \
  || die "could not create the door codes under $ROOT"
ok "family code ($ACCESS_FILE) and admin code ($ADMIN_FILE) ready — \`media-lab code\` shows them"

# ---------------------------------------------------------------------------
# 4. GPU
# ---------------------------------------------------------------------------
step "GPU"
if have nvidia-smi; then
  ok "NVIDIA GPU: $(nvidia-smi --query-gpu=name --format=csv,noheader 2>/dev/null | paste -sd, - || echo present)"
  say "  → the first-run shelf in the web UI can install local engines (LTX etc.); each shows its own terms and size first"
else
  warn "no NVIDIA GPU (nvidia-smi not found) — local engines are off"
  say "  → cloud-only: open the app, theme sheet → Cloud providers, paste your fal.ai key; fal-image/fal-video appear as engines"
fi

# ---------------------------------------------------------------------------
# 5. record choices, then start
# ---------------------------------------------------------------------------
cd "$REPO"
MLC="$PY -m media_lab_core.cli"
export MEDIA_LAB_HOME="$ROOT"
$MLC config --port "$PORT" --bind "$BIND" --service "$SERVICE" --venv "$VENV" >/dev/null

# a friendly `media-lab` on PATH (never overwrites something that isn't ours)
if [ -d "$HOME/.local/bin" ]; then
  if [ ! -e "$HOME/.local/bin/media-lab" ] || [ -L "$HOME/.local/bin/media-lab" ]; then
    ln -sfn "$REPO/tools/media-lab" "$HOME/.local/bin/media-lab"
  fi
fi

step "Start"
already_up() { $MLC wait --port "$PORT" --bind "$BIND" --timeout 1 >/dev/null 2>&1; }
SVC_STATE="$($MLC service state 2>/dev/null | "$PY" -c 'import json,sys; d=json.load(sys.stdin); print(d["state"], "managed" if d["managed"] else "unmanaged", d["name"])' 2>/dev/null || echo "absent unmanaged -")"
# shellcheck disable=SC2086  # three whitespace-free words by construction
set -- $SVC_STATE
SVC_RUNNING="$1"; SVC_MANAGED="$2"; SVC_NAME="$3"

if [ "$SVC_RUNNING" = running ] && [ "$SVC_MANAGED" = unmanaged ] && already_up; then
  # "I already run it": a hand-deployed service answers on this port. Leave it.
  ok "$SVC_NAME already runs a Media Lab on :$PORT (not installed by this script) — leaving it alone"
elif [ "$SERVICE" = yes ]; then
  $MLC service install || die "service install failed (use --no-service to run in the foreground)"
  ok "service installed"
  $MLC wait --port "$PORT" --bind "$BIND" --timeout 120 || die "the service did not answer /manifest.json within 120 s — run: $REPO/tools/media-lab logs"
  ok "answering on :$PORT"
else
  # No service: stop anything this script started before, then run uvicorn ourselves.
  $MLC stop >/dev/null 2>&1 || true
  if already_up; then
    die "port $PORT is already taken by another Media Lab (or something else) — pick another with --port, or stop it"
  fi
  if [ "$DETACH" = 1 ]; then
    $MLC start --timeout 120 || die "did not start — run: $REPO/tools/media-lab logs"
  else
    LOG="$ROOT/logs/media-lab.log"
    say "  foreground mode — Ctrl-C stops the studio. Log: $LOG"
    PYTHONUNBUFFERED=1 "$PY" -m uvicorn app:app --host "$BIND" --port "$PORT" >>"$LOG" 2>&1 &
    UV_PID=$!
    printf '%s\n' "$UV_PID" >"$ROOT/media-lab.pid"
    trap 'kill "$UV_PID" 2>/dev/null; rm -f "$ROOT/media-lab.pid"; exit 0' INT TERM
    $MLC wait --port "$PORT" --bind "$BIND" --pid "$UV_PID" --timeout 120 \
      || { tail -n 30 "$LOG" >&2; die "the server did not answer /manifest.json — last log lines above"; }
    ok "answering on :$PORT"
  fi
fi

# ---------------------------------------------------------------------------
# 6. summary + QR (every run)
# ---------------------------------------------------------------------------
$MLC pair
say "Next: ${B}$REPO/tools/media-lab status${N}  ·  ${B}media-lab pair${N} reprints this  ·  docs/INSTALL.md"
if [ "$SERVICE" = no ] && [ "$DETACH" = 0 ] && [ "${UV_PID:-}" != "" ]; then
  say "Running in the foreground (pid $UV_PID). Ctrl-C to stop; --service or --detach to keep it running without this terminal."
  wait "$UV_PID" || true
  rm -f "$ROOT/media-lab.pid"
fi
