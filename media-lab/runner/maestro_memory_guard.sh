#!/usr/bin/env bash
set -euo pipefail

NAME="${MAESTRO_CONTAINER:-maestro-gui}"
TARGET_BYTES=68719476736

if ! docker inspect "$NAME" >/dev/null 2>&1; then
  exit 0
fi

read -r current_memory current_swap < <(
  docker inspect "$NAME" --format '{{.HostConfig.Memory}} {{.HostConfig.MemorySwap}}'
)
if [[ "$current_memory" == "$TARGET_BYTES" && "$current_swap" == "$TARGET_BYTES" ]]; then
  exit 0
fi

docker update --memory 64g --memory-swap 64g "$NAME" >/dev/null
read -r verified_memory verified_swap < <(
  docker inspect "$NAME" --format '{{.HostConfig.Memory}} {{.HostConfig.MemorySwap}}'
)
if [[ "$verified_memory" != "$TARGET_BYTES" || "$verified_swap" != "$TARGET_BYTES" ]]; then
  echo "maestro memory limit verification failed: memory=$verified_memory swap=$verified_swap" >&2
  exit 1
fi
printf 'maestro memory limit repaired: memory=%s swap=%s\n' "$verified_memory" "$verified_swap"
