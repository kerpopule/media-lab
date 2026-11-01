"""The legacy pool shim must recognize our own canonical owner, and nothing else.

Regression cover for the 2026-09-21 image outage: `media-lab-simple` held
/run/user/1000/spark-gpu.lock for a fenced image operation, `pool_lock.sh
acquire` called that holder "external", and image_service answered
503 {"error": "gpu reserved elsewhere"} on an idle box.
"""
import os
import subprocess
from pathlib import Path

import pytest

from runner.lease_owner_probe import (
    controller_owns_lock,
    default_marker_path,
    is_controller_process,
    lock_holder_pids,
)


def _dev_inode(path: Path) -> str:
    st = os.stat(path)
    return f"{os.major(st.st_dev):02x}:{os.minor(st.st_dev):02x}:{st.st_ino}"


def _locks_text(pid, dev_inode, kind="FLOCK", mode="WRITE", waiting=False):
    prefix = "->" if waiting else ""
    return f"{prefix} 1: {kind}  ADVISORY  {mode} {pid} {dev_inode} 0 EOF"


class _Fake:
    """Injected evidence so ownership can be tested without a real controller."""

    def __init__(self, *, exists=(), free=False, locks="", cgroup="", cmdline="", alive=True):
        self._paths = {str(p) for p in exists}
        self._free = free
        self.locks = locks
        self.cgroup = cgroup
        self.cmdline = cmdline
        self.alive = alive

    def path_exists(self, path):
        return str(path) in self._paths

    def lock_free(self):
        return self._free


@pytest.fixture
def lockfile(tmp_path):
    path = tmp_path / "spark-gpu.lock"
    path.write_text("")
    return path


def _owned(fake, lock, marker=Path("/tmp/marker-does-not-exist"), consult_hold=True):
    return controller_owns_lock(lock, marker, consult_hold=consult_hold,
                                lock_free=fake.lock_free,
                                path_exists=fake.path_exists, locks_text=fake.locks,
                                cgroup=fake.cgroup, cmdline=fake.cmdline, alive=fake.alive)


CONTROLLER_CGROUP = "/user.slice/user-1000.slice/user@1000.service/app.slice/media-lab-simple.service\n"
CONTROLLER_CMDLINE = ("/srv/media-lab/.venv/bin/python .venv/bin/uvicorn "
                      "app:app --host 127.0.0.1 --port 7863")


# ------------------------------------------------------------------ the live case
def test_live_controller_holder_is_recognized(lockfile):
    fake = _Fake(exists=(lockfile,), free=False, locks=_locks_text(521659, _dev_inode(lockfile)),
                 cgroup=CONTROLLER_CGROUP, cmdline=CONTROLLER_CMDLINE)
    assert _owned(fake, lockfile) is True


def test_free_lock_is_not_ownership(lockfile):
    fake = _Fake(exists=(lockfile,), free=True, locks=_locks_text(521659, _dev_inode(lockfile)),
                 cgroup=CONTROLLER_CGROUP, cmdline=CONTROLLER_CMDLINE)
    assert _owned(fake, lockfile) is False


def test_missing_lock_file_is_not_ownership(lockfile):
    fake = _Fake(exists=(), free=False, locks=_locks_text(521659, _dev_inode(lockfile)),
                 cgroup=CONTROLLER_CGROUP, cmdline=CONTROLLER_CMDLINE)
    assert _owned(fake, lockfile) is False


# ------------------------------------------------------------------ fail closed
def test_unresolved_outcome_marker_refuses_residency(lockfile):
    """A recovery hold must never be read as 'the box is ours, go ahead'."""
    marker = lockfile.parent / "gpu-recovery-hold.json"
    marker.write_text("{}")
    fake = _Fake(exists=(lockfile, marker), free=False,
                 locks=_locks_text(521659, _dev_inode(lockfile)),
                 cgroup=CONTROLLER_CGROUP, cmdline=CONTROLLER_CMDLINE)
    assert _owned(fake, lockfile, marker) is False


def test_release_question_ignores_an_unresolved_outcome(lockfile):
    """`release` must still see its own held lock: starting an idle reservation
    against it would leave a second flock waiting on a lock we own."""
    marker = lockfile.parent / "gpu-recovery-hold.json"
    marker.write_text("{}")
    fake = _Fake(exists=(lockfile, marker), free=False,
                 locks=_locks_text(521659, _dev_inode(lockfile)),
                 cgroup=CONTROLLER_CGROUP, cmdline=CONTROLLER_CMDLINE)
    assert _owned(fake, lockfile, marker) is False
    assert _owned(fake, lockfile, marker, consult_hold=False) is True


def test_release_question_still_refuses_a_foreign_holder(lockfile):
    fake = _Fake(exists=(lockfile,), free=False, locks=_locks_text(521659, _dev_inode(lockfile)),
                 cgroup="/user.slice/user-1000.slice/user@1000.service/app.slice/media-lab-sol-h3.service\n",
                 cmdline="/usr/bin/flock /run/user/1000/spark-gpu.lock /usr/bin/sleep infinity")
    assert _owned(fake, lockfile, consult_hold=False) is False


def test_dead_controller_holder_is_not_ownership(lockfile):
    fake = _Fake(exists=(lockfile,), free=False, locks=_locks_text(521659, _dev_inode(lockfile)),
                 cgroup=CONTROLLER_CGROUP, cmdline=CONTROLLER_CMDLINE, alive=False)
    assert _owned(fake, lockfile) is False


def test_foreign_holder_is_not_ownership(lockfile):
    """An outside production batch keeps its BUSY answer."""
    fake = _Fake(exists=(lockfile,), free=False, locks=_locks_text(521659, _dev_inode(lockfile)),
                 cgroup="/user.slice/user-1000.slice/user@1000.service/app.slice/media-lab-sol-h3.service\n",
                 cmdline="/usr/bin/flock /run/user/1000/spark-gpu.lock /usr/bin/sleep infinity")
    assert _owned(fake, lockfile) is False


def test_multiple_holders_are_not_ownership(lockfile):
    """Two holders is the 2026-08-17 stale-holder failure; never wave it through."""
    dev = _dev_inode(lockfile)
    fake = _Fake(exists=(lockfile,), free=False,
                 locks="\n".join([_locks_text(521659, dev), _locks_text(521660, dev)]),
                 cgroup=CONTROLLER_CGROUP, cmdline=CONTROLLER_CMDLINE)
    assert _owned(fake, lockfile) is False


def test_no_holder_row_is_not_ownership(lockfile):
    fake = _Fake(exists=(lockfile,), free=False, locks="",
                 cgroup=CONTROLLER_CGROUP, cmdline=CONTROLLER_CMDLINE)
    assert _owned(fake, lockfile) is False


def test_unreadable_evidence_is_not_ownership(lockfile):
    """Anything we could not read must keep today's BUSY behaviour."""
    fake = _Fake(exists=(lockfile,), free=False, locks="", cgroup="", cmdline="", alive=True)
    assert _owned(fake, lockfile) is False


# ------------------------------------------------------------------ /proc/locks parsing
def test_waiting_and_shared_rows_are_not_holders(lockfile):
    wanted = _dev_inode(lockfile)
    text = "\n".join([
        _locks_text(999, wanted, waiting=True),      # blocked request
        _locks_text(998, wanted, mode="READ"),       # shared lock
        _locks_text(997, wanted, kind="POSIX"),      # not a flock
        _locks_text(521659, wanted),                 # the real owner
        _locks_text(996, "00:2c:999999"),            # somebody else's file
    ])
    assert lock_holder_pids(lockfile, text) == [521659]


def test_cmdline_markers_only_reject_a_surprise_command():
    """cgroup membership is the proof; a readable command line must still match."""
    assert is_controller_process(1, cgroup=CONTROLLER_CGROUP, cmdline=CONTROLLER_CMDLINE, alive=True)
    assert not is_controller_process(1, cgroup=CONTROLLER_CGROUP,
                                     cmdline="/usr/bin/flock /run/user/1000/spark-gpu.lock sleep 1",
                                     alive=True)
    # An unreadable cmdline falls back to the cgroup proof rather than failing.
    assert is_controller_process(1, cgroup=CONTROLLER_CGROUP, cmdline="", alive=True)


def test_marker_path_uses_the_lease_database_neighbourhood(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_LAB_GPU_LEASE_DB", str(tmp_path / "pool" / "gpu-lease.sqlite3"))
    assert default_marker_path() == tmp_path / "pool" / "gpu-recovery-hold.json"
    monkeypatch.delenv("MEDIA_LAB_GPU_LEASE_DB")
    monkeypatch.setenv("MEDIA_LAB_ROOT", str(tmp_path / "media-lab-simple"))
    assert default_marker_path() == tmp_path / "media-lab-simple" / "pool" / "gpu-recovery-hold.json"


# ------------------------------------------------------------------ real flock (Linux)
@pytest.mark.skipif(not Path("/proc/locks").is_file(), reason="needs /proc/locks")
def test_real_foreign_flock_is_read_from_proc(tmp_path):
    """End-to-end on a real held flock: the holder is found, and it is not ours."""
    import fcntl

    lock = tmp_path / "spark-gpu.lock"
    fd = os.open(str(lock), os.O_RDWR | os.O_CREAT, 0o664)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        assert lock_holder_pids(lock) == [os.getpid()]
        # this test process is not the controller unit, so residency stays refused
        assert controller_owns_lock(lock, tmp_path / "gpu-recovery-hold.json") is False
    finally:
        os.close(fd)


@pytest.mark.skipif(not Path("/proc/locks").is_file(), reason="needs /proc/locks")
def test_probe_cli_exits_nonzero_on_an_unowned_lock(tmp_path):
    probe = Path(__file__).resolve().parents[1] / "runner" / "lease_owner_probe.py"
    lock = tmp_path / "spark-gpu.lock"
    result = subprocess.run(["python3", str(probe), "--lock", str(lock),
                             "--marker", str(tmp_path / "absent.json")],
                            capture_output=True, text=True, timeout=60, check=False)
    assert result.returncode == 1, result.stdout + result.stderr