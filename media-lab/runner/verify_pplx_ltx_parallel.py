#!/usr/bin/env python3
"""Visible-queue LTX canary with concurrent PPLX availability and memory evidence."""
from __future__ import annotations

import hashlib
import json
import subprocess
import threading
import time
import urllib.error
import urllib.request
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from media_lab_core import local_config   # config/local.env, stdlib only
from media_lab_core import local_token    # local-token.txt: this box's own API pass

# Runs on the studio machine: every urlopen() to its own API carries the local
# token (the studio no longer trusts a localhost Host header). Nothing else does.
local_token.install()

ROOT = local_config.home()
REPORT = ROOT / "reports/pplx-ltx-co-residency-20260826"
APP = local_config.studio_url()
PPLX = local_config.text_upstream()
REPORT.mkdir(parents=True, exist_ok=True)


def request_json(url: str, payload=None, timeout=20):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"} if data is not None else {},
    )
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.status, json.loads(response.read())


def mem_available_kib() -> int:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1])
    raise RuntimeError("MemAvailable missing")


# Hard preconditions: no hidden/direct render and canonical pool lease is active.
for url in (f"{PPLX}/v1/models", "http://127.0.0.1:8290/health"):
    status, body = request_json(url, timeout=8)
    if status != 200:
        raise SystemExit(f"preflight failed: {url} -> {status}")
if subprocess.run(
    ["systemctl", "--user", "is-active", "--quiet", "media-lab-pool.service"]
).returncode:
    raise SystemExit("canonical GPU pool lock is not active")
status, queue = request_json(f"{APP}/api/queue?hist=0", timeout=8)
active = [x for x in queue.get("items", []) if x.get("status") in {"queued", "running"}]
if active:
    raise SystemExit(f"queue is not empty: {active}")

payload = {
    "prompt": (
        "A locked-off cinematic landscape shot of slow silver clouds drifting over "
        "a dark calm ocean at blue hour, restrained natural motion, no people, no text, "
        "one continuous take, soft ambient wind and distant surf."
    ),
    "style": "none",
    "duration": "3",
    "orientation": "landscape",
    "model": "ltx25",
    "seed": 260826,
}
status, submitted = request_json(f"{APP}/api/generate", payload, timeout=15)
job_id = submitted["id"]
(REPORT / "canary-submit.json").write_text(json.dumps({
    "status": status, "job_id": job_id, "payload": payload, "response": submitted,
}, indent=2) + "\n")

stop = threading.Event()
pplx_probes = []
mem_samples = []
probe_lock = threading.Lock()


def pplx_probe():
    seq = 0
    while not stop.is_set():
        seq += 1
        started = time.monotonic()
        record = {"seq": seq, "started": time.time()}
        try:
            code, body = request_json(
                f"{PPLX}/v1/chat/completions",
                {
                    "model": "media-lab-text",
                    "messages": [{"role": "user", "content": "Reply exactly PRIORITY_OK"}],
                    "max_tokens": 32,
                    "temperature": 0,
                    "chat_template_kwargs": {"enable_thinking": False},
                },
                timeout=180,
            )
            text = body.get("choices", [{}])[0].get("message", {}).get("content", "")
            record.update(code=code, ok=(code == 200 and "PRIORITY_OK" in text), text=text[:100])
        except Exception as exc:
            record.update(code=None, ok=False, error=str(exc)[:300])
        record["latency_s"] = round(time.monotonic() - started, 3)
        with probe_lock:
            pplx_probes.append(record)
        if stop.wait(15):
            break


def memory_probe():
    while not stop.is_set():
        mem_samples.append({"time": time.time(), "memavailable_kib": mem_available_kib()})
        stop.wait(2)


threads = [threading.Thread(target=pplx_probe), threading.Thread(target=memory_probe)]
for thread in threads:
    thread.start()

job = {}
deadline = time.monotonic() + 1800
try:
    while time.monotonic() < deadline:
        _, job = request_json(f"{APP}/api/jobs/{job_id}", timeout=15)
        (REPORT / "canary-job-latest.json").write_text(json.dumps(job, indent=2) + "\n")
        if job.get("status") in {"done", "error"}:
            break
        time.sleep(5)
    else:
        raise TimeoutError(f"LTX canary {job_id} exceeded 1800 seconds")
finally:
    stop.set()
    for thread in threads:
        thread.join(timeout=190)

if job.get("status") != "done":
    raise SystemExit(f"LTX canary failed: {job.get('message') or job.get('detail')}")

url = job.get("url") or ""
if not url.startswith("/media/"):
    raise SystemExit(f"canary has no Media Lab artifact URL: {url!r}")
video = ROOT / "media" / Path(url).name
if not video.is_file() or video.stat().st_size <= 0:
    raise SystemExit(f"canary artifact missing: {video}")

decode = subprocess.run(
    ["ffmpeg", "-v", "error", "-i", str(video), "-f", "null", "-"],
    text=True, capture_output=True,
)
probe = subprocess.run(
    ["ffprobe", "-v", "error", "-show_entries",
     "format=duration,size:stream=codec_type,codec_name,width,height,r_frame_rate,sample_rate,channels",
     "-of", "json", str(video)],
    text=True, capture_output=True, check=True,
)
sha256 = hashlib.sha256(video.read_bytes()).hexdigest()

ok_pplx = [p for p in pplx_probes if p.get("ok")]
summary = {
    "job_id": job_id,
    "status": job.get("status"),
    "url": url,
    "video": str(video),
    "bytes": video.stat().st_size,
    "sha256": sha256,
    "decode_exit": decode.returncode,
    "decode_stderr": decode.stderr[-1000:],
    "probe": json.loads(probe.stdout),
    "pplx_probes": pplx_probes,
    "pplx_all_ok": bool(pplx_probes) and len(ok_pplx) == len(pplx_probes),
    "pplx_max_latency_s": max((p.get("latency_s", 0) for p in pplx_probes), default=None),
    "mem_samples": len(mem_samples),
    "min_memavailable_kib": min((x["memavailable_kib"] for x in mem_samples), default=None),
    "min_memavailable_gib": round(min((x["memavailable_kib"] for x in mem_samples), default=0) / 1024 / 1024, 3),
    "operational_floor_gib": 24,
    "pool_active_after": subprocess.run(
        ["systemctl", "--user", "is-active", "--quiet", "media-lab-pool.service"]
    ).returncode == 0,
}
(REPORT / "canary-summary.json").write_text(json.dumps(summary, indent=2) + "\n")
(REPORT / "canary-memory.json").write_text(json.dumps(mem_samples, indent=2) + "\n")

if decode.returncode != 0:
    raise SystemExit("canary video decode failed")
if not summary["pplx_all_ok"]:
    raise SystemExit("one or more concurrent PPLX probes failed")
if summary["min_memavailable_gib"] < 24:
    raise SystemExit(f"memory floor violated: {summary['min_memavailable_gib']} GiB")
if not summary["pool_active_after"]:
    raise SystemExit("canonical GPU pool lock was not retained")
print(json.dumps(summary, indent=2))
