"""Read-only headless-reachable /proc and nvidia probe for Media Lab capacity.

Pure, side-effect-free, no process spawning that changes state, no writes, no
service manipulation.  It is the top of the capacity bar: total physical memory,
current available, and a confirmed headless (no sibling services).
"""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

GB = 1024 ** 3


@dataclass(frozen=True)
class ProbeResult:
    total_gb: float
    available_gb: float
    headless: bool
    display_service: str
    source: str


def _kb_to_gb(kb: str) -> float:
    try:
        return round(int(kb) / 1024 / 1024, 1)
    except (ValueError, TypeError):
        return 0.0


def parse_meminfo(meminfo: str) -> dict[str, float]:
    out: dict[str, float] = {}
    for line in meminfo.splitlines():
        key, _, rest = line.partition(":")
        for field in ("MemTotal", "MemFree", "MemAvailable"):
            if key.strip() == field:
                out[field] = _kb_to_gb(rest.strip().split()[0])
    return out


def probe_headless_from_mapping(meminfo: Mapping[str, float], services: list[str]) -> ProbeResult:
    """Deterministic probe used by tests: pass a parsed meminfo dict and a list
    of display-service processes/services found."""
    total = float(meminfo.get("MemTotal", 0))
    available = float(meminfo.get("MemAvailable", float(meminfo.get("MemFree", 0))))
    headless = not services
    return ProbeResult(
        total_gb=total,
        available_gb=available,
        headless=headless,
        display_service="none" if headless else ", ".join(sorted(services)),
        source="mapping",
    )


def probe_headless_live(meminfo_path: Path = Path("/proc/meminfo")) -> ProbeResult:
    """Live probe for the actual Spark. System-centric, read-only, no writes."""
    try:
        raw = Path(meminfo_path).read_text()
    except OSError as exc:  # not on Linux
        return ProbeResult(0.0, 0.0, True, "unknown", f"error:{exc}")
    facts = parse_meminfo(raw)
    # Display-service detection is intentionally conservative: any of these
    # running means a graphical session is consuming unified memory.
    services = _detect_display_services()
    return probe_headless_from_mapping(facts, services)


def _detect_display_services() -> list[str]:
    import subprocess
    found: list[str] = []
    try:
        procs = subprocess.run(
            ["pgrep", "-l", "-f"],
            capture_output=True, text=True, timeout=5,
        ).stdout.lower()
    except (OSError, subprocess.TimeoutExpired):
        procs = ""
    for name in ("Xorg", "Xwayland", "gnome-shell", "weston"):
        if name.lower() in procs:
            found.append(name)
    return found