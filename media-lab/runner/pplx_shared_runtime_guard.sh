#!/usr/bin/env bash
# Keep Perplexity Portable Computer on Media Lab's one canonical PPLX runtime.
# The desktop's optional Docker engine asks for 0.65 GPU memory and can OOM both
# the shared :8004 runtime and LTX. Stop only containers carrying Perplexity's
# exact local-engine label; never use a broad process/container kill.
set -Eeuo pipefail
LABEL='com.perplexity.computer.local-engine=vllm-docker'

stop_exact_conflict() {
  local id="$1" name label
  label=$(docker inspect -f '{{ index .Config.Labels "com.perplexity.computer.local-engine" }}' "$id" 2>/dev/null || true)
  [[ "$label" == vllm-docker ]] || return 0
  name=$(docker inspect -f '{{.Name}}' "$id" 2>/dev/null || true)
  name=${name#/}
  echo "$(date -u +%FT%TZ) stopping conflicting Perplexity local engine ${name:-$id}; canonical PPLX is :8004" >&2
  docker stop --time 10 "$id" >/dev/null 2>&1 || true
}

# Close any conflict that predates this service.
while IFS= read -r id; do
  [[ -n "$id" ]] && stop_exact_conflict "$id"
done < <(docker ps -q --filter "label=$LABEL")

# Then react to future launches before the duplicate 0.65 runtime can load.
docker events \
  --filter type=container \
  --filter event=start \
  --filter "label=$LABEL" \
  --format '{{.Actor.ID}}' |
while IFS= read -r id; do
  [[ -n "$id" ]] && stop_exact_conflict "$id"
done
