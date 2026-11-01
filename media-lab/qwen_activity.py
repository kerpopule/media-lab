"""Fail-closed Qwen activity probing for residency admission.

The Qwen service exposes scheduler gauges on ``/metrics`` as Prometheus text,
not JSON.  Port 8003 is an OpenAI-compatible shim for the authoritative
runtime on port 8004, so activity admission samples the canonical runtime once
and records the shim as skipped rather than double-counting it.
"""

from __future__ import annotations

import math
import re
import urllib.request
from typing import Iterable

# The deployed Qwen runtime is authoritative on 8004.  8003 is a shim and must
# never become a second activity sample for the same backend.
DEFAULT_ENDPOINTS = ("8004", "8003")
SHIM_ALIASES = {"8003": "8004"}

# vLLM's names are reviewed directly from the runtime evidence.  SGLang uses
# the *_reqs spellings in its scheduler metrics.  Matching is exact after the
# runtime prefix is normalized, never a substring search through labels.
RUNNING_METRICS = frozenset({
    "num_requests_running",
    "num_running_reqs",
    "running_requests",
    "requests_running",
    # llama.cpp server (including the pinned Qwen4-exp runtime used by
    # Qwen3.8-Flash-Next) publishes llamacpp:requests_processing.
    "requests_processing",
    "num_running",
})
WAITING_METRICS = frozenset({
    "num_requests_waiting",
    "num_queue_reqs",
    "waiting_requests",
    "requests_waiting",
    # llama.cpp's task queue calls waiting work "deferred".
    "requests_deferred",
    "num_waiting",
})
_NAME_RE = re.compile(r"^([a-zA-Z_:][a-zA-Z0-9_:]*)")
_SAMPLE_RE = re.compile(
    r'^([a-zA-Z_:][a-zA-Z0-9_:]*)'
    r'(?:\{(?:[^"{}]|"(?:\\.|[^"\\])*")*\})?'
    r'\s+(\S+)(?:\s+[+-]?\d+)?\s*$'
)


def prometheus_gauge_text(url: str, timeout: int = 3) -> str:
    """Fetch a Prometheus exposition body verbatim (never JSON)."""
    req = urllib.request.Request(url)
    with urllib.request.urlopen(req, timeout=timeout) as response:
        return response.read().decode("utf-8", "replace")


def _metric_name(sample_name: str) -> str:
    """Normalize ``vllm:num_requests_running{...}`` to a metric name."""
    name = sample_name.split("{", 1)[0].strip().lower()
    return name.replace(":", "_").replace(".", "_")


def _metric_kind(name: str) -> str | None:
    normalized = _metric_name(name)
    for metric in RUNNING_METRICS:
        if normalized == metric or normalized.endswith("_" + metric):
            return "running"
    for metric in WAITING_METRICS:
        if normalized == metric or normalized.endswith("_" + metric):
            return "waiting"
    return None


def parse_activity_gauges(raw: str) -> tuple[float, float, bool]:
    """Return ``(running, waiting, complete_valid_gauges)`` from Prometheus text.

    Values are summed across label dimensions.  A recognized negative,
    non-finite, or malformed sample invalidates the activity result instead of
    allowing a broken exporter to look idle. Both running and waiting gauges
    must be present; a missing family is not evidence of an empty scheduler.
    """
    running = waiting = 0.0
    seen: set[str] = set()
    invalid = False
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        name = _NAME_RE.match(line)
        kind = _metric_kind(name.group(1)) if name else None
        if kind is None:
            continue
        match = _SAMPLE_RE.fullmatch(line)
        if not match:
            invalid = True
            continue
        try:
            value = float(match.group(2))
        except ValueError:
            invalid = True
            continue
        if value < 0 or not math.isfinite(value):
            invalid = True
            continue
        seen.add(kind)
        if kind == "running":
            running += value
        else:
            waiting += value
    if invalid:
        return 0.0, 0.0, False
    return running, waiting, seen == {"running", "waiting"}


def _canonical_endpoints(endpoints: Iterable[str]) -> tuple[tuple[str, ...], dict[str, str]]:
    selected: list[str] = []
    skipped: dict[str, str] = {}
    seen: set[str] = set()
    for raw_port in endpoints:
        port = str(raw_port)
        canonical = SHIM_ALIASES.get(port, port)
        if canonical in seen:
            skipped[port] = canonical
            continue
        seen.add(canonical)
        selected.append(canonical)
    return tuple(selected), skipped


def probe_text_activity(
    endpoints: tuple[str, ...] = DEFAULT_ENDPOINTS,
    timeout: int = 3,
) -> tuple[str, dict]:
    """Return ``(busy|idle|unknown, non-secret diagnostics)``.

    ``idle`` is emitted only when a canonical endpoint answered and exposed
    valid running=0 and waiting=0 gauges.  Unreachable or gauge-less resident
    runtimes are ``unknown`` and therefore block eviction.  If callers provide
    multiple distinct canonical endpoints, the strictest verdict wins:
    ``busy > unknown > idle``.
    """
    canonical, skipped = _canonical_endpoints(endpoints)
    probes: list[str] = []
    statuses: dict[str, dict[str, object]] = {}
    verdicts: list[tuple[str, str, float, float]] = []

    for port in canonical:
        url = f"http://127.0.0.1:{port}/metrics"
        try:
            raw = prometheus_gauge_text(url, timeout=timeout)
        except Exception as exc:
            statuses[port] = {"status": "unreachable", "error_type": type(exc).__name__}
            continue
        probes.append(port)
        running, waiting, saw = parse_activity_gauges(raw)
        if not saw:
            state = "unknown"
            statuses[port] = {"status": "reachable", "activity": "unreadable"}
        elif running > 0 or waiting > 0:
            state = "busy"
            statuses[port] = {"status": "reachable", "activity": "readable"}
        else:
            state = "idle"
            statuses[port] = {"status": "reachable", "activity": "readable"}
        verdicts.append((state, port, running, waiting))

    if not verdicts:
        return "unknown", {
            "state": "unknown",
            "source": None,
            "probes": [],
            "skipped_shims": skipped,
            "probe_status": statuses,
            "detail": "no canonical text endpoint answered /metrics",
            "running": 0.0,
            "waiting": 0.0,
        }

    order = {"busy": 3, "unknown": 2, "idle": 1}
    state, port, running, waiting = max(verdicts, key=lambda item: order[item[0]])
    if state == "busy":
        detail = f"{port} reports running={running:g} waiting={waiting:g}"
    elif state == "idle":
        detail = f"{port} reports running=0 waiting=0"
    else:
        detail = f"{port} answered /metrics but exposed no valid activity gauge; cannot prove idle"
    return state, {
        "state": state,
        "source": port,
        "probes": probes,
        "skipped_shims": skipped,
        "probe_status": statuses,
        "detail": detail,
        "running": running,
        "waiting": waiting,
    }
