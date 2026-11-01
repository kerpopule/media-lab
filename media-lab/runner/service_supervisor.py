#!/usr/bin/env python3
"""Media Lab service supervisor — keeps the studio up without a human.

This is a PRODUCTION box: the studio, its models and its public URL must come
back on their own. systemd Restart=always covers a process that exits; it does
NOT cover the two failure modes that actually took us down:

  * the kernel OOM-kills a backend and its unit is left `failed`/gone, so every
    later render fails silently against a corpse (2026-08-17: the image engine
    was killed mid-edit and a say job waited on it for 15 minutes);
  * a wrapper service survives but latches a stale `rendering: true` while the
    backend it proxies is dead — healthy to systemd, useless in practice.

So this checks BEHAVIOUR (does the port answer, is the state sane), not just
liveness, and repairs what it finds. Runs from a 1-minute systemd timer.
Stdlib only.
"""
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
import sys
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from media_lab_core import local_config   # config/local.env, stdlib only
from media_lab_core import local_token    # local-token.txt, stdlib only

HOME = os.path.expanduser("~")
STATE = os.path.join(str(local_config.home()), "supervisor-state.json")
ENV = dict(os.environ,
           XDG_RUNTIME_DIR="/run/user/1000",
           DBUS_SESSION_BUS_ADDRESS="unix:path=/run/user/1000/bus")
LOCAL_OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# Restarting the same thing in a tight loop makes an outage worse, so each
# target gets a cooldown and a strike count before we act.
COOLDOWN_S = 300
STRIKES = 2

UNITS = [
    # (unit, probe url, what a healthy answer looks like)
    # media-lab-simple.service is NOT here on purpose (2026-09-26): the queue
    # watchdog is its one restart owner, with strikes and an hourly cap. Two
    # watchers restarting the studio caused the 2026-09-02 storm (189 restarts)
    # and every restart while H3 was warm used to cost a recovery hold.
    ("media-lab-image.service", "http://127.0.0.1:8295/health", None),
    ("media-lab-tunnel.service", None, None),
    # media-lab-pool.service is intentionally inactive in cold-idle mode.
    # SAM3 is intentionally absent: image_service starts it only around an
    # actual /segment request, then returns the memory to PPLX + LTX.
]

CONTAINERS = [
    # (container, probe url) — the always-on model servers
    ("voicebox", "http://127.0.0.1:17493/health"),
]

# The chat model is supervised by ENDPOINT, not by container name. It has been
# served by qwen38-vllm and (2026-08-18) a qwen38-sglang container appeared
# alongside it — someone is migrating backends. What matters is that :8003
# answers; which container provides it is not our business. And we never touch
# a container that was started in the last few minutes: that is somebody else
# working, and a restart would fight them mid-migration.
CHAT_PROBE = "http://127.0.0.1:8003/v1/models"
# Where the studio's text model really lives. On a box whose text model is
# served by another machine through the :8004 bridge, a dead :8003 is NOT a
# reason to start a local chat container (a ~34 GB model beside a warm H3).
# Local container repair is therefore opt-in: MEDIA_LAB_SUPERVISE_LOCAL_CHAT=1.
TEXT_BRIDGE = local_config.text_upstream() + "/v1/models"
TEXT_MODEL = "media-lab-text"
SUPERVISE_LOCAL_CHAT = local_config.int_value("MEDIA_LAB_SUPERVISE_LOCAL_CHAT", 0) == 1
CHAT_PREFIX = "qwen38-"
CHAT_STRIKES = 4          # ~4 minutes of a dead endpoint before we act
# Qwen3.8 27B can need more than five minutes to load from a cold container.
# A five-minute freshness guard killed healthy warmups just before readiness and
# created a clean SIGTERM/recreate loop. Stand clear for 15 minutes; after that,
# the normal strike/cooldown repair path still handles a genuinely wedged load.
FRESH_S = 900
CHAT_MAINTENANCE = os.path.join(HOME, "media-lab-simple", ".chat-maintenance")

# NOT supervised: media-lab-ltx-engine / media-lab-h3-engine. The app's versioned
# residency policy owns those. Most profiles select one video slot, while the
# admitted dual-video profile may intentionally retain both and evict Qwen. A
# generic supervisor that "repairs" either container would fight desired state.
# The app reconciles its committed profile once the queue drains, and the queue
# watchdog covers a wedged render.
APP_MANAGED = ("media-lab-ltx-engine", "media-lab-h3-engine")


def log(msg):
    print(f"[supervisor] {msg}", flush=True)


def load():
    try:
        with open(STATE) as f:
            return json.load(f)
    except Exception:
        return {}


def save(st):
    tmp = STATE + ".tmp"
    with open(tmp, "w") as f:
        json.dump(st, f)
    os.replace(tmp, STATE)


def probe_request(url):
    """The probe request. The studio behind the family door only answers a
    caller that proves it runs on this machine: local-token.txt in the
    X-Media-Lab-Local header, the same proof the queue watchdog sends. Without
    it every probe was a 401 in the studio's log (two a minute). headers_for
    attaches the token only to the studio's own address AND port, so the image
    service and every other probe carry nothing."""
    return local_token.authorize(urllib.request.Request(url))


def probe(url, timeout=10):
    if not url:
        return True
    try:
        with LOCAL_OPENER.open(probe_request(url), timeout=timeout) as r:
            return 200 <= r.getcode() < 400
    except urllib.error.HTTPError as e:
        return e.code < 500          # a 4xx still proves the server is alive
    except Exception:
        return False


def strike(st, key, reason, fix):
    """Two consecutive failures before acting; one cooldown per target."""
    n = st.get("strikes", {}).get(key, 0) + 1
    st.setdefault("strikes", {})[key] = n
    log(f"{key} unhealthy ({reason}) — strike {n}/{STRIKES}")
    if n < STRIKES:
        return
    last = st.get("last_fix", {}).get(key, 0)
    if time.time() - last < COOLDOWN_S:
        log(f"{key} still unhealthy but cooling down — not touching it")
        return
    log(f"REPAIRING {key}: {fix.__doc__ or fix}")
    fix()
    st.setdefault("last_fix", {})[key] = time.time()
    st["strikes"][key] = 0


def clear(st, key):
    if st.get("strikes", {}).get(key):
        log(f"{key} recovered")
    st.setdefault("strikes", {})[key] = 0


def unit_active(unit):
    r = subprocess.run(["systemctl", "--user", "is-active", unit],
                       env=ENV, capture_output=True, text=True)
    return r.stdout.strip() == "active"


def maestro_queue_runner_active():
    result = subprocess.run(
        ["pgrep", "-f", r"docker exec .*media-lab-maestro-runner\.py"],
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def restart_unit(unit):
    def fix():
        """restart the systemd unit"""
        # reset-failed first: a unit left `failed` after an OOM kill refuses to
        # start again until its failure state is cleared
        subprocess.run(["systemctl", "--user", "reset-failed", unit], env=ENV, check=False)
        subprocess.run(["systemctl", "--user", "restart", unit], env=ENV, check=False)
    return fix


def restart_container(name):
    def fix():
        """restart the docker container"""
        subprocess.run(["docker", "restart", name], capture_output=True, check=False)
    return fix


def text_bridge_models(timeout=10):
    try:
        with LOCAL_OPENER.open(TEXT_BRIDGE, timeout=timeout) as r:
            data = json.load(r)
        return [str(m.get("id")) for m in data.get("data", []) if isinstance(m, dict)]
    except Exception:
        return None


def check_text_bridge(st):
    """Report the remote text model's state; never start anything for it."""
    models = text_bridge_models()
    ok = models is not None and TEXT_MODEL in models
    was = st.get("text_bridge_ok")
    if ok != was:
        log(f"text bridge {'lists ' + TEXT_MODEL if ok else ('answers without ' + TEXT_MODEL if models is not None else 'is not answering')}"
            " (reported only; the text model is served remotely)")
    st["text_bridge_ok"] = ok


def main():
    st = load()

    for unit, url, _ in UNITS:
        key = f"unit:{unit}"
        if not unit_active(unit):
            strike(st, key, "systemd says not active", restart_unit(unit))
        elif not probe(url) and unit == "media-lab-simple.service" and maestro_queue_runner_active():
            log(f"{key} probe failed during a queue-owned Maestro render — standing clear")
            clear(st, key)
        elif not probe(url):
            strike(st, key, "active but its port does not answer", restart_unit(unit))
        else:
            clear(st, key)

    for name, url in CONTAINERS:
        key = f"docker:{name}"
        r = subprocess.run(["docker", "inspect", "-f", "{{.State.Running}}", name],
                           capture_output=True, text=True)
        running = r.stdout.strip() == "true"
        if not running:
            strike(st, key, "container is not running", restart_container(name))
        elif not probe(url, timeout=15):
            strike(st, key, "running but its port does not answer", restart_container(name))
        else:
            clear(st, key)

    # --- the text model, by endpoint ---
    key = "chat:qwen"
    if not SUPERVISE_LOCAL_CHAT:
        check_text_bridge(st)
    elif os.path.exists(CHAT_MAINTENANCE):
        clear(st, key)
        log(f"{key} maintenance marker present — standing clear")
    elif probe(CHAT_PROBE, timeout=15):
        clear(st, key)
    else:
        n = st.get("strikes", {}).get(key, 0) + 1
        st.setdefault("strikes", {})[key] = n
        log(f"{key} endpoint dead — strike {n}/{CHAT_STRIKES}")
        if n >= CHAT_STRIKES:
            names = subprocess.run(["docker", "ps", "-a", "--format", "{{.Names}}"],
                                   capture_output=True, text=True).stdout.split()
            cands = [c for c in names if c.startswith(CHAT_PREFIX)]
            fresh = []
            for c in cands:
                started = subprocess.run(
                    ["docker", "inspect", "-f", "{{.State.StartedAt}}", c],
                    capture_output=True, text=True).stdout.strip()
                # crude but dependency-free: a container started in the last few
                # minutes is someone else's work in progress
                if started[:16] and time.strftime("%Y-%m-%dT%H:%M", time.gmtime(
                        time.time() - FRESH_S)) <= started[:16]:
                    fresh.append(c)
            if fresh:
                log(f"chat containers {fresh} started recently — someone is working, standing clear")
            else:
                last = st.get("last_fix", {}).get(key, 0)
                if time.time() - last >= COOLDOWN_S and cands:
                    log(f"REPAIRING {key}: restarting {cands[0]}")
                    subprocess.run(["docker", "restart", cands[0]], capture_output=True, check=False)
                    st.setdefault("last_fix", {})[key] = time.time()
                    st["strikes"][key] = 0

    # --- orphaned GPU lock holders ---
    # The GPU flock should only ever be held by media-lab-pool.service or
    # media-lab-gpu-reservation.service. On 2026-08-17 two flock processes
    # belonging to NO unit were squatting on it (leftovers from transient units
    # that died), so every attempt to stand an engine up concluded "someone else
    # owns the GPU" and gave up — H3 takes failed indefinitely with no error
    # anywhere. Nothing else cleans these up.
    LOCK = os.environ.get("MEDIA_LAB_GPU_LOCK") or f"{os.environ.get('XDG_RUNTIME_DIR', '/run/user/' + str(os.getuid()))}/spark-gpu.lock"
    try:
        pids = subprocess.run(["fuser", LOCK], capture_output=True, text=True).stdout.split()
        orphans = []
        for pid in pids:
            try:
                with open(f"/proc/{pid}/cgroup") as f:
                    cg = f.read().strip().rsplit("/", 1)[-1]
                with open(f"/proc/{pid}/cmdline", "rb") as f:
                    cmd = f.read().replace(b"\x00", b" ").decode(errors="replace")
            except Exception:
                continue
            ours = LOCK in cmd and "flock" in cmd and "sleep" in cmd
            unowned = not cg.endswith(".service")
            if ours and unowned:
                orphans.append(pid)
        if orphans:
            log(f"GPU lock held by unit-less processes {orphans} — reclaiming")
            for pid in orphans:
                subprocess.run(["kill", pid], capture_output=True, check=False)
    except FileNotFoundError:
        pass                      # no fuser on this box; skip the check
    except Exception as e:
        log(f"lock check skipped: {e}")

    # The latch: the image wrapper claims to be rendering while the ComfyUI it
    # proxies is gone. Healthy to systemd, fatal to every job that needs a still.
    try:
        with urllib.request.urlopen("http://127.0.0.1:8295/health", timeout=10) as r:
            h = json.load(r)
        stuck = (bool(h.get("rendering")) and not bool((h.get("comfy") or {}).get("up"))
                 and not bool(h.get("booting")))
        key = "latch:image"
        if stuck:
            strike(st, key, "claims rendering while its ComfyUI backend is down",
                   restart_unit("media-lab-image.service"))
        else:
            clear(st, key)
    except Exception as e:
        log(f"could not read the image service health: {e}")

    # Docker restart policies drift when a container is recreated by hand.
    # unless-stopped on a video engine would resurrect it after the app stood it
    # down on purpose, so those are deliberately excluded here too.
    for name, _ in CONTAINERS:
        p = subprocess.run(["docker", "inspect", "-f", "{{.HostConfig.RestartPolicy.Name}}", name],
                           capture_output=True, text=True).stdout.strip()
        if p not in ("always", "unless-stopped"):
            log(f"{name} had restart policy '{p}' — pinning to unless-stopped")
            subprocess.run(["docker", "update", "--restart", "unless-stopped", name],
                           capture_output=True, check=False)

    save(st)


if __name__ == "__main__":
    main()
