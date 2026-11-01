#!/usr/bin/env bash
# PPLX-only, fail-closed supervisor for the governed Spark text slot.
set -u
# Per-host paths come from config/local.env (see config/local.env.example).
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/local_env.sh"
ROOT="$MEDIA_LAB_HOME"
STATE_DIR="$ROOT/pool"
ACTIVE="$STATE_DIR/text-runtime-active"
MAINT="$ROOT/.chat-maintenance"
GRACE=900
mkdir -p "$STATE_DIR"

while true; do
  if [[ -e "$MAINT" ]]; then sleep 10; continue; fi
  mode=pplx
  name=qwen38-vllm
  starter="$ROOT/runner/start_pplx27.sh"
  printf '%s\n' "$mode" > "$ACTIVE.tmp" && mv "$ACTIVE.tmp" "$ACTIVE"
  if docker ps --format '{{.Names}}' | grep -Fxq qwen38-flash-next; then
    echo "$(date -u +%FT%TZ) standing down disabled Flash-Next runtime" >&2
    docker stop --time 30 qwen38-flash-next >/dev/null 2>&1 || true
  fi
  if ! docker inspect "$name" >/dev/null 2>&1; then
    echo "$(date -u +%FT%TZ) $mode container missing -> starting"
    "$starter" >/dev/null 2>&1 || true
    sleep "$GRACE"; continue
  fi
  running=$(docker inspect "$name" --format '{{.State.Running}}' 2>/dev/null || true)
  if [[ "$running" != true ]]; then
    echo "$(date -u +%FT%TZ) $mode container stopped -> starting"
    "$starter" >/dev/null 2>&1 || true
    sleep "$GRACE"; continue
  fi
  started=$(docker inspect "$name" --format '{{.State.StartedAt}}' 2>/dev/null || true)
  age=$(( $(date +%s) - $(date -d "$started" +%s 2>/dev/null || echo 0) ))
  if (( age > GRACE )); then
    code=$(curl -sS --max-time 10 -o /dev/null -w '%{http_code}' http://127.0.0.1:8004/health 2>/dev/null || true)
    if [[ "$code" != 200 ]]; then
      echo "$(date -u +%FT%TZ) $mode health=$code -> recreating"
      "$starter" >/dev/null 2>&1 || true
      sleep "$GRACE"; continue
    fi
  fi
  sleep 60
done
