#!/usr/bin/env python3
"""Crash-visible, fail-closed PPLX/Flash-Next text-slot transaction.

The canonical pool service must already own spark-gpu.lock. This controller
serializes only the mutation itself with media-lab-inference.lock, proves the
current scheduler idle, changes one exact container, verifies the OpenAI model
surface, and rolls back to the captured runtime on any failure.
"""
from __future__ import annotations

import fcntl
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT / "pool"
ACTIVE = STATE / "text-runtime-active"
MAINT = ROOT / ".chat-maintenance"
LOCK = Path("/run/user/1000/media-lab-inference.lock")
RECEIPTS = STATE / "text-runtime-receipts"
TEXT = {"pplx": "qwen38-vllm", "flash": "qwen38-flash-next"}
START = {
    "pplx": ROOT / "runner/start_pplx27.sh",
    "flash": ROOT / "runner/start_flash_next.sh",
}
VIDEO = {"media-lab-ltx-engine", "media-lab-h3-engine"}


def run(*args: str, timeout: int = 60, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(args, text=True, capture_output=True, timeout=timeout, check=check)


def running_containers() -> set[str]:
    out = run("docker", "ps", "--format", "{{.Names}}").stdout
    return {line.strip() for line in out.splitlines() if line.strip()}


def atomic_text(path: Path, value: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(value)
    os.replace(tmp, path)


def http_json(path: str, timeout: int = 10) -> dict:
    with urllib.request.urlopen(f"http://127.0.0.1:8004{path}", timeout=timeout) as res:
        return json.loads(res.read())


def wait_model(mode: str, timeout_s: int = 900) -> dict:
    expected = {
        "pplx": "pplx-computer-qwen-3-8-27b-dflash2-20260824",
        "flash": "qwen3.8-flash-next-ud-iq1-m",
    }[mode]
    deadline = time.monotonic() + timeout_s
    last = "not attempted"
    while time.monotonic() < deadline:
        try:
            body = http_json("/v1/models")
            offered = set()
            for row in body.get("data", []):
                offered.add(str(row.get("id")))
                offered.update(str(alias) for alias in (row.get("aliases") or []))
            if expected not in offered:
                raise RuntimeError(f"expected model {expected} missing; offered={sorted(offered)}")
            return body
        except Exception as exc:  # bounded warmup probe
            last = f"{type(exc).__name__}: {exc}"
            time.sleep(5)
    raise RuntimeError(f"text runtime did not become ready: {last}")


def prove_idle() -> dict:
    probe = run(str(ROOT / ".venv/bin/python"),
                str(ROOT / "runner/probe_text_idle.py"), check=False, timeout=20)
    try:
        detail = json.loads((probe.stdout or "{}").splitlines()[-1])
    except Exception:
        detail = {"state": "unknown", "detail": (probe.stderr or probe.stdout)[-300:]}
    if probe.returncode != 0:
        raise RuntimeError(
            f"current text runtime is not proven idle: state={detail.get('state')} "
            f"source={detail.get('source')}")
    return detail


def mem_available_gib() -> float:
    for line in Path("/proc/meminfo").read_text().splitlines():
        if line.startswith("MemAvailable:"):
            return int(line.split()[1]) / 1024 / 1024
    raise RuntimeError("MemAvailable unavailable")


def flash_floor_gib() -> float:
    receipt = Path.home() / "models/Qwen3.8-Flash-Next-UD-IQ1_M/MEDIA-LAB-RECEIPT.json"
    total = int(json.loads(receipt.read_text())["total_bytes"])
    return total / (1024 ** 3) + 24.0 + 4.0


def start(mode: str) -> None:
    proc = run("bash", str(START[mode]), timeout=120, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"{mode} starter failed: {(proc.stderr or proc.stdout)[-500:]}")
    wait_model(mode)


def stop_exact(name: str) -> None:
    proc = run("docker", "stop", "--time", "45", name, timeout=90, check=False)
    if proc.returncode != 0:
        raise RuntimeError(f"could not stop exact container {name}: {proc.stderr[-300:]}")


def current_mode(names: set[str]) -> str | None:
    found = [mode for mode, name in TEXT.items() if name in names]
    if len(found) > 1:
        raise RuntimeError("both PPLX and Flash text containers are running")
    return found[0] if found else None


def main() -> int:
    if len(sys.argv) != 2 or sys.argv[1] not in TEXT:
        print("usage: text_runtime_switch.py pplx|flash", file=sys.stderr)
        return 2
    target = sys.argv[1]
    STATE.mkdir(parents=True, exist_ok=True)
    RECEIPTS.mkdir(parents=True, exist_ok=True)
    LOCK.parent.mkdir(parents=True, exist_ok=True)
    with LOCK.open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        if run("systemctl", "--user", "is-active", "media-lab-pool.service",
               check=False).stdout.strip() != "active":
            raise RuntimeError("canonical media-lab-pool lock owner is not active")
        names = running_containers()
        before = current_mode(names)
        if target == "flash" and names & VIDEO:
            raise RuntimeError(
                f"Flash-Next requires an empty video slot; still running={sorted(names & VIDEO)}")
        if before == target:
            wait_model(target, 30)
            atomic_text(ACTIVE, target + "\n")
            print(json.dumps({"status": "already-active", "mode": target}))
            return 0
        idle = prove_idle() if before else {"state": "absent"}
        txid = f"{int(time.time())}-{os.getpid()}"
        receipt = {
            "id": txid, "started_at": time.time(), "from": before, "to": target,
            "pre_running": sorted(names), "idle_proof": idle, "status": "running",
        }
        receipt_path = RECEIPTS / f"{txid}.json"
        receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
        MAINT.write_text(f"text runtime transaction {txid}\n")
        restored = False
        try:
            if before:
                stop_exact(TEXT[before])
            if target == "flash":
                available = mem_available_gib()
                required = flash_floor_gib()
                receipt["flash_admission_gib"] = {
                    "available": round(available, 2), "required": round(required, 2),
                }
                if available < required:
                    raise RuntimeError(
                        f"Flash-Next admission needs {required:.1f} GiB including floor; "
                        f"only {available:.1f} GiB is available")
            start(target)
            atomic_text(ACTIVE, target + "\n")
            receipt.update({"status": "committed", "finished_at": time.time()})
            receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
            MAINT.unlink(missing_ok=True)
            print(json.dumps({"status": "committed", "from": before, "to": target,
                              "receipt": str(receipt_path)}))
            return 0
        except Exception as exc:
            receipt["error"] = f"{type(exc).__name__}: {exc}"
            try:
                now = running_containers()
                if TEXT[target] in now:
                    stop_exact(TEXT[target])
                if before:
                    start(before)
                    atomic_text(ACTIVE, before + "\n")
                restored = True
            except Exception as rollback_exc:
                receipt["rollback_error"] = f"{type(rollback_exc).__name__}: {rollback_exc}"
            receipt.update({"status": "rolled-back" if restored else "rollback-failed",
                            "finished_at": time.time()})
            receipt_path.write_text(json.dumps(receipt, indent=2) + "\n")
            if restored:
                MAINT.unlink(missing_ok=True)
            raise RuntimeError(f"text runtime switch failed; {receipt['status']}: {exc}") from exc


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:
        print(str(exc), file=sys.stderr)
        raise SystemExit(1)
