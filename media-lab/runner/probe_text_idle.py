#!/usr/bin/env python3
"""Exit-code activity probe for guarded text-runtime swaps.

0 = idle, 4 = busy, 5 = unknown. The JSON body is safe to retain in a
transaction receipt and intentionally contains no prompts or credentials.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from qwen_activity import probe_text_activity  # noqa: E402

state, detail = probe_text_activity(("8004",), timeout=5)
print(json.dumps(detail, sort_keys=True))
raise SystemExit({"idle": 0, "busy": 4, "unknown": 5}.get(state, 5))
