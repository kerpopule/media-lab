#!/usr/bin/env python3
"""Read-only health probe, sent over ssh on stdin by spark_health_watch.py.

    ssh <host> python3 - studio [media-lab root]
    ssh <host> python3 - text   [sparky profile dir]

Prints ONE line of JSON. It reads files, asks local HTTP endpoints and runs
``systemctl --user is-active`` / ``show``; it never starts, stops, clears or
writes anything. The studio's local tool token is read in-process and sent
only to the studio's own address; it is never printed.
"""
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

HOME = Path.home()
NOW = time.time()
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

STUDIO_UNITS = ["media-lab-simple.service", "media-lab-image.service", "media-lab-tunnel.service",
                "text-upstream-bridge.service", "solh3-control-plane-guard.service",
                "spark-emergency-sshd.service"]


def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return None


def age(ts):
    try:
        return max(0.0, NOW - float(ts))
    except (TypeError, ValueError):
        return None


def get(url, headers=None, timeout=5.0):
    req = urllib.request.Request(url, headers=headers or {})
    try:
        with OPENER.open(req, timeout=timeout) as r:
            return r.getcode(), json.loads(r.read(2_000_000))
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except Exception:
        return 0, None


def boot_id():
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return ""


def disk_free_pct(path="/"):
    try:
        u = shutil.disk_usage(path)
        return round(100.0 * u.free / u.total, 1)
    except OSError:
        return None


def uptime_s():
    try:
        return float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        return None


def env_file(path):
    out = {}
    try:
        for line in Path(path).read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    return out


def units_state(units):
    out = {}
    for unit in units:
        try:
            r = subprocess.run(["systemctl", "--user", "is-active", unit],
                               capture_output=True, text=True, timeout=5)
            out[unit] = r.stdout.strip() or "unknown"
        except (OSError, subprocess.SubprocessError):
            out[unit] = "unknown"
    return out


def text_listed(url):
    code, body = get(url.rstrip("/") + "/v1/models", timeout=5)
    ids = [m.get("id") for m in (body or {}).get("data", [])] if isinstance(body, dict) else []
    return {"answering": code == 200, "listed": "media-lab-text" in ids}


def studio(root):
    root = Path(root or HOME / "media-lab-simple")
    cfg = env_file(root / "config" / "local.env")
    bind = cfg.get("MEDIA_LAB_BIND_HOST") or "127.0.0.1"
    if bind in ("0.0.0.0", "::"):
        bind = "127.0.0.1"
    base = f"http://{bind}:7863"
    token = ""
    try:
        token = (root / "local-token.txt").read_text().strip()
    except OSError:
        pass
    headers = {"X-Media-Lab-Local": token} if token else {}
    code, health = get(base + "/api/health", headers=headers, timeout=8)
    qcode, queue = get(base + "/api/queue?hist=0", headers=headers, timeout=8)
    del token, headers
    active = (queue or {}).get("active") if isinstance(queue, dict) else None
    q = {"answered": qcode == 200}
    if isinstance(active, list):
        running = [j for j in active if j.get("status") == "running"]
        queued = [j for j in active if j.get("status") == "queued"]
        q.update(queued=len(queued), running=len(running))
        oldest = [age(j.get("added") or j.get("ts")) for j in queued]
        q["oldest_queued_age_s"] = max([a for a in oldest if a is not None], default=None)
        over = False
        for j in running:
            started, est = j.get("started"), j.get("eta_total")
            if started and est and age(started) > 3 * float(est) * 60:
                over = True
        q["running_over_eta"] = over
    pool = root / "pool"
    hold_doc = read_json(pool / "gpu-recovery-hold.json")
    hold = {"exists": (pool / "gpu-recovery-hold.json").exists()}
    if isinstance(hold_doc, dict):
        hold.update(reason=hold_doc.get("reason"), job_id=hold_doc.get("job_id"),
                    age_s=age(hold_doc.get("created")))
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
    sol_root = Path(os.path.expanduser(cfg.get("SOL_ROOT") or "~/.local/share/sol-h3-spark"))
    guard = read_json(runtime / "solh3-control-plane-guard.json") or {}
    scode, sol_health = get("http://127.0.0.1:8291/health", timeout=4)
    bid = boot_id()
    permit = read_json(sol_root / "boot-clearance.json") or {}
    sol = {"configured": bool(cfg.get("SOL_PKG")),
           "loaded": bool((sol_health or {}).get("loaded")) if isinstance(sol_health, dict) else False,
           "task": (sol_health or {}).get("task") if isinstance(sol_health, dict) else None,
           "busy": (sol_health or {}).get("busy") if isinstance(sol_health, dict) else None,
           "boot_cleared": permit.get("approved") is True and permit.get("boot_id") == bid}
    thermal = None
    try:
        r = subprocess.run(["nvidia-smi", "--query-gpu=clocks_event_reasons_counters.hw_thermal_slowdown",
                            "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=10)
        thermal = float(r.stdout.strip().splitlines()[0]) if r.returncode == 0 and r.stdout.strip() else None
    except (OSError, ValueError, IndexError, subprocess.SubprocessError):
        thermal = None
    baton = root / ".operator-baton"
    deployed = read_json(root / "deployed-source.json") or {}
    try:
        nrestarts = int(subprocess.run(["systemctl", "--user", "show", "media-lab-simple.service",
                                        "-p", "NRestarts", "--value"], capture_output=True,
                                       text=True, timeout=5).stdout.strip() or 0)
    except (OSError, ValueError, subprocess.SubprocessError):
        nrestarts = None
    return {
        "kind": "studio", "boot_id": bid, "uptime_s": uptime_s(),
        "health": health if code == 200 and isinstance(health, dict) else None,
        "health_code": code, "queue": q, "hold": hold,
        "latch": (runtime / "flashnext-memwatch.latch").exists(),
        "safety_stop": (sol_root / "safety-stop.json").exists(),
        "safety_stop_reason": (read_json(sol_root / "safety-stop.json") or {}).get("reason"),
        "guard": {"state": guard.get("state"), "age_s": age(guard.get("written_at"))},
        "autorecover_gaveup": (pool / "autorecover-gaveup.json").exists(),
        "autorecover_gaveup_why": (read_json(pool / "autorecover-gaveup.json") or {}).get("why"),
        "autorecover_last": (read_json(pool / "autorecover-state.json") or {}).get("last_logged"),
        "handoff_pending": (pool / "gpu-handoff.json").exists(),
        "sol": sol, "sol_answered": scode == 200,
        "text": text_listed("http://127.0.0.1:8004"),
        "units": units_state(STUDIO_UNITS), "service_restarts": nrestarts,
        "disk_free_pct": disk_free_pct("/"), "thermal_hw_us": thermal,
        "maintenance_age_s": age(baton.stat().st_mtime) if baton.exists() else None,
        "deployed": {"tag": deployed.get("tag"), "commit": (deployed.get("commit") or "")[:12]},
    }


def text(profile):
    profile = Path(profile or HOME / ".hermes" / "profiles" / "sparky")
    state = read_json(HOME / "flashnext" / "text-model-state.json") or {}
    gw = read_json(profile / "gateway_state.json") or {}
    tg = ((gw.get("platforms") or {}).get("telegram") or {})
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR") or f"/run/user/{os.getuid()}")
    marker = HOME / ".cache" / "spark-text-switch" / "maintenance"
    return {
        "kind": "text", "boot_id": boot_id(), "uptime_s": uptime_s(),
        "text_state": {"state": state.get("state"), "active": state.get("active"),
                       "age_s": age_iso(state.get("stamp"))},
        "gateway": {"state": gw.get("gateway_state"), "telegram": tg.get("state"),
                    "age_s": age_iso(gw.get("updated_at"))},
        "memwatch_latch": (runtime / "flashnext-memwatch.latch").exists(),
        "local_text": text_listed("http://127.0.0.1:8004"),
        "disk_free_pct": disk_free_pct("/"),
        "maintenance_age_s": age(marker.stat().st_mtime) if marker.exists() else None,
    }


def age_iso(stamp):
    if not stamp:
        return None
    try:
        from datetime import datetime
        return age(datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def main(argv):
    kind = argv[1] if len(argv) > 1 else ""
    arg = argv[2] if len(argv) > 2 else None
    if kind == "studio":
        doc = studio(arg)
    elif kind == "text":
        doc = text(arg)
    else:
        doc = {"error": f"unknown probe kind {kind!r}"}
    print(json.dumps(doc, sort_keys=True))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
