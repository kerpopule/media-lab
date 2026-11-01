#!/usr/bin/env bash
set -Eeuo pipefail
ROOT=$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)
exec "$ROOT/.venv/bin/python" "$ROOT/runner/text_runtime_switch.py" "$@"
