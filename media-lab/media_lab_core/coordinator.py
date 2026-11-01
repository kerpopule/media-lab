"""Transaction / recovery coordinator bridging the capacity planner into the
existing residency controller.

Residency (``residency.py``) already owns the crash-safe transaction receipt,
exact rollback, degraded-state reporting, and recovery.  This module adds:

* ``plan_to_profile`` — translate a capacity plan's selected slots into the
  residency controller's named-profile ``slots`` dict,
* ``reconcile_to_committed`` — boot reconciliation toward the last committed
  desired profile (read-only introspection; mutation still goes through calls),
* a `BusyGuard` that only *blocks/drains* (never kills) a busy service.

It deliberately does NOT re-implement apply/rollback/recover — those live in the
residency machine and are exercised directly by the proof-5 tests.
"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping

from residency import ResidencyController  # REUSE, do not replace


def plan_to_profile(plan: Mapping[str, Any]) -> dict[str, Any]:
    """Map a capacity plan's resident models into a residency ``slots`` dict.

    The residency policy uses ``text_primary`` and ``video_primary`` slots; the
    capacity plan keys by modality.  We map the known ones and leave the rest
    unset so residency validation can still refuse unknown combos.
    """
    selected = dict(plan.get("selected") or {})
    slots: dict[str, Any] = {}
    text = [m for k, m in selected.items() if k in ("language",)]
    video = [m for k, m in selected.items() if k == "video"]
    # Map model ids to residency slot names:
    #   qwen -> text_primary=qwen (if no deepseek-style full-memory text picked)
    if text:
        slots["text_primary"] = text[0]
    if video:
        slots["video_primary"] = video[0]
        if len(video) > 1:
            slots["video_secondary"] = video[1]
    return slots


class BusyGuard:
    """Drain-only guard: never kills a busy service when the capacity planner
    says a model must swap out. It only reports 'busy' so the caller can wait
    (or the scheduler can defer) rather than terminating healthy work."""

    def __init__(self, probes: Mapping[str, Any] | None = None):
        # probes: model_id -> callable returning truthy if busy
        self._probes = dict(probes or {})

    def guard(self, draining: list[str]) -> dict[str, Any]:
        busy = [m for m in draining if self._probes.get(m, lambda: False)()]
        return {
            "busy": busy,
            "killable": [m for m in draining if m not in busy],
            "action": ("wait-for-busy" if busy else "drain"),
            "never_killed": True,
        }


def reconcile_desired(controller: ResidencyController) -> dict[str, Any]:
    """Boot reconciliation read: return the last committed desired profile and
    whether actual matches it. Mutation (apply) is up to the caller, so this is
    safe to call at read time and never silently mutates."""
    try:
        actual = controller.state()
    except Exception as exc:
        return {"ok": False, "error": str(exc)}
    desired = controller.desired()
    return {
        "ok": True,
        "desired": desired,
        "actual": actual,
        "matches": bool(desired.get("name") and desired.get("name") == actual.get("profile", {}).get("name")),
    }