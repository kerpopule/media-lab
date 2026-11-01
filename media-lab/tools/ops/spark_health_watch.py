#!/usr/bin/env python3
"""Watch the studio host and the text host from a third machine; speak only when a person must act.

Run every 5 minutes. Two ways:
  * ``--outbox`` under launchd (the deterministic scheduler): the check runs
    whether or not the chat gateway is up, and queues any message; a Hermes
    ``no_agent`` job runs ``--deliver`` every 5 minutes and prints the queued
    messages (printed text is delivered as-is, empty output is a silent run).
    ``--deliver`` also says so when the check itself has stopped running.
  * no flag: the check prints its message directly (a stdout-delivering job).
No LLM is involved. Stdlib only, Python 3.9+.

What it does each run:
  1. reads each host over ssh with a small read-only probe (probe_hosts.py,
     sent on stdin, so nothing is installed on the hosts);
  2. checks a few things from here: the public edge, the text bridge over the
     tailnet, a tiny completion every 15 minutes, this machine's disk and the
     age of the nightly backups;
  3. turns every failed check into a finding (warn or action) with the exact
     next step, applying "for N minutes" persistence so a blip is not an alert;
  4. sends a message only when a finding ENTERS action, reminds at most every
     6 hours, says "resolved" once if it had alerted, groups related failures
     (an unreachable host hides its own sub-checks), holds everything during
     quiet hours and sends one merged message when they end, and mutes a
     host's checks while a maintenance marker exists (at most 2 hours, then it
     says the marker was left behind);
  5. writes state.json, status.json (for spark-status), daily.json (warnings
     for the morning brief) and one line of history.

Dry run (SPARK_HEALTH_DRY_RUN=1, or "dry_run": true in the config): would-be
messages go to dry-run.log and nothing is printed, so nothing can be sent.

    spark_health_watch.py                 # one scheduled run
    spark_health_watch.py --status        # one plain line per host (last run)
    spark_health_watch.py --status --refresh   # check live now; alerts untouched
    spark_health_watch.py --status --json

Config: $SPARK_HEALTH_CONFIG or ~/.config/spark-health/config.json. See
docs/SPARK-HEALTH.md for every key. Host names and addresses live only there.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROBE = HERE / "probe_hosts.py"
DEFAULT_CONFIG = Path(os.path.expanduser("~/.config/spark-health/config.json"))
REMINDER_S = 6 * 3600
MARKER_MAX_S = 2 * 3600
SSH_CONNECT_S = 10
SSH_TOTAL_S = 30
COMPLETION_EVERY_S = 15 * 60
HOLD_GRACE_S = 20 * 60           # the harmless restart hold clears itself at 10 min
TRIP_GRACE_S = 40 * 60           # a cold-load guard trip clears itself at 15 min + reload
BOOT_GRACE_S = 50 * 60           # after a reboot the studio clears H3 + the carried-over hold
                                 # by itself (2 min .. 45 min, then it gives up with a reason)
GUARD_TRIP_REASONS = ("memory-psi", "low-memavailable", "swap-growth")
STALE_S = 20 * 60                # the watch itself has not run for this long


# ---------------------------------------------------------------- utilities

def now_local(ts: float, tz_offset_h: float | None = None) -> _dt.datetime:
    if tz_offset_h is None:
        return _dt.datetime.fromtimestamp(ts)
    return _dt.datetime.fromtimestamp(ts, _dt.timezone(_dt.timedelta(hours=tz_offset_h)))


def in_quiet_hours(ts: float, quiet: list | None, tz_offset_h: float | None = None) -> bool:
    if not quiet:
        return False
    start, end = int(quiet[0]), int(quiet[1])
    hour = now_local(ts, tz_offset_h).hour
    return (start <= hour or hour < end) if start > end else (start <= hour < end)


def read_json(path: Path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def write_json(path: Path, value) -> None:
    path = Path(path)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(json.dumps(value, indent=1, sort_keys=True))
    os.replace(tmp, path)


def http(url: str, *, timeout: float = 10.0, data: bytes | None = None,
         headers: dict | None = None):
    """(status, parsed-or-text body). status 0 = no answer."""
    # Cloudflare answers Python's stock user agent with a 403 (error 1010).
    req = urllib.request.Request(url, data=data,
                                 headers={"User-Agent": "SparkHealthWatch/1.0", **(headers or {})})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open(req, timeout=timeout) as r:
            body = r.read(200_000)
            status = r.getcode()
    except urllib.error.HTTPError as exc:
        return exc.code, None
    except Exception:
        return 0, None
    try:
        return status, json.loads(body)
    except ValueError:
        return status, body[:200].decode("utf-8", "replace")


def run_probe(target: str, kind: str, args: list | None = None,
              ssh_options: list | None = None) -> dict | None:
    """Run probe_hosts.py on ``target`` over ssh (script on stdin). None = unreachable."""
    cmd = ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={SSH_CONNECT_S}",
           *(ssh_options or []), target, "python3", "-", kind, *(args or [])]
    try:
        r = subprocess.run(cmd, input=PROBE.read_bytes(), capture_output=True,
                           timeout=SSH_TOTAL_S)
    except (subprocess.TimeoutExpired, OSError):
        return None
    if r.returncode != 0:
        return None
    try:
        doc = json.loads(r.stdout.decode("utf-8", "replace").strip().splitlines()[-1])
    except (ValueError, IndexError):
        return None
    return doc if isinstance(doc, dict) else None


# ---------------------------------------------------------------- findings

class Finding(dict):
    """{key, host, level, text, step}. A dict so it serializes as-is."""

    def __init__(self, key, host, level, text, step=""):
        super().__init__(key=key, host=host, level=level, text=text, step=step)


def studio_findings(name: str, p: dict, cfg: dict) -> list:
    """Findings for the studio host (the machine that runs Media Lab)."""
    out = []
    h = p.get("health") or {}
    step_hold = cfg.get("step_hold") or (
        "The studio clears this kind of hold by itself; it gave up or it is a kind "
        "it never touches. Look at pool/gpu-recovery-hold.json, then clear it only "
        "with tools/reconcile-gpu-recovery.py --job-id <lease job>.")
    hold = p.get("hold") or {}
    reason = str(hold.get("reason") or "")
    # A guard pressure trip during a cold load: the studio clears it by itself
    # once memory recovers (15 min + a ~6 min reload), at most once a day.
    guard_trip = (bool(p.get("safety_stop")) and reason.startswith("operation-uncertain:")
                  and p.get("safety_stop_reason") in GUARD_TRIP_REASONS)
    gaveup_why = p.get("autorecover_gaveup_why")
    gave_up = ("; auto-recover gave up" + (f": {gaveup_why}" if gaveup_why else "")
               if p.get("autorecover_gaveup") else "")
    uptime = p.get("uptime_s")
    after_reboot = reason == "durable-lease-recovery:boot-changed"
    if hold.get("exists"):
        age = float(hold.get("age_s") or 0)
        harmless = reason in (
            "durable-lease-recovery:owner-exited", "durable-lease-recovery:controller-restarted")
        grace = TRIP_GRACE_S if guard_trip else HOLD_GRACE_S
        if after_reboot:
            # 2026-09-27: the studio clears a carried-over hold by itself after a reboot.
            level = "action" if (p.get("autorecover_gaveup") or uptime is None
                                 or float(uptime) >= BOOT_GRACE_S) else "warn"
        else:
            level = "action" if (p.get("autorecover_gaveup") or age >= grace
                                 or not (harmless or guard_trip)) else "warn"
        last = p.get("autorecover_last")
        what = (f"Media Lab is on a GPU recovery hold for {age / 60:.0f} min "
                f"({reason or 'unknown reason'}"
                + (f", guard {p.get('safety_stop_reason')} trip" if guard_trip else "") + ")"
                + gave_up
                + ("; the host rebooted and the studio did not clear it by itself"
                   + (f" (last check: {last})" if last and not p.get("autorecover_gaveup") else "")
                   if after_reboot and level == "action" and not p.get("autorecover_gaveup") else "")
                + ("; it did not clear itself" if guard_trip and level == "action"
                   and not p.get("autorecover_gaveup") else ""))
        out.append(Finding("hold", name, level, what, step_hold))
    elif p.get("autorecover_gaveup"):
        out.append(Finding("autorecover", name, "action",
                           "Hold auto-recovery gave up earlier" + (f": {gaveup_why}" if gaveup_why else ""),
                           "Read pool/autorecover.log, then remove pool/autorecover-gaveup.json."))
    # With a hold, the stop and the latch are part of it (one message, not three).
    if p.get("safety_stop") and not hold.get("exists"):
        out.append(Finding("safety_stop", name, "action", "The H3 safety stop is set",
                           "Read the newest control-plane incident before clearing it."))
    if p.get("latch") and not hold.get("exists"):
        out.append(Finding("latch", name, "action",
                           "memwatch stopped H3 for low memory (latch set)",
                           "Check memory and kernel NVRM errors before clearing the latch."))
    q = p.get("queue") or {}
    if q.get("answered") is False:
        out.append(Finding("studio_api", name, "action", "The studio does not answer on its own port",
                           "Check `systemctl --user status media-lab-simple` on the studio host."))
    stuck = bool(h.get("queue", {}).get("stuck")) or bool(
        q.get("queued") and not q.get("running") and not hold.get("exists")
        and float(q.get("oldest_queued_age_s") or 0) >= 15 * 60)
    if stuck:
        out.append(Finding("queue_stuck", name, "action",
                           f"Queue stuck: {q.get('queued')} waiting, nothing running",
                           "Look at the studio journal; the watchdog restarts once, never twice."))
    if any("restart cap" in str(r) for r in (h.get("reasons") or [])):
        out.append(Finding("restart_capped", name, "action",
                           "The queue watchdog hit its restart cap (something keeps killing the studio)",
                           "Runbook 'Queue watchdog hit its restart cap': read the journal before anything else."))
    if q.get("running_over_eta"):
        out.append(Finding("job_slow", name, "action", "A running job is past 3x its estimate",
                           "Check the job in the studio; stop it only if it is really wedged."))
    for unit, state in sorted((p.get("units") or {}).items()):
        if state != "active":
            out.append(Finding(f"unit:{unit}", name, "action", f"{unit} is {state}",
                               f"`systemctl --user status {unit}` on the studio host."))
    text = p.get("text") or {}
    if not text.get("listed"):
        out.append(Finding("text_bridge", name, "action",
                           "The studio's text bridge does not list media-lab-text "
                           "(image, music and voice jobs are refused)",
                           "Check the text host first; then text-upstream-bridge on the studio host."))
    sol = p.get("sol") or {}
    if sol.get("configured") and not sol.get("boot_cleared"):
        # Since 2026-09-27 the studio clears H3 for a new boot by itself once its
        # checks pass (runner/hold_autorecover.py); only a give-up or a slow clear speaks.
        late = uptime is None or float(uptime) >= BOOT_GRACE_S
        if not (p.get("autorecover_gaveup") or late):
            out.append(Finding("h3_clearance", name, "warn",
                               "The studio host rebooted; the studio is clearing H3 for this boot "
                               "by itself", ""))
        elif not (hold.get("exists") and after_reboot):     # else the hold line already speaks
            last = p.get("autorecover_last")
            out.append(Finding("h3_clearance", name, "action",
                               "The studio host rebooted and H3 was not cleared automatically"
                               + (f": {gaveup_why}" if gaveup_why else
                                  (f" (last check: {last})" if last else "")),
                               cfg.get("step_clearance") or
                               "Read pool/autorecover.log on the studio host; then spark1-clear-h3."))
    elif sol.get("configured") and not sol.get("loaded") and not hold.get("exists") \
            and not q.get("running"):
        out.append(Finding("h3_cold", name, "warn", "H3 is cold (not warm in t2va)", ""))
        # The idle restore brings H3 back within ~15 min; an hour cold means it will not.
        out.append(Finding("h3_cold_long", name, "action",
                           "H3 has stayed cold for over an hour with nothing running",
                           "Read `journalctl --user -u media-lab-simple | grep -E 'h3-load|idle'` "
                           "on the studio host; the runbook's 'A hold that did not clear' covers the rest."))
    disk = p.get("disk_free_pct")
    if disk is not None and disk < 8:
        out.append(Finding("disk", name, "action", f"Disk {disk}% free", "Free space before renders fail."))
    elif disk is not None and disk < 15:
        out.append(Finding("disk", name, "warn", f"Disk {disk}% free", ""))
    if p.get("thermal_hw_rising"):
        out.append(Finding("thermal", name, "warn", "GPU hardware thermal slowdown is rising", ""))
    return out


def text_findings(name: str, p: dict, cfg: dict) -> list:
    out = []
    state = (p.get("text_state") or {}).get("state")
    if state != "serving":
        out.append(Finding("text_state", name, "action",
                           f"The text model is {state or 'unknown'}, not serving",
                           cfg.get("step_text") or "Check spark-text-switch status on the text host."))
    gw = p.get("gateway") or {}
    if gw.get("state") != "running" or gw.get("telegram") != "connected":
        out.append(Finding("sparky", name, "action",
                           f"Sparky's gateway is {gw.get('state') or 'unknown'} "
                           f"(Telegram {gw.get('telegram') or 'unknown'})",
                           "Check the Sparky gateway unit on the text host."))
    if p.get("memwatch_latch"):
        out.append(Finding("memwatch", name, "warn", "memwatch stopped the text engine", ""))
    disk = p.get("disk_free_pct")
    if disk is not None and disk < 8:
        out.append(Finding("disk", name, "action", f"Disk {disk}% free", "Free space."))
    elif disk is not None and disk < 15:
        out.append(Finding("disk", name, "warn", f"Disk {disk}% free", ""))
    return out


# ---------------------------------------------------------------- persistence

def apply_persistence(findings: list, st: dict, now: float, rules: dict) -> list:
    """Keep a finding only once it has lasted ``rules[key]`` seconds."""
    first = st.setdefault("first_seen", {})
    seen = set()
    kept = []
    for f in findings:
        k = f"{f['host']}:{f['key']}"
        seen.add(k)
        first.setdefault(k, now)
        need = rules.get(f["key"], rules.get(f["key"].split(":")[0], 0))
        if now - first[k] >= need:
            kept.append(f)
    for k in list(first):
        if k not in seen:
            del first[k]
    return kept


DEFAULT_PERSIST = {
    "unreachable": 15 * 60 - 60,  # 3 missed runs
    "unit": 10 * 60,
    "text_bridge": 15 * 60,
    "text_state": 15 * 60,
    "text_remote": 15 * 60,
    "sparky": 15 * 60,
    "public_edge": 15 * 60,
    "studio_api": 10 * 60,
    "completion": 0,            # already needs 2 misses 15 min apart
    "h3_cold": 30 * 60,
    "h3_cold_long": 60 * 60,
}


# ---------------------------------------------------------------- alerting

def plan_messages(findings: list, st: dict, now: float, *, quiet: bool,
                  muted_hosts: set) -> list:
    """Decide what to say. Mutates st["alerts"]. Returns message lines."""
    alerts = st.setdefault("alerts", {})
    actions = {f"{f['host']}:{f['key']}": f for f in findings
               if f["level"] == "action" and f["host"] not in muted_hosts}
    # An unreachable host hides its own sub-checks.
    down = {f["host"] for f in actions.values() if f["key"] == "unreachable"}
    actions = {k: f for k, f in actions.items()
               if f["key"] == "unreachable" or f["host"] not in down}
    lines = []
    for k, f in actions.items():
        a = alerts.get(k)
        if a is None:
            a = alerts[k] = {"since": now, "sent": None, "text": f["text"], "pending": True}
        a["text"] = f["text"]
        due = a["sent"] is None or (now - a["sent"] >= REMINDER_S)
        if due and not quiet:
            prefix = "" if a["sent"] is None else "Still: "
            line = f"{prefix}{f['text']}." + (f" Next: {f['step']}" if f.get("step") else "")
            lines.append(line)
            a["sent"] = now
            a["pending"] = False
    for k in list(alerts):
        if k in actions:
            continue
        host = k.split(":", 1)[0]
        if host in muted_hosts or host in down:
            continue           # muted or hidden: neither alert nor resolve now
        a = alerts.pop(k)
        if a.get("sent") is not None and not quiet:
            lines.append(f"Resolved: {a['text']}.")
        elif a.get("sent") is not None and quiet:
            st.setdefault("resolved_in_quiet", []).append(a["text"])
    if not quiet and st.get("resolved_in_quiet"):
        for text in st.pop("resolved_in_quiet"):
            lines.append(f"Resolved overnight: {text}.")
    return lines


def render(lines: list, now: float) -> str:
    if not lines:
        return ""
    stamp = _dt.datetime.fromtimestamp(now).strftime("%H:%M")
    return f"Spark check {stamp}\n" + "\n".join(f"- {line}" for line in lines)


# ---------------------------------------------------------------- the run

def maintenance(cfg: dict, now: float, host_markers: dict) -> tuple:
    """(muted host names, findings for markers left behind)."""
    muted, found = set(), []
    for m in cfg.get("maintenance_markers", []):
        path = Path(os.path.expanduser(m["path"]))
        if path.exists():
            age = now - path.stat().st_mtime
            if age > MARKER_MAX_S:
                found.append(Finding("marker", "monitor", "action",
                                     f"Maintenance marker {path} has been there {age / 3600:.1f} h",
                                     "Remove it if the work is finished."))
            else:
                muted.update(m.get("mutes", []))
    for host, age in host_markers.items():
        if age is None:
            continue
        if age > MARKER_MAX_S:
            found.append(Finding("marker", host, "action",
                                 f"A maintenance marker on {host} has been there {age / 3600:.1f} h",
                                 "Remove it if the work is finished."))
        else:
            muted.add(host)
    return muted, found


def collect(cfg: dict, st: dict, now: float, *, probe=run_probe, fetch=http) -> dict:
    """Probe every host and local check; return {hosts: {...}, findings: [...]}."""
    hosts, findings, host_markers = {}, [], {}
    # Probe the hosts in parallel so one slow host cannot push the run past
    # the scheduler's script timeout.
    from concurrent.futures import ThreadPoolExecutor
    items = list(cfg.get("hosts", {}).items())
    with ThreadPoolExecutor(max_workers=max(1, len(items))) as pool:
        futures = {name: pool.submit(probe, h["ssh"], h["role"], h.get("probe_args"),
                                     h.get("ssh_options")) for name, h in items}
    for name, h in items:
        doc = futures[name].result()
        hosts[name] = {"role": h["role"], "label": h.get("label", name), "probe": doc}
        if doc is None:
            findings.append(Finding("unreachable", name, "action",
                                    f"{h.get('label', name)} does not answer over ssh",
                                    h.get("step_unreachable") or "Check power and the network; "
                                    "the emergency door is the way in."))
            continue
        host_markers[name] = doc.get("maintenance_age_s")
        if doc.get("boot_id") and st.get("boot_ids", {}).get(name) not in (None, doc["boot_id"]):
            findings.append(Finding("rebooted", name, "warn", f"{h.get('label', name)} rebooted", ""))
        st.setdefault("boot_ids", {})[name] = doc.get("boot_id")
        if h["role"] == "studio":
            counter = doc.get("thermal_hw_us")
            prev = st.setdefault("thermal", {}).get(name)
            if isinstance(counter, (int, float)):
                if isinstance(prev, (int, float)) and counter - prev > 60e6:
                    doc["thermal_hw_rising"] = True
                st["thermal"][name] = counter
            findings += studio_findings(name, doc, h)
        elif h["role"] == "text":
            findings += text_findings(name, doc, h)
    for name, h in cfg.get("hosts", {}).items():
        label = h.get("label", name)
        if h.get("public_url"):
            code, _ = fetch(h["public_url"], timeout=15)
            hosts[name]["public_code"] = code
            if code == 0 or code >= 500:
                findings.append(Finding("public_edge", name, "action",
                                        f"{h['public_url']} answers {code or 'nothing'}",
                                        "Check media-lab-tunnel on the studio host."))
        if h.get("text_url"):
            code, body = fetch(h["text_url"].rstrip("/") + "/v1/models", timeout=10)
            ids = [m.get("id") for m in (body or {}).get("data", [])] if isinstance(body, dict) else []
            hosts[name]["text_listed"] = "media-lab-text" in ids
            if "media-lab-text" not in ids:
                findings.append(Finding("text_remote", name, "action",
                                        f"{label}'s text endpoint does not list media-lab-text",
                                        h.get("step_text") or "Check spark-text-switch on the text host."))
            last = float(st.get("last_completion", {}).get(name, 0))
            if now - last >= COMPLETION_EVERY_S - 30:
                payload = json.dumps({"model": "media-lab-text", "max_tokens": 4,
                                      "messages": [{"role": "user", "content": "Say OK."}]}).encode()
                code, body = fetch(h["text_url"].rstrip("/") + "/v1/chat/completions", timeout=30,
                                   data=payload, headers={"Content-Type": "application/json"})
                ok = code == 200 and isinstance(body, dict) and bool(body.get("choices"))
                st.setdefault("last_completion", {})[name] = now
                misses = 0 if ok else int(st.get("completion_misses", {}).get(name, 0)) + 1
                st.setdefault("completion_misses", {})[name] = misses
                hosts[name]["completion_ok"] = ok
            if int(st.get("completion_misses", {}).get(name, 0)) >= 2:
                findings.append(Finding("completion", name, "action",
                                        f"{label}'s text model failed a 4-token test twice",
                                        h.get("step_text") or "Check the text engine log."))
    local = cfg.get("local") or {}
    if local.get("disk_path"):
        try:
            free_gb = shutil.disk_usage(os.path.expanduser(local["disk_path"])).free / 1e9
        except OSError:
            free_gb = None
        hosts.setdefault("monitor", {"role": "monitor", "label": local.get("label", "monitor")})
        hosts["monitor"]["disk_free_gb"] = None if free_gb is None else round(free_gb, 1)
        if free_gb is not None and free_gb < float(local.get("action_gb", 40)):
            findings.append(Finding("disk", "monitor", "action",
                                    f"{local.get('label', 'monitor')} has {free_gb:.0f} GB free "
                                    f"(backups need room)", "Free space on the monitor machine."))
        elif free_gb is not None and free_gb < float(local.get("warn_gb", 60)):
            findings.append(Finding("disk", "monitor", "warn",
                                    f"{local.get('label', 'monitor')} has {free_gb:.0f} GB free", ""))
    backups = cfg.get("backups") or {}
    for set_name in backups.get("sets", []):
        last = read_json(Path(os.path.expanduser(backups["dir"])) / set_name / "last-success.json")
        hosts.setdefault("backups", {"role": "backups", "label": "backups"})
        hosts["backups"][set_name] = last
        age_h = (now - float(last["finished"])) / 3600 if isinstance(last, dict) and last.get("finished") else None
        if age_h is None or age_h > float(backups.get("max_age_h", 36)):
            findings.append(Finding(f"backup:{set_name}", "backups", "action",
                                    f"Backup {set_name} is " + ("missing" if age_h is None
                                                                else f"{age_h:.0f} h old"),
                                    "Read the backup log on the monitor machine."))
        elif isinstance(last, dict) and last.get("shrunk"):
            findings.append(Finding(f"backup:{set_name}", "backups", "action",
                                    f"Backup {set_name} is under 90% of the previous one",
                                    "Check the source before the next run prunes anything."))
    # Status files written by other backup jobs: {"name", "path", "time_key"}.
    for extra in backups.get("files", []):
        doc = read_json(Path(os.path.expanduser(extra["path"])))
        hosts.setdefault("backups", {"role": "backups", "label": "backups"})
        stamp = doc.get(extra.get("time_key", "finished")) if isinstance(doc, dict) else None
        hosts["backups"][extra["name"]] = {"finished": stamp} if stamp else None
        age_h = (now - float(stamp)) / 3600 if stamp else None
        if age_h is None or age_h > float(backups.get("max_age_h", 36)):
            findings.append(Finding(f"backup:{extra['name']}", "backups", "action",
                                    f"Backup {extra['name']} is " + ("missing" if age_h is None
                                                                     else f"{age_h:.0f} h old"),
                                    "Read that backup job's log on the monitor machine."))
    return {"hosts": hosts, "findings": findings, "host_markers": host_markers}


def summary_line(name: str, host: dict, findings: list) -> str:
    mine = [f for f in findings if f["host"] == name]
    level = "ACTION" if any(f["level"] == "action" for f in mine) else \
            "WARN" if mine else "OK"
    label = host.get("label", name)
    p = host.get("probe") or {}
    bits = []
    if host.get("role") == "studio" and p:
        sol = p.get("sol") or {}
        q = p.get("queue") or {}
        bits.append("H3 warm (" + str(sol.get("task")) + ")" if sol.get("loaded") else "H3 cold")
        bits.append(f"queue {q.get('queued', '?')} waiting / {q.get('running', '?')} running")
        if (p.get("hold") or {}).get("exists"):
            bits.append("HELD")
    elif host.get("role") == "text" and p:
        bits.append(f"text {(p.get('text_state') or {}).get('state', '?')}")
        gw = p.get("gateway") or {}
        bits.append(f"Sparky {gw.get('state', '?')}/{gw.get('telegram', '?')}")
    elif host.get("role") == "monitor":
        bits.append(f"{host.get('disk_free_gb')} GB free")
    elif host.get("role") == "backups":
        for k, v in host.items():
            if isinstance(v, dict) and v.get("finished"):
                when = _dt.datetime.fromtimestamp(float(v["finished"])).strftime("%m-%d %H:%M")
                bits.append(f"{k} {when} ({float(v.get('bytes', 0)) / 1e9:.1f} GB)")
    text = "; ".join(f["text"] for f in mine)
    return f"{label}: {level}. " + ", ".join(bits) + (f". {text}" if text else "")


def run(cfg: dict, *, now: float | None = None, probe=run_probe, fetch=http,
        alerts: bool = True) -> dict:
    now = time.time() if now is None else now
    state_dir = Path(os.path.expanduser(cfg["state_dir"]))
    state_dir.mkdir(parents=True, exist_ok=True)
    st = read_json(state_dir / "state.json", {}) or {}
    got = collect(cfg, st, now, probe=probe, fetch=fetch)
    muted, marker_findings = maintenance(cfg, now, got["host_markers"])
    findings = apply_persistence(got["findings"], st, now,
                                 {**DEFAULT_PERSIST, **cfg.get("persist", {})}) + marker_findings
    quiet = in_quiet_hours(now, cfg.get("quiet_hours", [22, 7]), cfg.get("tz_offset_h"))
    dry = bool(cfg.get("dry_run")) or os.environ.get("SPARK_HEALTH_DRY_RUN") == "1"
    message = ""
    if alerts:
        message = render(plan_messages(findings, st, now, quiet=quiet, muted_hosts=muted), now)
        st["last_run"] = now
    status = {"ts": now, "muted": sorted(muted), "quiet": quiet,
              "lines": [summary_line(n, h, findings) for n, h in got["hosts"].items()],
              "findings": findings, "hosts": {n: {k: v for k, v in h.items() if k != "probe"}
                                              | {"probe": h.get("probe")} for n, h in got["hosts"].items()}}
    if alerts:
        write_json(state_dir / "state.json", st)
        write_json(state_dir / "status.json", status)
        daily = read_json(state_dir / "daily.json", {}) or {}
        today = _dt.datetime.fromtimestamp(now).strftime("%Y-%m-%d")
        if daily.get("date") != today:
            daily = {"date": today, "warn": {}, "actions_sent": []}
        for f in findings:
            if f["level"] == "warn":
                daily["warn"][f"{f['host']}:{f['key']}"] = f["text"]
        if message:
            daily["actions_sent"].append({"ts": now, "message": message, "dry_run": dry})
        daily["monitor_last_ran"] = _dt.datetime.fromtimestamp(now).strftime("%H:%M")
        daily["lines"] = status["lines"]
        write_json(state_dir / "daily.json", daily)
        with (state_dir / "history.jsonl").open("a") as fh:
            fh.write(json.dumps({"ts": now, "actions": sorted(f"{f['host']}:{f['key']}" for f in findings
                                                              if f["level"] == "action"),
                                 "warns": sorted(f"{f['host']}:{f['key']}" for f in findings
                                                 if f["level"] == "warn"),
                                 "muted": sorted(muted), "quiet": quiet, "sent": bool(message)}) + "\n")
        if message and dry:
            with (state_dir / "dry-run.log").open("a") as fh:
                fh.write(message + "\n\n")
            message = ""
    return {"message": message, "status": status}


def queue_message(state_dir: Path, message: str, now: float) -> None:
    """--outbox mode (a launchd run): keep the message for the delivering job."""
    with (Path(state_dir) / "outbox.jsonl").open("a") as fh:
        fh.write(json.dumps({"ts": now, "message": message}) + "\n")


def deliver(cfg: dict, *, now: float | None = None) -> str:
    """--deliver mode (the Hermes job): hand over queued messages exactly once.

    Also watches the watcher: if the scheduled check has not run for STALE_S it
    says so (once per REMINDER_S), because silence must never look like "fine".
    Dry run: nothing is returned; the staleness note goes to dry-run.log."""
    now = time.time() if now is None else now
    state_dir = Path(os.path.expanduser(cfg["state_dir"]))
    state_dir.mkdir(parents=True, exist_ok=True)
    dry = bool(cfg.get("dry_run")) or os.environ.get("SPARK_HEALTH_DRY_RUN") == "1"
    outbox, sending = state_dir / "outbox.jsonl", state_dir / ".outbox.sending"
    if outbox.exists():
        # Take the outbox atomically (the check may be appending right now),
        # then add it to anything a crashed delivery left behind.
        taking = state_dir / ".outbox.taking"
        os.replace(outbox, taking)
        with sending.open("a") as fh:
            fh.write(taking.read_text())
        taking.unlink()
    messages = []
    for line in (sending.read_text() if sending.exists() else "").splitlines():
        try:
            messages.append(json.loads(line)["message"])
        except (ValueError, KeyError, TypeError):
            continue
    st_path = state_dir / "deliver-state.json"
    dst = read_json(st_path, {}) or {}
    status = read_json(state_dir / "status.json") or {}
    age = now - float(status.get("ts") or 0)
    if age > STALE_S:
        if now - float(dst.get("stale_sent") or 0) >= REMINDER_S:
            messages.append(f"Spark check: the health watch has not run for {age / 60:.0f} min. "
                            "Next: on VibeX, `launchctl print gui/$(id -u)/com.vibex.spark-health-watch` "
                            "and ~/Library/Logs/spark-health-watch.log.")
            dst["stale_sent"] = now
    elif dst.get("stale_sent"):
        messages.append("Spark check: the health watch is running again.")
        dst["stale_sent"] = None
    write_json(st_path, dst)
    text = "\n\n".join(messages)
    if dry and text:
        with (state_dir / "dry-run.log").open("a") as fh:
            fh.write("[deliver] " + text + "\n\n")
        text = ""
    if sending.exists():
        sending.unlink()
    return text


def load_config(path: Path | None = None) -> dict:
    path = Path(path or os.environ.get("SPARK_HEALTH_CONFIG") or DEFAULT_CONFIG)
    return json.loads(path.read_text())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Spark health watch")
    ap.add_argument("--config", type=Path)
    ap.add_argument("--status", action="store_true", help="print one line per host")
    ap.add_argument("--refresh", action="store_true", help="with --status: check live now")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--outbox", action="store_true",
                    help="scheduled check (launchd): queue messages for --deliver, print nothing")
    ap.add_argument("--deliver", action="store_true",
                    help="delivering job (Hermes no_agent): print queued messages once")
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    if args.deliver:
        text = deliver(cfg)
        if text:
            print(text)
        return 0
    if args.status:
        if args.refresh:
            status = run(cfg, alerts=False)["status"]
        else:
            status = read_json(Path(os.path.expanduser(cfg["state_dir"])) / "status.json")
            if not status:
                print("no run yet")
                return 1
        if args.json:
            print(json.dumps(status, indent=1, sort_keys=True))
        else:
            age = time.time() - float(status.get("ts", 0))
            for line in status.get("lines", []):
                print(line)
            print(f"(checked {age / 60:.0f} min ago"
                  + (", quiet hours" if status.get("quiet") else "")
                  + (f", muted: {', '.join(status['muted'])}" if status.get("muted") else "") + ")")
        return 0
    out = run(cfg)
    if out["message"]:
        if args.outbox:
            queue_message(Path(os.path.expanduser(cfg["state_dir"])), out["message"], time.time())
        else:
            print(out["message"])
    return 0


if __name__ == "__main__":
    sys.exit(main())
