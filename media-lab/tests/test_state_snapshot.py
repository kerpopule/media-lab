"""The hourly on-box snapshot covers storyboards and prunes no-op receipts."""
import json
import os
import subprocess
import time
from pathlib import Path

from runner import prune_residency_receipts as prr

ROOT = Path(__file__).parents[1]


def test_snapshot_covers_storyboards_and_a_daily_jobs_copy(tmp_path):
    lab = tmp_path / "media-lab-simple"
    lab.mkdir()
    for name in ("characters.json", "storyboards.json", "storyboards-archive.json", "jobs.json"):
        (lab / name).write_text(json.dumps({"name": name}))
    env = {"HOME": str(tmp_path), "PATH": os.environ["PATH"]}
    subprocess.run(["bash", str(ROOT / "runner/state_snapshot.sh")], env=env, check=True)
    subprocess.run(["bash", str(ROOT / "runner/state_snapshot.sh")], env=env, check=True)
    names = sorted(p.name.split(".json")[0] for p in (lab / "backups").iterdir())
    assert names == ["characters", "jobs", "storyboards", "storyboards-archive"]
    assert len(list((lab / "backups").glob("jobs.json.*.gz"))) == 1
    assert " gallery.json boards.json" not in (ROOT / "runner/state_snapshot.sh").read_text()


def _receipt(d, name, actions, status="committed", age_days=20):
    path = d / f"{name}.json"
    path.write_text(json.dumps({"status": status, "rollback": [],
                                "plan": {"actions": [{"action": a} for a in actions]}}))
    t = time.time() - age_days * 86400
    os.utime(path, (t, t))
    return path


def test_prune_removes_only_old_noop_receipts_and_keeps_the_newest(tmp_path):
    old_noop = _receipt(tmp_path, "1-a", ["retain", "commit"])
    old_real = _receipt(tmp_path, "2-b", ["retain", "evict"])
    old_failed = _receipt(tmp_path, "3-c", ["retain"], status="failed")
    fresh_noop = _receipt(tmp_path, "4-d", ["retain"], age_days=1)
    newest_old = _receipt(tmp_path, "5-e", ["commit"], age_days=15)
    os.utime(newest_old, None)  # newest by mtime
    assert prr.prune(tmp_path, days=14) == 1
    assert not old_noop.exists()
    for kept in (old_real, old_failed, fresh_noop, newest_old):
        assert kept.exists()
