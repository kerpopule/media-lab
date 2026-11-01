#!/bin/bash
# voice_enhance.sh IN.wav OUT.wav — Adobe-Podcast-style narration cleanup via
# resemble-enhance (CPU) in ~/voicefx-venv. Exit 0 + OUT on success; any other
# exit means "skip enhancement, use the original" (the app treats it as a no-op).
#
# --run_dir is REQUIRED: without it resemble-enhance tries `git lfs pull` on
# every run and git-lfs is not installed on the box. The checkpoint was fetched
# by hand (sha256-verified) into the venv's model_repo.
set -eu
IN="$1"; OUT="$2"
VE="$HOME/voicefx-venv/bin/resemble-enhance"
RUN_DIR="$HOME/voicefx-venv/lib/python3.12/site-packages/resemble_enhance/model_repo/enhancer_stage2"
[ -x "$VE" ] || exit 3
# ~8GB peak RAM — skip (don't OOM the box) when memory is tight
AVAIL=$(awk '/MemAvailable/{print int($2/1048576)}' /proc/meminfo)
[ "$AVAIL" -ge 14 ] || { echo "skipping enhance: only ${AVAIL}GB available" >&2; exit 5; }
TD=$(mktemp -d)
trap 'rm -rf "$TD"' EXIT
mkdir -p "$TD/in" "$TD/out"
cp "$IN" "$TD/in/a.wav"
"$VE" --device cpu --run_dir "$RUN_DIR" "$TD/in" "$TD/out" >&2
[ -s "$TD/out/a.wav" ] || exit 4
cp "$TD/out/a.wav" "$OUT"
