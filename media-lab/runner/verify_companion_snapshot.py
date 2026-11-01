#!/usr/bin/env python3
"""Read-only proof of the protected-PPLX + one-companion Spark invariant."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from media_lab_core import local_config   # config/local.env, stdlib only
from media_lab_core import local_token    # local-token.txt: this box's own API pass

# Runs on the studio machine: every urlopen() to its own API carries the local
# token (the studio no longer trusts a localhost Host header). Nothing else does.
local_token.install()

PPLX = local_config.text_upstream()
APP = local_config.studio_url()
FLOOR_GIB = 24.0


def get_json(url: str, timeout: int = 8):
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return response.status, json.loads(response.read())


def post_json(url: str, payload: dict, timeout: int = 120):
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.status, json.loads(response.read())


def optional_json(url: str):
    try:
        return get_json(url)[1]
    except Exception:
        return None


def mem_available_gib() -> float:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return round(int(line.split()[1]) / 1024 / 1024, 3)
    raise RuntimeError("MemAvailable missing")


def unit_rss_gib(unit: str) -> float:
    result = subprocess.run(
        ["systemctl", "--user", "show", unit, "-p", "MainPID", "--value"],
        capture_output=True, text=True,
    )
    try:
        pid = int(result.stdout.strip())
        if pid <= 0:
            return 0.0
        for line in Path(f"/proc/{pid}/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return round(int(line.split()[1]) / 1024 / 1024, 3)
    except Exception:
        pass
    return 0.0


models_status, models = get_json(f"{PPLX}/v1/models")
model_ids = [str(item.get("id") or "") for item in models.get("data", [])]
pplx_advertised = bool({"media-lab-text", "pplx-computer-qwen-3-8-27b-dflash2-20260824"} & set(model_ids))
probe_status, probe = post_json(
    f"{PPLX}/v1/chat/completions",
    {
        "model": "media-lab-text",
        "messages": [{"role": "user", "content": "Reply exactly PPLX_PRIMARY_OK"}],
        "max_tokens": 32,
        "temperature": 0,
        "chat_template_kwargs": {"enable_thinking": False},
    },
)
probe_text = probe.get("choices", [{}])[0].get("message", {}).get("content", "")
pplx_ok = models_status == 200 and probe_status == 200 and pplx_advertised and "PPLX_PRIMARY_OK" in probe_text

ltx = optional_json("http://127.0.0.1:8290/health")
h3 = optional_json("http://127.0.0.1:8291/health")
image = optional_json("http://127.0.0.1:8295/health") or {}
music = optional_json("http://127.0.0.1:8196/system_stats")
voice = optional_json("http://127.0.0.1:17493/models/status") or {}
queue = optional_json(f"{APP}/api/queue?hist=0") or {}

companions = []
if ltx and ltx.get("loaded"):
    companions.append("ltx")
if h3 and h3.get("loaded"):
    companions.append("h3")
if image.get("loaded_model"):
    companions.append(f"image:{image['loaded_model']}")
# A bare ComfyUI shell is cheap. Count Music 3 only when its process RSS proves
# heavyweight weights are resident; 2 GiB is far above an unloaded server.
music_rss = unit_rss_gib("media-lab-comfy-music.service") if music else 0.0
if music and music_rss >= 2.0:
    companions.append("music3")
loaded_voice = [m.get("model_name") for m in voice.get("models", []) if m.get("loaded")]
if loaded_voice:
    companions.append("voice:" + ",".join(map(str, loaded_voice)))

active_jobs = [item for item in queue.get("active", [])
               if item.get("status") in {"queued", "running"}]
available = mem_available_gib()
summary = {
    "time": time.time(),
    "pplx": {
        "ok": pplx_ok,
        "model_ids": model_ids,
        "probe_text": probe_text[:100],
    },
    "companions": companions,
    "exactly_one_companion": len(companions) == 1,
    "default_companion_is_ltx": companions == ["ltx"],
    "mem_available_gib": available,
    "operational_floor_gib": FLOOR_GIB,
    "floor_ok": available >= FLOOR_GIB,
    "active_jobs": active_jobs,
    "observations": {
        "ltx_health": ltx,
        "h3_health": h3,
        "image_loaded_model": image.get("loaded_model"),
        "music_service_up": music is not None,
        "music_rss_gib": music_rss,
        "voice_loaded_models": loaded_voice,
    },
}
summary["ok"] = bool(pplx_ok and len(companions) == 1 and available >= FLOOR_GIB)
print(json.dumps(summary, indent=2, sort_keys=True))
raise SystemExit(0 if summary["ok"] else 1)
