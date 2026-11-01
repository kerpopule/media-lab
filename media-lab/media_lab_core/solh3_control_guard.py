"""Independent, stdlib-only Sol-H3 control-plane pressure guard.

The guard runs outside the H3 transient unit.  It never imports CUDA/model code,
never restarts a workload, and terminates only PIDs proven to belong to the exact
H3 cgroup.  A fresh boot-bound heartbeat is also the H3 admission token.
"""
import json
import os
import signal
import sys
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path

GIB_KIB = 1024 * 1024
HEARTBEAT_VERSION = 1
UNIT = "media-lab-sol-h3.service"


@dataclass(frozen=True)
class GuardThresholds:
    min_available_gib: float = 8.0
    max_swap_growth_gib: float = 2.0
    max_psi_full_avg10: float = 10.0
    consecutive: int = 3


@dataclass(frozen=True)
class PressureSample:
    available_kib: int
    swap_total_kib: int
    swap_free_kib: int
    psi_some_avg10: float
    psi_full_avg10: float

    @property
    def swap_used_kib(self) -> int:
        return max(0, self.swap_total_kib - self.swap_free_kib)


class PressureTracker:
    """Require consecutive evidence for one pressure mode before tripping."""

    def __init__(self, limits: GuardThresholds, baseline: PressureSample):
        self.limits = limits
        self.baseline_swap_used_kib = baseline.swap_used_kib
        self.strikes: dict[str, int] = {
            "low-memavailable": 0,
            "swap-growth": 0,
            "memory-psi": 0,
        }

    def observe(self, current: PressureSample) -> str | None:
        triggered = {
            "low-memavailable": current.available_kib < self.limits.min_available_gib * GIB_KIB,
            "swap-growth": (
                current.swap_used_kib - self.baseline_swap_used_kib
                >= self.limits.max_swap_growth_gib * GIB_KIB
            ),
            "memory-psi": current.psi_full_avg10 >= self.limits.max_psi_full_avg10,
        }
        for reason, active in triggered.items():
            self.strikes[reason] = self.strikes[reason] + 1 if active else 0
        for reason in ("low-memavailable", "swap-growth", "memory-psi"):
            if self.strikes[reason] >= self.limits.consecutive:
                return reason
        return None


def write_runtime_environment(path: Path, values: dict[str, str]) -> None:
    """Atomically write a private systemd EnvironmentFile for one H3 lease."""
    lines = []
    for key, raw in sorted(values.items()):
        if not key or not all(char == "_" or char.isupper() or char.isdigit() for char in key):
            raise ValueError("invalid environment key: " + repr(key))
        value = str(raw)
        if "\n" in value or "\r" in value or "\x00" in value:
            raise ValueError("control character in environment value for " + key)
        escaped = value.replace("\\", "\\\\").replace('"', '\\"')
        lines.append(f'{key}="{escaped}"')
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}-{uuid.uuid4().hex}.tmp")
    fd = os.open(str(temporary), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(str(temporary), str(path))
    finally:
        temporary.unlink(missing_ok=True)


def heartbeat_allows_h3(
    path: Path,
    *,
    boot_id: str,
    now: float | None = None,
    max_age_s: float = 5.0,
) -> bool:
    """A missing, stale, wrong-boot, malformed, or non-ready guard fails closed."""
    try:
        row = json.loads(path.read_text(encoding="utf-8"))
        current = time.time() if now is None else now
        return (
            isinstance(row, dict)
            and row.get("version") == HEARTBEAT_VERSION
            and row.get("boot_id") == boot_id
            and row.get("state") in ("ready", "monitoring")
            and isinstance(row.get("written_at"), (int, float))
            and 0 <= current - float(row["written_at"]) <= max_age_s
        )
    except (OSError, ValueError, TypeError):
        return False


def _parse_key_values(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) >= 2:
            values[parts[0].rstrip(":")] = parts[1]
    return values


def read_pressure_sample(meminfo: Path = Path("/proc/meminfo"),
                         psi: Path = Path("/proc/pressure/memory")) -> PressureSample:
    memory = _parse_key_values(meminfo)
    psi_rows: dict[str, dict[str, float]] = {}
    for line in psi.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if not parts:
            continue
        psi_rows[parts[0]] = {
            key: float(value) for key, value in (field.split("=", 1) for field in parts[1:])
        }
    return PressureSample(
        available_kib=int(memory["MemAvailable"]),
        swap_total_kib=int(memory.get("SwapTotal", "0")),
        swap_free_kib=int(memory.get("SwapFree", "0")),
        psi_some_avg10=float(psi_rows.get("some", {}).get("avg10", 0.0)),
        psi_full_avg10=float(psi_rows.get("full", {}).get("avg10", 0.0)),
    )


def find_unit_cgroup(root: Path, unit: str = UNIT) -> Path | None:
    """Resolve exactly one unit cgroup; ambiguity is never guessed through."""
    if not root.exists():
        return None
    matches = sorted(path for path in root.rglob(unit) if path.is_dir() and path.name == unit)
    if len(matches) > 1:
        raise RuntimeError("ambiguous H3 unit cgroups: " + ", ".join(map(str, matches)))
    return matches[0] if matches else None


def cgroup_pids(cgroup: Path) -> set[int]:
    """Read exact recursive membership; only a removed unit is safely empty."""
    root_members = cgroup / "cgroup.procs"
    try:
        member_files = [root_members, *(
            path for path in cgroup.rglob("cgroup.procs") if path != root_members
        )]
        pids: set[int] = set()
        for member_file in member_files:
            for value in member_file.read_text(encoding="utf-8").split():
                pid = int(value)
                if pid > 1 and pid != os.getpid():
                    pids.add(pid)
        return pids
    except FileNotFoundError as exc:
        # systemd removes an empty unit cgroup as its last process exits. That
        # exact disappearance is positive proof of no remaining members, not an
        # observer failure. A missing member file while the unit still exists
        # remains fail-closed because membership could not be proven.
        try:
            cgroup.stat()
        except FileNotFoundError:
            return set()
        except OSError:
            pass
        raise RuntimeError(
            f"cannot read exact H3 cgroup membership at {cgroup}"
        ) from exc
    except (OSError, ValueError) as exc:
        raise RuntimeError(
            f"cannot read exact H3 cgroup membership at {cgroup}"
        ) from exc


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True


def _pidfd_open(pid: int) -> int:
    opener = getattr(os, "pidfd_open", None)
    if opener is None:
        raise RuntimeError("pidfd_open is required for race-safe cgroup termination")
    return opener(pid)


def _pidfd_signal(pidfd: int, sig: int) -> None:
    sender = getattr(signal, "pidfd_send_signal", None)
    if sender is None:
        raise RuntimeError("pidfd_send_signal is required for race-safe cgroup termination")
    sender(pidfd, sig)


def terminate_cgroup(
    cgroup: Path,
    *,
    term_wait_s: float = 10.0,
    kill_wait_s: float = 2.0,
    pidfd_open_fn: Callable[[int], int] = _pidfd_open,
    pidfd_signal_fn: Callable[[int, int], None] = _pidfd_signal,
    close_fn: Callable[[int], None] = os.close,
    alive_fn: Callable[[int], bool] = _pid_alive,
    sleep_fn: Callable[[float], None] = time.sleep,
) -> list[int]:
    """Bounded TERM/KILL of exact, recursively proven cgroup identities."""

    def live_members() -> set[int]:
        return {pid for pid in cgroup_pids(cgroup) if alive_fn(pid)}

    def signal_members(sig: int) -> None:
        for pid in sorted(live_members(), reverse=True):
            try:
                pidfd = pidfd_open_fn(pid)
            except ProcessLookupError:
                continue
            try:
                # Opening a pidfd pins one process identity. Revalidate cgroup
                # membership after opening it so a PID that moved or was reused
                # outside the exact unit is never signalled.
                if pid not in cgroup_pids(cgroup):
                    continue
                pidfd_signal_fn(pidfd, sig)
            except ProcessLookupError:
                pass
            finally:
                close_fn(pidfd)

    signal_members(signal.SIGTERM)
    deadline = time.monotonic() + max(0.0, term_wait_s)
    while time.monotonic() < deadline and live_members():
        sleep_fn(min(0.1, max(0.0, deadline - time.monotonic())))
    if live_members():
        signal_members(signal.SIGKILL)
    deadline = time.monotonic() + max(0.0, kill_wait_s)
    while time.monotonic() < deadline and live_members():
        sleep_fn(min(0.05, max(0.0, deadline - time.monotonic())))
    return sorted(live_members())


def _atomic_json(path: Path, value: dict, *, replace: bool = True) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    if not replace and path.exists():
        return False
    temporary = path.with_name("." + path.name + "." + uuid.uuid4().hex + ".tmp")
    flags = os.O_CREAT | os.O_EXCL | os.O_WRONLY
    fd = os.open(str(temporary), flags, 0o600)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        if replace:
            os.replace(str(temporary), str(path))
        else:
            try:
                os.link(str(temporary), str(path))
            except FileExistsError:
                return False
            finally:
                temporary.unlink(missing_ok=True)
        directory_fd = os.open(str(path.parent), os.O_RDONLY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
        return True
    finally:
        temporary.unlink(missing_ok=True)


class ControlPlaneGuard:
    def __init__(self) -> None:
        self.runtime = Path(os.environ.get("XDG_RUNTIME_DIR", "/tmp"))
        self.sol_root = Path(os.path.expanduser(os.environ.get(
            "SOL_ROOT", "~/.local/share/sol-h3-spark"
        )))
        self.boot_id_path = Path(os.environ.get(
            "SOL_BOOT_ID_PATH", "/proc/sys/kernel/random/boot_id"
        ))
        self.boot_id = self.boot_id_path.read_text(encoding="utf-8").strip()
        self.heartbeat = Path(os.environ.get(
            "SOL_H3_GUARD_HEARTBEAT", str(self.runtime / "solh3-control-plane-guard.json")
        ))
        default_cgroup_root = Path(
            f"/sys/fs/cgroup/user.slice/user-{os.getuid()}.slice/user@{os.getuid()}.service"
        )
        self.cgroup_root = Path(os.environ.get(
            "SOL_H3_GUARD_CGROUP_ROOT", str(default_cgroup_root)
        ))
        self.latch = self.runtime / "flashnext-memwatch.latch"
        self.safety_stop = self.sol_root / "safety-stop.json"
        self.incident_dir = self.sol_root / "control-plane-incidents"
        self.poll_s = float(os.environ.get("SOL_H3_GUARD_POLL_S", "1"))
        self.limits = GuardThresholds(
            min_available_gib=float(os.environ.get("SOL_H3_GUARD_MIN_AVAILABLE_GIB", "8")),
            max_swap_growth_gib=float(os.environ.get("SOL_H3_GUARD_MAX_SWAP_GROWTH_GIB", "2")),
            max_psi_full_avg10=float(os.environ.get("SOL_H3_GUARD_MAX_PSI_FULL_AVG10", "10")),
            consecutive=int(os.environ.get("SOL_H3_GUARD_CONSECUTIVE", "3")),
        )
        self.tracker: PressureTracker | None = None
        self.cgroup: Path | None = None

    def _write_heartbeat(self, state: str, sample: PressureSample) -> None:
        _atomic_json(self.heartbeat, {
            "version": HEARTBEAT_VERSION,
            "boot_id": self.boot_id,
            "pid": os.getpid(),
            "state": state,
            "written_at": time.time(),
            "available_kib": sample.available_kib,
            "swap_used_kib": sample.swap_used_kib,
            "psi_full_avg10": sample.psi_full_avg10,
            "thresholds": asdict(self.limits),
        })

    def _trip(self, reason: str, sample: PressureSample) -> None:
        incident_id = f"{int(time.time())}-{uuid.uuid4().hex[:12]}"
        record = {
            "version": 1,
            "incident_id": incident_id,
            "reason": reason,
            "unit": UNIT,
            "boot_id": self.boot_id,
            "time": time.time(),
            "sample": asdict(sample),
            "thresholds": asdict(self.limits),
        }
        _atomic_json(self.safety_stop, record, replace=False)
        self.latch.parent.mkdir(parents=True, exist_ok=True)
        try:
            self.latch.write_text(incident_id + "\n", encoding="utf-8")
        except OSError:
            pass
        targets = sorted(cgroup_pids(self.cgroup)) if self.cgroup else []
        record["target_pids"] = targets
        record["status"] = "terminating"
        incident_path = self.incident_dir / (incident_id + ".json")
        # Persist the exact cause and target set before signalling. If the host or
        # observer dies during bounded termination, operators still retain a
        # durable incident receipt instead of only an unexplained safety latch.
        _atomic_json(incident_path, record)
        survivors = terminate_cgroup(self.cgroup) if self.cgroup else []
        record["survivors"] = survivors
        record["status"] = "survivors" if survivors else "terminated"
        _atomic_json(incident_path, record)
        if survivors:
            raise RuntimeError("H3 cgroup survivors after bounded termination: " + str(survivors))

    def step(self) -> str:
        sample = read_pressure_sample()
        if self.cgroup is None or not self.cgroup.exists():
            self.cgroup = find_unit_cgroup(self.cgroup_root)
        pids = cgroup_pids(self.cgroup) if self.cgroup else set()
        if self.safety_stop.exists() or self.latch.exists():
            self.tracker = None
            # A controller relaunch must not turn durable evidence into a bypass.
            # Exact cgroup proof is required; no process-name or broad kill scan.
            if pids:
                assert self.cgroup is not None
                survivors = terminate_cgroup(self.cgroup)
                if survivors:
                    raise RuntimeError(
                        "quarantined H3 cgroup survivors after bounded termination: "
                        + str(survivors)
                    )
            self._write_heartbeat("quarantined", sample)
            return "quarantined"
        if not pids:
            self.tracker = None
            self._write_heartbeat("ready", sample)
            return "ready"
        if self.tracker is None:
            self.tracker = PressureTracker(self.limits, sample)
        reason = self.tracker.observe(sample)
        if reason:
            self._trip(reason, sample)
            self._write_heartbeat("quarantined", sample)
            return "quarantined"
        self._write_heartbeat("monitoring", sample)
        return "monitoring"

    def run(self) -> None:
        while True:
            try:
                self.step()
            except Exception as exc:
                # A broken observer cannot grant admission. Remove its token and
                # let systemd restart it; no broad kill or reboot is attempted.
                self.heartbeat.unlink(missing_ok=True)
                raise RuntimeError("Sol-H3 control-plane guard failed closed") from exc
            time.sleep(self.poll_s)


def current_heartbeat_allows_h3() -> bool:
    runtime = Path(os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}"))
    heartbeat = Path(os.environ.get(
        "SOL_H3_GUARD_HEARTBEAT", str(runtime / "solh3-control-plane-guard.json")
    ))
    boot_path = Path(os.environ.get("SOL_BOOT_ID_PATH", "/proc/sys/kernel/random/boot_id"))
    try:
        boot_id = boot_path.read_text(encoding="utf-8").strip()
    except OSError:
        return False
    return heartbeat_allows_h3(heartbeat, boot_id=boot_id)


def main() -> int:
    if sys.argv[1:] == ["--check-heartbeat"]:
        return 0 if current_heartbeat_allows_h3() else 1
    if sys.argv[1:]:
        print("usage: python -m media_lab_core.solh3_control_guard [--check-heartbeat]", file=sys.stderr)
        return 2
    ControlPlaneGuard().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
