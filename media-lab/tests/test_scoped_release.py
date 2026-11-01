"""Exercise real selective file install/refusal/rollback without SSH or systemd."""

import base64
import importlib.util
import json
from pathlib import Path
import subprocess

import pytest

SPEC = importlib.util.spec_from_file_location(
    "scoped_release", Path(__file__).parents[1] / "tools/scoped_release.py"
)
assert SPEC is not None and SPEC.loader is not None
sr = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(sr)


@pytest.fixture
def host(tmp_path):
    root = tmp_path / "media-lab-simple"
    root.mkdir()
    (root / "app.py").write_text("old = 1\n")
    (root / "deployed-source.json").write_text(
        json.dumps({"commit": "old-base", "private_overlays": [{"id": "private"}]})
    )
    (root / "foreign.py").write_text("untouched = True\n")
    return root


def bundle(root, name="app.py", data=b"new = 2\n"):
    p = root / name
    return {
        "schema": 1,
        "tag": "test-v1",
        "commit": "a" * 40,
        "repository": "fixture",
        "files": [
            {
                "path": name,
                "before": sr.digest(p.read_bytes()) if p.exists() else None,
                "after": sr.digest(data),
                "tag_sha256": sr.digest(data),
                "composed": False,
                "data": base64.b64encode(data).decode(),
            }
        ],
    }


def idle():
    return {"active_total": 0, "active": []}


def test_install_preserves_foreign_code_and_base_receipt(host):
    before = (host / "foreign.py").read_bytes()
    restarts = []
    result = sr.apply_bundle(bundle(host), host, queue=idle, restart=restarts.append)
    assert result["status"] == "installed"
    assert (host / "app.py").read_bytes() == b"new = 2\n"
    assert (host / "foreign.py").read_bytes() == before
    receipt = json.loads((host / "deployed-source.json").read_text())
    assert receipt["commit"] == "old-base"
    assert receipt["private_overlays"] == [{"id": "private"}]
    assert receipt["scoped_releases"][-1]["files"][0]["after"] == sr.digest(b"new = 2\n")
    assert restarts == ["media-lab-simple.service"]
    assert (Path(result["backup"]) / "preimage/app.py").read_bytes() == b"old = 1\n"


def test_pair_restarts_only_web(host):
    p = host.parent / ".local/state/spark-pair-control/reservation.json"
    p.parent.mkdir(parents=True)
    p.write_text('{"tx":"foreign-pair"}')
    restarts = []
    sr.apply_bundle(bundle(host), host, queue=idle, restart=restarts.append)
    assert restarts == ["media-lab-web.service"]
    assert p.read_text() == '{"tx":"foreign-pair"}'


def test_dry_run_changes_no_live_files(host):
    restarts = []
    receipt = (host / "deployed-source.json").read_bytes()
    result = sr.apply_bundle(bundle(host), host, dry_run=True, queue=idle, restart=restarts.append)
    assert result["status"] == "dry-run" and not result["live_changed"]
    assert (host / "app.py").read_text() == "old = 1\n"
    assert (host / "deployed-source.json").read_bytes() == receipt
    assert not restarts


@pytest.mark.parametrize(
    "doc",
    [{}, {"error": "locked"}, {"active_total": 1}, {"active_total": False}, {"active": ["busy"]}],
)
def test_unknown_or_busy_queue_refuses(host, doc):
    with pytest.raises(ValueError):
        sr.apply_bundle(
            bundle(host), host, queue=lambda: doc, restart=lambda _: pytest.fail("restart")
        )
    assert (host / "app.py").read_text() == "old = 1\n"


def test_stale_preimage_refuses(host):
    b = bundle(host)
    (host / "app.py").write_text("foreign = 3\n")
    with pytest.raises(ValueError, match="preimage changed"):
        sr.apply_bundle(b, host, queue=idle)
    assert (host / "app.py").read_text() == "foreign = 3\n"


def test_second_check_refuses_pair_transition(host):
    count = 0

    def queue():
        nonlocal count
        count += 1
        if count == 1:
            p = host.parent / ".local/state/spark-pair-control/reservation.json"
            p.parent.mkdir(parents=True)
            p.write_text('{"tx":"new"}')
        return idle()

    with pytest.raises(ValueError, match="pair ownership changed"):
        sr.apply_bundle(bundle(host), host, queue=queue)
    assert (host / "app.py").read_text() == "old = 1\n"


@pytest.mark.parametrize(
    "name",
    [
        "../app.py",
        "/app.py",
        "./app.py",
        "config/local.env",
        "jobs.json",
        "runner/models/weights.json",
        "static/../app.py",
    ],
)
def test_protected_paths_refuse(host, name):
    b = bundle(host)
    b["files"][0]["path"] = name
    with pytest.raises(ValueError):
        sr.apply_bundle(b, host, queue=idle)


def test_symlink_refuses(host, tmp_path):
    outside = tmp_path / "foreign"
    outside.write_text("private")
    (host / "app.py").unlink()
    (host / "app.py").symlink_to(outside)
    with pytest.raises(ValueError, match="symlink"):
        sr.apply_bundle(bundle(host), host, queue=idle)
    assert outside.read_text() == "private"


def test_payload_corruption_refuses(host):
    b = bundle(host)
    b["files"][0]["data"] = base64.b64encode(b"corrupted").decode()
    with pytest.raises(ValueError, match="corruption"):
        sr.apply_bundle(b, host, queue=idle)


def test_restart_failure_rolls_back_new_and_old_files(host):
    b = bundle(host)
    b["files"] += bundle(host, "media_lab_core/new.py")["files"]
    before = (host / "deployed-source.json").read_bytes()
    calls = []

    def restart(service):
        calls.append(service)
        if len(calls) == 1:
            raise RuntimeError("restart failed")

    with pytest.raises(RuntimeError, match="restart failed"):
        sr.apply_bundle(b, host, queue=idle, restart=restart)
    assert (host / "app.py").read_text() == "old = 1\n"
    assert not (host / "media_lab_core/new.py").exists()
    assert (host / "deployed-source.json").read_bytes() == before
    assert len(calls) == 2


def test_failed_health_rolls_back(host):
    count = 0

    def queue():
        nonlocal count
        count += 1
        return idle() if count < 3 else {"error": "locked"}

    with pytest.raises(ValueError, match="unknown"):
        sr.apply_bundle(bundle(host), host, queue=queue, restart=lambda _: None)
    assert (host / "app.py").read_text() == "old = 1\n"


def test_healthy_new_job_after_restart_is_not_interrupted(host):
    calls = 0
    restarts = []

    def queue():
        nonlocal calls
        calls += 1
        return idle() if calls < 3 else {"active_total": 1, "active": [{"id": "new-work"}]}

    result = sr.apply_bundle(bundle(host), host, queue=queue, restart=restarts.append)
    assert result["status"] == "installed"
    assert len(restarts) == 1
    assert (host / "app.py").read_bytes() == b"new = 2\n"


def test_rollback_will_not_overwrite_foreign_writer(host):
    def restart(_):
        (host / "app.py").write_text("foreign = 4\n")
        raise RuntimeError("external writer")

    with pytest.raises(RuntimeError, match="rollback refused foreign"):
        sr.apply_bundle(bundle(host), host, queue=idle, restart=restart)
    assert (host / "app.py").read_text() == "foreign = 4\n"


def test_duplicate_refuses(host):
    b = bundle(host)
    b["files"] *= 2
    with pytest.raises(ValueError, match="duplicate"):
        sr.apply_bundle(b, host, queue=idle)


@pytest.mark.parametrize("transition_at", [1, 2])
def test_queue_probe_transition_never_mutates_live(host, transition_at):
    calls = 0

    def queue():
        nonlocal calls
        calls += 1
        if calls == transition_at:
            p = host.parent / ".local/state/spark-pair-control/reservation.json"
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text('{"tx":"different"}')
        return idle()

    with pytest.raises(ValueError, match="pair ownership changed"):
        sr.apply_bundle(bundle(host), host, queue=queue, restart=lambda _: pytest.fail("restart"))
    assert (host / "app.py").read_text() == "old = 1\n"


def test_remote_code_is_syntactically_executable():
    source = (Path(__file__).parents[1] / "tools/scoped_release.py").read_text()
    remote = source.rsplit('\nif __name__ == "__main__":', 1)[0]
    remote += "\nassert callable(apply_bundle)\n"
    result = subprocess.run(["python3", "-c", remote], capture_output=True, text=True)
    assert result.returncode == 0, result.stderr


def test_unknown_shell_option_refuses_before_ssh(tmp_path):
    script = Path(__file__).parents[1] / "tools/deploy-spark.sh"
    result = subprocess.run(
        ["bash", str(script), "fixture", "--typo"], capture_output=True, text=True
    )
    assert result.returncode == 2
    assert "unknown deployment option" in result.stderr


@pytest.mark.parametrize(
    "receipt",
    [
        {"private_overlays": [{"id": "foreign"}]},
        {"scoped_releases": [{"commit": "foreign"}]},
        "malformed",
        None,
    ],
)
def test_actual_whole_tree_entrypoint_refuses_private_receipts(tmp_path, receipt):
    import os

    root = tmp_path / "host"
    root.mkdir()
    if receipt is not None:
        (root / "deployed-source.json").write_text(json.dumps(receipt))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    ssh = bindir / "ssh"
    ssh.write_text(
        '#!/usr/bin/env python3\nimport subprocess,sys\nraise SystemExit(subprocess.run(["bash","-c",sys.argv[-1]]).returncode)\n'
    )
    ssh.chmod(0o755)
    env = dict(
        os.environ,
        PATH=str(bindir) + os.pathsep + os.environ["PATH"],
        SPARK="fixture@spark",
        REMOTE_HOME=str(root),
    )
    script = Path(__file__).parents[1] / "tools/deploy-spark.sh"
    result = subprocess.run(
        ["bash", str(script), "fixture-v1", "--dry-run"],
        cwd=script.parents[2],
        env=env,
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "whole-tree deployment refused" in result.stderr
    assert not (root / ".backups").exists()


def test_build_binds_tag_and_composition(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)

    def git(*args):
        return subprocess.check_output(["git", "-C", str(repo), *args], text=True).strip()

    git("config", "user.name", "Fixture")
    git("config", "user.email", "fixture@example.invalid")
    p = repo / "media-lab/app.py"
    p.parent.mkdir()
    p.write_text("public = 1\n")
    git("add", ".")
    git("commit", "-qm", "fixture")
    git("tag", "fixture-v1")
    git("remote", "add", "origin", "https://example.invalid/repo")
    private = tmp_path / "composed.py"
    private.write_text("public = 1\nprivate = 2\n")
    plan = tmp_path / "plan.json"
    spec = {
        "schema": 1,
        "commit": git("rev-parse", "HEAD"),
        "files": [
            {
                "path": "app.py",
                "before": None,
                "after": sr.digest(private.read_bytes()),
                "tag_sha256": sr.digest(p.read_bytes()),
                "composed": private.name,
            }
        ],
    }
    plan.write_text(json.dumps(spec))
    result = sr.build_bundle(repo, "fixture-v1", plan)
    assert base64.b64decode(result["files"][0]["data"]) == private.read_bytes()
    spec["commit"] = "stale"
    plan.write_text(json.dumps(spec))
    with pytest.raises(ValueError, match="plan/tag mismatch"):
        sr.build_bundle(repo, "fixture-v1", plan)
