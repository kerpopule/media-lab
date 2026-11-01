#!/usr/bin/env bash
# Static-unit entrypoint for Sol-H3. All host paths and the exact delegated GPU
# fence arrive through %t/media-lab-sol-h3.env, written atomically by app.py.
set -Eeuo pipefail
: "${SOL_PKG:?missing SOL_PKG}"
: "${SOL_ROOT:?missing SOL_ROOT}"
: "${SOL_H3_SPARK_RUNTIME_ROOT:?missing SOL_H3_SPARK_RUNTIME_ROOT}"
: "${SOL_H3_SPARK_QWEN_IMAGE:?missing SOL_H3_SPARK_QWEN_IMAGE}"
: "${SOL_H3_SPARK_QWEN_WEIGHTS_ROOT:?missing SOL_H3_SPARK_QWEN_WEIGHTS_ROOT}"
: "${MEDIA_LAB_GPU_FENCE:?missing delegated GPU fence}"
: "${MEDIA_LAB_GPU_JOB_ID:?missing delegated GPU job}"
: "${MEDIA_LAB_GPU_ENGINE:?missing delegated GPU engine}"
[[ "$MEDIA_LAB_GPU_ENGINE" == "h3" ]] || { echo "delegated engine is not h3" >&2; exit 78; }
PYTHON="$SOL_ROOT/envs/stage2/bin/python"
SERVER="$HOME/media-lab-simple/runner/sol_engine_server.py"
[[ -x "$PYTHON" && -f "$SERVER" && -d "$SOL_PKG" ]] || {
  echo "Sol-H3 runtime paths are incomplete" >&2
  exit 78
}
cd "$SOL_PKG"
exec "$PYTHON" "$SERVER"
