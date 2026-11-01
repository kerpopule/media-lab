#!/usr/bin/env python3
"""Topaz mastering worker — runs on the Mac, serves the Spark's queue.

The Spark (arm64) can't run Topaz; this Mac can, under the owner's Topaz
subscription. The studio drops master requests into pool/topaz-inbox on the
Spark; this worker pulls the clip, runs Topaz's tvai ffmpeg, and pushes the
result to pool/topaz-outbox. It heartbeats pool/topaz-worker.json so the UI
can say honestly whether mastering is available.

Setup (one time, by the owner):
  1. Install "Topaz Video" (topazlabs.com) and SIGN IN — the login is what
     unlocks watermark-free local rendering. This worker never sees the
     password; it only uses the app's own ffmpeg + auth on disk.
  2. python3 tools/topaz_worker.py            # or install the launchd plist

The worker self-reports: ready / no-app / auth-missing. Auth files expire
periodically — if status flips to auth-missing, open the Topaz app once.
"""
import glob
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

# ssh target of the studio host: MEDIA_LAB_SSH in config/local.env (or the env).
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from media_lab_core import local_config
SPARK = local_config.get("MEDIA_LAB_SSH")
RPOOL = "media-lab-simple/pool"
RMEDIA = "media-lab-simple/media"
MODEL = os.environ.get("TVAI_MODEL", "prob-4")     # Proteus v4 auto
SCALE = os.environ.get("TVAI_SCALE", "2")

APP_CANDIDATES = [
    "/Applications/Topaz Video AI.app",
    "/Applications/Topaz Video.app",
    os.path.expanduser("~/Applications/Topaz Video AI.app"),
]


def find_app():
    for a in APP_CANDIDATES:
        if os.path.isdir(a):
            return a
    return None


def find_ffmpeg(app):
    for c in (f"{app}/Contents/MacOS/ffmpeg", f"{app}/Contents/Resources/bin/ffmpeg"):
        if os.path.isfile(c):
            return c
    hits = glob.glob(f"{app}/Contents/**/ffmpeg", recursive=True)
    return hits[0] if hits else None


def auth_ok():
    roots = [os.path.expanduser("~/Library/Application Support/Topaz Labs LLC")]
    for r in roots:
        for f in glob.glob(f"{r}/**/auth.tpz", recursive=True):
            return True
    return False


def ssh(*args, **kw):
    return subprocess.run(["ssh", "-o", "BatchMode=yes", SPARK, *args],
                          capture_output=True, text=True, timeout=kw.pop("timeout", 60))


def heartbeat(status, detail=""):
    payload = json.dumps({"ts": time.time(), "status": status, "detail": detail})
    ssh(f"cat > {RPOOL}/topaz-worker.json <<'EOF'\n{payload}\nEOF")


def master(ffmpeg, app, job):
    jid, fname = job["id"], job["file"]
    tmp = tempfile.mkdtemp(prefix="topaz-")
    src = os.path.join(tmp, fname)
    out = os.path.join(tmp, f"{jid}.mp4")
    try:
        subprocess.run(["scp", "-q", f"{SPARK}:{RMEDIA}/{fname}", src], check=True, timeout=600)
        env = dict(os.environ)
        env.setdefault("TVAI_MODEL_DIR", f"{app}/Contents/Resources/models")
        env.setdefault("TVAI_MODEL_DATA_DIR",
                       os.path.expanduser("~/Library/Application Support/Topaz Labs LLC/Topaz Video AI/models"))
        cmd = [ffmpeg, "-nostdin", "-y", "-i", src,
               "-vf", f"tvai_up=model={MODEL}:scale={SCALE}",
               "-c:v", "hevc_videotoolbox", "-q:v", "60", "-tag:v", "hvc1",
               "-c:a", "copy", out]
        r = subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=40 * 60)
        if r.returncode != 0 or not os.path.getsize(out):
            raise RuntimeError((r.stderr or "ffmpeg failed")[-280:])
        subprocess.run(["scp", "-q", out, f"{SPARK}:{RPOOL}/topaz-outbox/{jid}.mp4.part"],
                       check=True, timeout=900)
        ssh(f"mv {RPOOL}/topaz-outbox/{jid}.mp4.part {RPOOL}/topaz-outbox/{jid}.mp4; "
            f"rm -f {RPOOL}/topaz-inbox/{jid}.json")
        print(f"mastered {jid}", flush=True)
    except Exception as e:
        msg = str(e).replace("'", "")[:280]
        ssh(f"printf '%s' '{msg}' > {RPOOL}/topaz-outbox/{jid}.err; "
            f"rm -f {RPOOL}/topaz-inbox/{jid}.json")
        print(f"FAILED {jid}: {e}", flush=True)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def main():
    print("Topaz worker up", flush=True)
    ssh(f"mkdir -p {RPOOL}/topaz-inbox {RPOOL}/topaz-outbox")
    while True:
        app = find_app()
        if not app:
            heartbeat("no-app", "Install Topaz Video on the Mac and sign in")
        elif not auth_ok():
            heartbeat("auth-missing", "Open Topaz Video once to refresh sign-in")
        else:
            ffmpeg = find_ffmpeg(app)
            if not ffmpeg:
                heartbeat("no-app", "Topaz ffmpeg binary not found inside the app")
            else:
                heartbeat("ready")
                r = ssh(f"ls {RPOOL}/topaz-inbox/*.json 2>/dev/null | head -1")
                path = r.stdout.strip()
                if path:
                    rj = ssh(f"cat {path}")
                    try:
                        job = json.loads(rj.stdout)
                        master(ffmpeg, app, job)
                    except Exception as e:
                        print(f"bad job file {path}: {e}", flush=True)
                        ssh(f"rm -f {path}")
        time.sleep(20)


if __name__ == "__main__":
    main()
