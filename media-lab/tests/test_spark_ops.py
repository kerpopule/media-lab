"""The off-box health watch and the nightly pull backup (tools/ops)."""
import datetime as dt
import importlib.util
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

OPS = Path(__file__).parents[1] / "tools" / "ops"


def _load(name):
    spec = importlib.util.spec_from_file_location(name, OPS / f"{name}.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


watch = _load("spark_health_watch")
backup = _load("spark_backup")
probe = _load("probe_hosts")

# 2026-09-26 10:00 local (a daytime hour) and 23:30 (quiet hours)
DAY = dt.datetime(2026, 9, 26, 10, 0).timestamp()
NIGHT = dt.datetime(2026, 9, 26, 23, 30).timestamp()


def F(key, host="s1", level="action", text=None):
    return watch.Finding(key, host, level, text or f"{key} broke", "do the thing")


# ---------------------------------------------------------------- alerting

def test_enter_remind_resolve():
    st = {}
    lines = watch.plan_messages([F("hold")], st, DAY, quiet=False, muted_hosts=set())
    assert lines == ["hold broke. Next: do the thing"]
    assert watch.plan_messages([F("hold")], st, DAY + 300, quiet=False, muted_hosts=set()) == []
    again = watch.plan_messages([F("hold")], st, DAY + watch.REMINDER_S + 1, quiet=False,
                                muted_hosts=set())
    assert again and again[0].startswith("Still: ")
    done = watch.plan_messages([], st, DAY + watch.REMINDER_S + 600, quiet=False, muted_hosts=set())
    assert done == ["Resolved: hold broke."]
    assert watch.plan_messages([], st, DAY + 99999, quiet=False, muted_hosts=set()) == []


def test_warn_never_messages_and_unsent_alerts_resolve_silently():
    st = {}
    assert watch.plan_messages([F("h3_cold", level="warn")], st, DAY, quiet=False, muted_hosts=set()) == []
    assert watch.plan_messages([F("hold")], st, NIGHT, quiet=True, muted_hosts=set()) == []
    assert watch.plan_messages([], st, NIGHT + 600, quiet=True, muted_hosts=set()) == []
    assert st["alerts"] == {}


def test_quiet_hours_hold_then_merge_at_the_end():
    st = {}
    assert watch.plan_messages([F("hold"), F("latch")], st, NIGHT, quiet=True, muted_hosts=set()) == []
    morning = dt.datetime(2026, 9, 27, 7, 0).timestamp()
    lines = watch.plan_messages([F("hold"), F("latch")], st, morning, quiet=False, muted_hosts=set())
    assert len(lines) == 2
    assert watch.render(lines, morning).startswith("Spark check 07:00\n- ")


def test_quiet_hours_window():
    assert watch.in_quiet_hours(NIGHT, [22, 7])
    assert watch.in_quiet_hours(dt.datetime(2026, 9, 27, 6, 59).timestamp(), [22, 7])
    assert not watch.in_quiet_hours(DAY, [22, 7])
    assert not watch.in_quiet_hours(NIGHT, [])


def test_unreachable_hides_its_own_subchecks_and_mute_holds_everything():
    st = {}
    lines = watch.plan_messages([F("unreachable"), F("hold"), F("text_state", host="s2")], st, DAY,
                                quiet=False, muted_hosts=set())
    assert len(lines) == 2 and any("unreachable" in l for l in lines)
    st = {}
    assert watch.plan_messages([F("hold")], st, DAY, quiet=False, muted_hosts={"s1"}) == []


def test_persistence_needs_duration():
    st = {}
    rules = {"unit": 600}
    assert watch.apply_persistence([F("unit:x")], st, DAY, rules) == []
    assert watch.apply_persistence([F("unit:x")], st, DAY + 601, rules)
    assert watch.apply_persistence([], st, DAY + 700, rules) == [] and st["first_seen"] == {}


def test_studio_findings_levels():
    p = {"hold": {"exists": True, "reason": "durable-lease-recovery:owner-exited", "age_s": 300},
         "queue": {"answered": True, "queued": 0, "running": 0}, "units": {"a.service": "active"},
         "text": {"listed": True}, "sol": {"configured": True, "boot_cleared": True, "loaded": True},
         "disk_free_pct": 50}
    assert [f["level"] for f in watch.studio_findings("s1", p, {})] == ["warn"]
    p["hold"]["age_s"] = 1300
    assert [f["level"] for f in watch.studio_findings("s1", p, {})] == ["action"]
    p["hold"] = {"exists": False}
    p["sol"]["boot_cleared"] = False
    keys = [f["key"] for f in watch.studio_findings("s1", p, {})]
    assert keys == ["h3_clearance"]
    p["sol"]["boot_cleared"] = True
    p["units"]["a.service"] = "failed"
    p["text"]["listed"] = False
    p["queue"].update(queued=2, running=0, oldest_queued_age_s=1000)
    keys = {f["key"] for f in watch.studio_findings("s1", p, {})}
    assert {"unit:a.service", "text_bridge", "queue_stuck"} <= keys


def test_cold_load_guard_trip_waits_for_the_self_heal_then_alerts():
    base = {"queue": {"answered": True, "queued": 0, "running": 0}, "units": {},
            "text": {"listed": True}, "sol": {"configured": True, "boot_cleared": True, "loaded": False},
            "disk_free_pct": 50, "safety_stop": True, "safety_stop_reason": "memory-psi", "latch": True}
    trip = {**base, "hold": {"exists": True, "reason": "operation-uncertain:RuntimeError", "age_s": 900}}
    found = watch.studio_findings("s1", trip, {})
    assert [(f["key"], f["level"]) for f in found] == [("hold", "warn")]   # one finding, not three
    trip["hold"]["age_s"] = 41 * 60
    found = watch.studio_findings("s1", trip, {})
    assert found[0]["level"] == "action" and "did not clear itself" in found[0]["text"]
    gave = {**trip, "autorecover_gaveup": True, "hold": {**trip["hold"], "age_s": 900}}
    assert watch.studio_findings("s1", gave, {})[0]["level"] == "action"
    render_fail = {**base, "safety_stop_reason": "generation_failed",
                   "hold": {"exists": True, "reason": "operation-uncertain:RuntimeError", "age_s": 60}}
    assert watch.studio_findings("s1", render_fail, {})[0]["level"] == "action"
    no_hold = {**base, "hold": {"exists": False}}
    keys = {f["key"] for f in watch.studio_findings("s1", no_hold, {})}
    assert {"safety_stop", "latch"} <= keys


def test_restart_cap_from_the_studio_health_is_an_action():
    p = {"hold": {"exists": False}, "queue": {"answered": True, "queued": 0, "running": 0},
         "units": {}, "text": {"listed": True}, "disk_free_pct": 50,
         "sol": {"configured": True, "boot_cleared": True, "loaded": True},
         "health": {"level": "action", "reasons": ["queue watchdog hit its restart cap"]}}
    assert [f["key"] for f in watch.studio_findings("s1", p, {})] == ["restart_capped"]


def test_h3_cold_for_an_hour_becomes_action():
    p = {"hold": {"exists": False}, "queue": {"answered": True, "queued": 0, "running": 0},
         "units": {}, "text": {"listed": True}, "disk_free_pct": 50,
         "sol": {"configured": True, "boot_cleared": True, "loaded": False}}
    found = watch.studio_findings("s1", p, {})
    assert {(f["key"], f["level"]) for f in found} == {("h3_cold", "warn"), ("h3_cold_long", "action")}
    st = {}
    assert [f["key"] for f in watch.apply_persistence(found, st, DAY, watch.DEFAULT_PERSIST)] == []
    kept = watch.apply_persistence(found, st, DAY + 3601, watch.DEFAULT_PERSIST)
    assert {f["key"] for f in kept} == {"h3_cold", "h3_cold_long"}


def test_outbox_then_deliver_exactly_once_and_watch_the_watcher(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("SPARK_HEALTH_DRY_RUN", raising=False)
    cfg = _cfg(tmp_path, dry_run=False)
    state = tmp_path / "state"
    state.mkdir()
    (state / "status.json").write_text(json.dumps({"ts": DAY}))
    watch.queue_message(state, "Spark check 10:00\n- hold broke.", DAY)
    watch.queue_message(state, "Spark check 10:05\n- Resolved: hold broke.", DAY + 300)
    text = watch.deliver(cfg, now=DAY + 310)
    assert "hold broke" in text and "Resolved" in text
    assert watch.deliver(cfg, now=DAY + 320) == ""          # exactly once
    stale = watch.deliver(cfg, now=DAY + 25 * 60)
    assert "has not run for 25 min" in stale
    assert watch.deliver(cfg, now=DAY + 30 * 60) == ""      # deduped
    (state / "status.json").write_text(json.dumps({"ts": DAY + 31 * 60}))
    assert "running again" in watch.deliver(cfg, now=DAY + 32 * 60)


def test_outbox_mode_prints_nothing_and_dry_run_deliver_sends_nothing(tmp_path, monkeypatch, capsys):
    monkeypatch.delenv("SPARK_HEALTH_DRY_RUN", raising=False)
    cfg = _cfg(tmp_path, dry_run=False, quiet_hours=[])   # real clock: never quiet
    path = tmp_path / "cfg.json"
    path.write_text(json.dumps(cfg))
    monkeypatch.setattr(watch.run, "__kwdefaults__",
                        {**watch.run.__kwdefaults__, "probe": lambda t, k, a=None, o=None: None,
                         "fetch": lambda *a, **k: (200, {})})
    assert watch.main(["--config", str(path), "--outbox"]) == 0
    assert capsys.readouterr().out == ""
    assert (tmp_path / "state" / "outbox.jsonl").exists()
    cfg["dry_run"] = True
    path.write_text(json.dumps(cfg))
    assert watch.main(["--config", str(path), "--deliver"]) == 0
    assert capsys.readouterr().out == ""
    assert "[deliver]" in (tmp_path / "state" / "dry-run.log").read_text()


def _cfg(tmp_path, **over):
    cfg = {"state_dir": str(tmp_path / "state"), "quiet_hours": [22, 7], "dry_run": True,
           "hosts": {"s1": {"ssh": "user@host1.example", "role": "studio", "label": "Studio host"},
                     "s2": {"ssh": "user@host2.example", "role": "text", "label": "Text host"}},
           "persist": {"unreachable": 0}}
    cfg.update(over)
    return cfg


def fake_probe_ok(target, kind, args=None, opts=None):
    if kind == "studio":
        return {"boot_id": "b1", "hold": {"exists": False}, "queue": {"answered": True, "queued": 0,
                "running": 0}, "units": {}, "text": {"listed": True},
                "sol": {"configured": True, "boot_cleared": True, "loaded": True, "task": "t2va"},
                "disk_free_pct": 60}
    return {"boot_id": "b2", "text_state": {"state": "serving"},
            "gateway": {"state": "running", "telegram": "connected"}, "disk_free_pct": 60}


def test_dry_run_writes_the_log_and_prints_nothing(tmp_path, capsys):
    cfg = _cfg(tmp_path)
    calls = []

    def down(target, kind, args=None, opts=None):
        calls.append(target)
        return None if kind == "studio" else fake_probe_ok(target, kind)
    out = watch.run(cfg, now=DAY, probe=down, fetch=lambda *a, **k: (200, {}))
    assert out["message"] == ""
    log = (tmp_path / "state" / "dry-run.log").read_text()
    assert "Studio host does not answer over ssh" in log
    status = json.loads((tmp_path / "state" / "status.json").read_text())
    assert status["lines"][0].startswith("Studio host: ACTION.")
    assert status["lines"][1].startswith("Text host: OK.")
    daily = json.loads((tmp_path / "state" / "daily.json").read_text())
    assert daily["actions_sent"][0]["dry_run"] is True
    assert (tmp_path / "state" / "history.jsonl").read_text().count("\n") == 1


def test_live_mode_prints_only_on_change(tmp_path, monkeypatch):
    monkeypatch.delenv("SPARK_HEALTH_DRY_RUN", raising=False)
    cfg = _cfg(tmp_path, dry_run=False)
    ok = watch.run(cfg, now=DAY, probe=fake_probe_ok, fetch=lambda *a, **k: (200, {}))
    assert ok["message"] == ""
    down = lambda t, k, a=None, o=None: None if k == "text" else fake_probe_ok(t, k)
    first = watch.run(cfg, now=DAY + 300, probe=down, fetch=lambda *a, **k: (200, {}))
    assert "Text host does not answer" in first["message"]
    second = watch.run(cfg, now=DAY + 600, probe=down, fetch=lambda *a, **k: (200, {}))
    assert second["message"] == ""
    back = watch.run(cfg, now=DAY + 900, probe=fake_probe_ok, fetch=lambda *a, **k: (200, {}))
    assert "Resolved" in back["message"]


def test_maintenance_marker_mutes_then_expires(tmp_path, monkeypatch):
    monkeypatch.delenv("SPARK_HEALTH_DRY_RUN", raising=False)
    marker = tmp_path / "baton"
    marker.mkdir()
    cfg = _cfg(tmp_path, dry_run=False,
               maintenance_markers=[{"path": str(marker), "mutes": ["s1"]}])
    os.utime(marker, (DAY, DAY))
    now = DAY + 60
    down = lambda t, k, a=None, o=None: None if k == "studio" else fake_probe_ok(t, k)
    assert watch.run(cfg, now=now, probe=down, fetch=lambda *a, **k: (200, {}))["message"] == ""
    later = now + watch.MARKER_MAX_S + 60
    msg = watch.run(cfg, now=later, probe=fake_probe_ok, fetch=lambda *a, **k: (200, {}))["message"]
    assert "Maintenance marker" in msg


def test_remote_text_checks_and_completion_every_15_minutes(tmp_path, monkeypatch):
    monkeypatch.delenv("SPARK_HEALTH_DRY_RUN", raising=False)
    cfg = _cfg(tmp_path, dry_run=False)
    cfg["hosts"]["s2"]["text_url"] = "http://100.64.0.2:8004"
    posts = []

    def fetch(url, timeout=10, data=None, headers=None):
        if data is not None:
            posts.append(url)
            return 500, None
        return 200, {"data": [{"id": "media-lab-text"}]}
    watch.run(cfg, now=DAY, probe=fake_probe_ok, fetch=fetch)
    watch.run(cfg, now=DAY + 300, probe=fake_probe_ok, fetch=fetch)
    assert len(posts) == 1
    msg = watch.run(cfg, now=DAY + 901, probe=fake_probe_ok, fetch=fetch)["message"]
    assert len(posts) == 2 and "4-token test twice" in msg


def test_backup_freshness_is_checked(tmp_path, monkeypatch):
    monkeypatch.delenv("SPARK_HEALTH_DRY_RUN", raising=False)
    root = tmp_path / "backups"
    (root / "lib").mkdir(parents=True)
    (root / "lib" / "last-success.json").write_text(json.dumps({"finished": DAY - 40 * 3600, "bytes": 1}))
    cfg = _cfg(tmp_path, dry_run=False, backups={"dir": str(root), "sets": ["lib", "missing"],
                                                 "max_age_h": 36})
    (root / "other.json").write_text(json.dumps({"epoch": DAY - 3600}))
    cfg["backups"]["files"] = [{"name": "other", "path": str(root / "other.json"), "time_key": "epoch"},
                               {"name": "gone", "path": str(root / "gone.json"), "time_key": "epoch"}]
    msg = watch.run(cfg, now=DAY, probe=fake_probe_ok, fetch=lambda *a, **k: (200, {}))["message"]
    assert "Backup lib is 40 h old" in msg and "Backup missing is missing" in msg
    assert "Backup gone is missing" in msg and "Backup other" not in msg


# ---------------------------------------------------------------- backup

def test_retention_keeps_daily_weekly_monthly():
    start = dt.date(2026, 1, 1)
    names = [(start + dt.timedelta(days=i)).isoformat() for i in range(200)]
    kept = backup.keep_set(names, {"daily": 14, "weekly": 8, "monthly": 6})
    dates = sorted(dt.date.fromisoformat(n) for n in kept)
    assert len([d for d in dates if d >= dt.date.fromisoformat(names[-14])]) >= 14
    assert len([d for d in dates if d.day == 1]) == 6
    assert len([d for d in dates if d.weekday() == 6]) >= 8
    assert names[-1] in kept and len(kept) <= 14 + 8 + 6


def test_prune_only_touches_dated_snapshots(tmp_path):
    for n in ("2026-09-01", "2026-09-02", "2026-09-03", "notes", "last-success.json"):
        p = tmp_path / n
        p.mkdir() if "." not in n else p.write_text("{}")
    removed = backup.prune(tmp_path, {"daily": 2})
    assert removed == ["2026-09-01"]
    assert (tmp_path / "notes").exists() and (tmp_path / "last-success.json").exists()


def test_rsync_command_is_a_safe_pull(tmp_path):
    seen = []
    link = tmp_path / "prev"
    (link / "lab" / "media").mkdir(parents=True)
    backup.rsync_pull({"ssh": "user@host.example"}, "lab/media/", tmp_path / "new", link,
                      runner=lambda cmd, **k: seen.append(cmd) or type("R", (), {"returncode": 0})())
    cmd = seen[0]
    assert cmd[:4] == ["rsync", "-a", "--numeric-ids", "--delete"]
    assert f"--link-dest={link / 'lab/media'}" in cmd
    assert "--exclude=admin-pin.txt" in cmd and "--exclude=local.env" in cmd
    assert cmd[-2] == "user@host.example:lab/media/" and cmd[-1].endswith("new/lab/media/")
    assert any(c.startswith("--rsync-path=systemd-run --user --scope") and "ionice -c3 rsync" in c
               for c in cmd)
    with pytest.raises(ValueError):
        backup.rsync_pull({"ssh": "x"}, "lab/file.json", tmp_path, None)


def test_space_guard(tmp_path, monkeypatch):
    usage = type("U", (), {"free": 70e9, "total": 1e12, "used": 0})
    monkeypatch.setattr(backup.shutil, "disk_usage", lambda p: usage)
    assert backup.space_ok(tmp_path, int(8e9), {"min_free_gb": 60, "min_free_after_gb": 50})[0]
    assert not backup.space_ok(tmp_path, int(30e9), {"min_free_gb": 60, "min_free_after_gb": 50})[0]
    usage.free = 50e9
    assert not backup.space_ok(tmp_path, 0, {"min_free_gb": 60})[0]


def test_back_up_end_to_end_with_a_local_fake_host(tmp_path, monkeypatch):
    host = tmp_path / "host"
    (host / "lab" / "media").mkdir(parents=True)
    for i in range(3):
        (host / "lab" / "media" / f"{i}.mp4").write_bytes(os.urandom(1000))
    (host / "lab" / "storyboards.json").write_text('{"boards": []}')

    def fake_remote(spec, script, payload, timeout=900):
        if "PAYLOAD" in script and "shutil.copy2" in script:
            stage = host / payload["stage"] / "files"
            for rel in payload["files"]:
                (stage / rel).parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(host / rel, stage / rel)
            return {"copied": payload["files"], "sqlite": [], "commands": [], "missing": []}
        return {rel: backup.sha256(host / rel.replace("_staged/", "", 1)) if (host / rel).exists() else
                backup.sha256(host / rel) if (host / rel).exists() else None for rel in payload}

    def fake_pull(spec, rel, dest, link, runner=None):
        src = host / rel.rstrip("/")
        dst = dest / rel.rstrip("/")
        shutil.copytree(src, dst, dirs_exist_ok=True)

    monkeypatch.setattr(backup, "remote_python", fake_remote)
    monkeypatch.setattr(backup, "rsync_pull", fake_pull)
    cfg = {"root": str(tmp_path / "Backups"), "min_free_gb": 0, "min_free_after_gb": 0}
    spec = {"ssh": "x", "paths": ["lab/media/"], "stage_files": ["lab/storyboards.json"],
            "keep": {"daily": 14}}
    done = backup.back_up("lib", spec, cfg, today="2026-09-26")
    snap = tmp_path / "Backups" / "lib" / "2026-09-26"
    assert snap.is_dir() and not (tmp_path / "Backups" / "lib" / "2026-09-26.partial").exists()
    assert (snap / "lab" / "media" / "2.mp4").exists()
    assert done["files"] >= 4 and done["media_checked"] == 3 and done["shrunk"] is False
    last = json.loads((tmp_path / "Backups" / "lib" / "last-success.json").read_text())
    assert last["snapshot"].endswith("2026-09-26")
    v = backup.verify("lib", cfg, restore_to=tmp_path / "scratch")
    assert v["json_bad"] == [] and v["restored"] and all(v["restored"].values())
    with pytest.raises(RuntimeError, match="not empty"):
        backup.verify("lib", cfg, restore_to=tmp_path / "scratch")


# ---------------------------------------------------------------- probe

def test_studio_probe_reads_files_and_never_prints_the_token(tmp_path, monkeypatch, capsys):
    root = tmp_path / "lab"
    (root / "pool").mkdir(parents=True)
    (root / "config").mkdir()
    (root / "config" / "local.env").write_text("MEDIA_LAB_BIND_HOST=127.0.0.1\nSOL_PKG=/x\n")
    (root / "local-token.txt").write_text("t" * 64)
    (root / "pool" / "gpu-recovery-hold.json").write_text(json.dumps(
        {"reason": "durable-lease-recovery:owner-exited", "job_id": None, "created": probe.NOW - 60}))
    monkeypatch.setattr(probe, "get", lambda url, headers=None, timeout=5.0: (0, None))
    doc = probe.studio(str(root))
    assert doc["hold"]["exists"] and doc["hold"]["reason"].endswith("owner-exited")
    assert doc["queue"] == {"answered": False}
    assert doc["sol"]["configured"] is True and doc["sol"]["loaded"] is False
    probe.main(["-", "studio", str(root)])
    assert "t" * 64 not in capsys.readouterr().out


def test_precheck_waits_then_gives_up(monkeypatch):
    results = iter([1, 1, 0])
    calls = []
    runner = lambda cmd, **k: calls.append(cmd) or type("R", (), {"returncode": next(results)})()
    backup.wait_for_precheck({"ssh": "x", "precheck": "true", "precheck_wait_s": 9999},
                             runner=runner, sleep=lambda s: None)
    assert len(calls) == 3 and calls[0][-1] == "true"
    with pytest.raises(RuntimeError, match="precheck never passed"):
        backup.wait_for_precheck({"ssh": "x", "precheck": "false", "precheck_wait_s": 0},
                                 runner=lambda c, **k: type("R", (), {"returncode": 1})(),
                                 sleep=lambda s: None)


def test_after_a_reboot_the_self_clear_gets_its_time_then_alerts_with_the_reason():
    # 2026-09-27 (owner decision): after any reboot the studio clears H3 and the carried-over hold by itself.
    base = {"queue": {"answered": True, "queued": 0, "running": 0}, "units": {},
            "text": {"listed": True}, "sol": {"configured": True, "boot_cleared": False, "loaded": False},
            "disk_free_pct": 50, "uptime_s": 300,
            "hold": {"exists": True, "reason": "durable-lease-recovery:boot-changed", "age_s": 300}}
    found = watch.studio_findings("s1", base, {})
    assert {f["level"] for f in found} == {"warn"}                  # nothing sent yet
    late = {**base, "uptime_s": 51 * 60, "autorecover_last": "MemAvailable 60.0 GiB is below 90 GiB"}
    found = [f for f in watch.studio_findings("s1", late, {}) if f["level"] == "action"]
    assert [f["key"] for f in found] == ["hold"]                    # one message, not two
    assert "did not clear it by itself" in found[0]["text"] and "MemAvailable" in found[0]["text"]
    gave = {**base, "autorecover_gaveup": True,
            "autorecover_gaveup_why": "the GPU does not answer nvidia-smi"}
    found = [f for f in watch.studio_findings("s1", gave, {}) if f["level"] == "action"]
    assert [f["key"] for f in found] == ["hold"] and "nvidia-smi" in found[0]["text"]
    no_hold = {**base, "hold": {"exists": False}}
    assert [(f["key"], f["level"]) for f in watch.studio_findings("s1", no_hold, {})
            if f["key"] == "h3_clearance"] == [("h3_clearance", "warn")]
    no_hold_gave = {**no_hold, "autorecover_gaveup": True, "autorecover_gaveup_why": "latch set"}
    found = [f for f in watch.studio_findings("s1", no_hold_gave, {}) if f["key"] == "h3_clearance"]
    assert found[0]["level"] == "action" and "latch set" in found[0]["text"]
