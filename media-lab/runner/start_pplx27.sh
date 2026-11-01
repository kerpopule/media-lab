#!/usr/bin/env bash
# PPLX Computer Qwen3.8-27B + DFlash2, private/local Spark inference.
# Stable media-lab-text alias lets Director survive governed PPLX/Flash swaps.
set -Eeuo pipefail
# Per-host paths come from config/local.env (see config/local.env.example).
. "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/local_env.sh"
MODEL_DIR="$HOME/models/pplx-computer-qwen-3-8-27b-dflash2-20260824"
IMAGE=vllm-dflash2:lmheadfix
# Keep PPLX resident and first in the memory budget while leaving the measured
# LTX decode floor intact. 0.30 left only ~69.4 GiB MemAvailable on the 121 GiB
# Spark (below LTX's 46 + 24 GiB decode admission requirement). A measured 0.27
# could not allocate the 4.13 GiB KV cache needed for 65,536 tokens; 0.28 is the
# bounded co-residency floor that preserves the full context. Override only for
# a separately measured run.
GPU_UTIL=${PPLX_GPU_UTIL:-0.28}
MAX_LEN=${PPLX_MAX_LEN:-65536}

[[ -f "$MODEL_DIR/config.json" && -f "$MODEL_DIR/draft/config.json" ]]
docker image inspect "$IMAGE" >/dev/null
if docker ps --format '{{.Names}}' | grep -Fxq qwen38-flash-next; then
  echo "refusing PPLX start while qwen38-flash-next is running" >&2
  exit 72
fi

docker rm -f qwen38-vllm >/dev/null 2>&1 || true
exec docker run -d --name qwen38-vllm --restart unless-stopped --init \
  --device nvidia.com/gpu=all --ipc=host \
  "${MEDIA_LAB_PUBLISH_8004[@]}" \
  -v "$MODEL_DIR":/models/active:ro \
  -v "$HOME/.cache/huggingface:/root/.cache/huggingface" \
  -v "$HOME/.cache/vllm:/root/.cache/vllm" \
  -v "$HOME/.cache/flashinfer:/root/.cache/flashinfer" \
  -e HF_HUB_OFFLINE=1 \
  --entrypoint vllm \
  "$IMAGE" \
  serve /models/active \
  --served-model-name pplx-computer-qwen-3-8-27b-dflash2-20260824 media-lab-text \
  --host 0.0.0.0 --port 8000 --tensor-parallel-size 1 \
  --gpu-memory-utilization "$GPU_UTIL" --max-model-len "$MAX_LEN" --max-num-seqs 2 \
  --max-num-batched-tokens 8192 --async-scheduling --enable-prefix-caching \
  --speculative-config '{"method":"dflash","model":"/models/active/draft","num_speculative_tokens":7}' \
  --reasoning-parser qwen3 --tool-call-parser qwen3_coder --enable-auto-tool-choice
