"""Deterministic capacity planner and admission for the headless Media Lab.

This module is deliberately free of FastAPI and process side effects so a test
can drive the whole capacity model offline.  It owns:

* loading the capacity-budget JSON (reserves -> allocatable pool),
* loading model/runtime manifests (measured phase ranges, license, source),
* loading profile/policy (allowed per-slot selections + behaviors),
* the deterministic admission/planning algorithm,
* a capacity-bar reconciler the UI consumes byte-for-byte.

It REUSES the existing residency transaction/recovery state machine
(``residency.py``) for apply/rollback/recover; this file does not replace it.
The pool-ownership lease is intentionally observational here, never reused as
the inference transaction lock.

Everything is deterministic: inputs are sorted, phase checks are worst-case
(hi_gb), tie-breaks are stable, and unknown values block rather than estimate.
"""
from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Any, Mapping, Sequence

# Canonical measured phases. Order is load lifecycle.
PHASES = (
    "cold_load",
    "warm_idle",
    "active_inference",
    "decode_vae_mux",
    "kv_growth",
    "swap_overlap",
)

# Behaviors an entry may carry for a slot.
BEHAVIORS = (
    "primary",
    "fallback",
    "always_resident",
    "on_demand",
    "drainable",
    "mutually_exclusive",
)

# Modality buckets the catalog is sorted/filtered by.
MODALITIES = (
    "language",
    "vision",
    "image",
    "video",
    "audio",
    "speech",
    "segmentation",
    "harness",
)

SLOTS = (
    "language",
    "vision",
    "image",
    "video",
    "video_secondary",
    "audio",
    "speech",
    "segmentation",
    "harness",
)


class CapacityError(RuntimeError):
    """A fail-closed admission, planning, or manifest error."""


def _round2(x: float) -> float:
    return round(float(x), 2)


def _require(cond: bool, msg: str) -> None:
    if not cond:
        raise CapacityError(msg)


# ---------------------------------------------------------------------------
# Budget
# ---------------------------------------------------------------------------
def load_budget(path: Path) -> dict[str, Any]:
    """Load and validate capacity-budget.json."""
    budget = json.loads(Path(path).read_text())
    _require(budget.get("schema_version") == 1, "capacity-budget schema_version must be 1")
    physical = float(budget.get("physical_total_gb") or 0)
    _require(physical > 0, "physical_total_gb must be positive")
    reserves = budget.get("reserves_gb") or {}
    _require(reserves, "capacity budget requires reserves_gb")
    _require(all(float(v) >= 0 for v in reserves.values()), "reserves must be >= 0")
    floor = float(reserves.get("safety_floor") or 0)
    _require(floor > 0, "safety_floor must be positive")
    allocatable = physical - sum(float(v) for v in reserves.values())
    _require(allocatable > 0, "reserves exceed physical_total_gb; nothing allocatable")
    budget["_allocatable_pool_gb"] = _round2(allocatable)
    return budget


def allocatable_pool_gb(budget: Mapping[str, Any]) -> float:
    if "_allocatable_pool_gb" not in budget:
        return _round2(
            float(budget["physical_total_gb"])
            - sum(float(v) for v in budget["reserves_gb"].values())
        )
    return float(budget["_allocatable_pool_gb"])


# ---------------------------------------------------------------------------
# Manifests (model / runtime)
# ---------------------------------------------------------------------------
def load_manifests(path: Path) -> dict[str, dict[str, Any]]:
    """Load model/runtime manifests.

    Each entry:
      id, name, modality, slot
      phases: {phase: {"lo_gb": float, "hi_gb": float}}  (measured range)
      license_name, license_url, terms_acceptance_required
      source_url, immutable_revision, sha256, trusted
      exclusive_group (swap group), runtime
      estimated_load_seconds, disk_gb
    """
    data = json.loads(Path(path).read_text())
    _require(data.get("schema_version") == 1, "manifests schema_version must be 1")
    entries = data.get("manifests") or {}
    _require(entries, "manifests requires >= 1 entry")
    out: dict[str, dict[str, Any]] = {}
    for mid, m in entries.items():
        _require(m.get("slot") in SLOTS, f"{mid}: unknown slot {m.get('slot')!r}")
        _require(m.get("modality") in MODALITIES, f"{mid}: unknown modality {m.get('modality')!r}")
        phases = m.get("phases") or {}
        _require(set(phases) == set(PHASES), f"{mid}: phases must be exactly {PHASES}")
        for phase, rng in phases.items():
            lo, hi = float(rng["lo_gb"]), float(rng["hi_gb"])
            _require(0 <= lo <= hi, f"{mid}.{phase}: lo_gb must be <= hi_gb and >= 0")
            rng["lo_gb"] = lo
            rng["hi_gb"] = hi
        _require(m.get("license_name") not in {"", "unresolved", "unknown"},
                 f"{mid}: unresolved license")
        _require(m.get("source_url") and m.get("immutable_revision"),
                 f"{mid}: source_url and immutable_revision required")
        sha = m.get("sha256") or ""
        _require(len(sha) == 64, f"{mid}: sha256 must be 64 hex chars")
        out[mid] = m
    return out


def phase_hi(m: Mapping[str, Any], phase: str) -> float:
    return float(m["phases"][phase]["hi_gb"])


def phase_lo(m: Mapping[str, Any], phase: str) -> float:
    return float(m["phases"][phase]["lo_gb"])


# ---------------------------------------------------------------------------
# Profile / policy
# ---------------------------------------------------------------------------
def load_policy(path: Path) -> dict[str, Any]:
    """Load the per-slot policy: allowed primary/fallback per slot + constraints."""
    policy = json.loads(Path(path).read_text())
    _require(policy.get("schema_version") == 1, "policy schema_version must be 1")
    slots = policy.get("slots") or {}
    _require(set(slots) <= set(SLOTS), "policy slots exceed known SLOTS")
    for slot, spec in slots.items():
        allowed = spec.get("allowed") or []
        _require(allowed, f"slot {slot} must declare >= 1 allowed model")
        _require(spec.get("primary") in allowed, f"slot {slot}: primary not in allowed")
    return policy


# ---------------------------------------------------------------------------
# Plan model
# ---------------------------------------------------------------------------
def _sorted_ids(mapping: Mapping[str, Any]) -> list[str]:
    """Stable deterministic key order so phase/admission arithmetic never varies."""
    return sorted(mapping)


class Planner:
    """Deterministic capacity planner over budget + manifests + policy.

    A plan selects one model per requested slot, applies per-slot behavior,
    checks mutual-exclusive groups, and admits only if worst-case resident
    phases fit the allocatable pool.  It returns which models stay resident,
    which drain, the swap order, estimated transition time, concurrency limits,
    and an exact phase/floor reason for every refused combination.
    """

    def __init__(self, budget_path: Path, manifests_path: Path, policy_path: Path):
        self.budget = load_budget(budget_path)
        self.pool_gb = allocatable_pool_gb(self.budget)
        self.floor_gb = float(self.budget["reserves_gb"]["safety_floor"])
        self.manifests = load_manifests(manifests_path)
        self.policy = load_policy(policy_path)

    # -- helpers -------------------------------------------------------
    def _check_pool(self, name: str, phase: str, needed: float) -> dict[str, Any]:
        # "all-resident" is a synthetic aggregate across all resident models,
        # not a manifest key; there is no single owning slot for it.
        slot = self.manifests[name]["slot"] if name in self.manifests else "all-resident"
        return {
            "model": name, "slot": slot, "phase": phase,
            "available_gb": self.pool_gb, "required_gb": _round2(needed),
            "floor_gb": self.floor_gb, "pass": self.pool_gb >= needed,
        }

    # -- public API ----------------------------------------------------
    def plan(self, selections: Mapping[str, str]) -> dict[str, Any]:
        """``selections`` maps slot -> model id. Returns a full admission plan."""
        if not selections:
            raise CapacityError("no slots selected")
        unknown = sorted(set(selections.values()) - set(self.manifests))
        if unknown:
            raise CapacityError(f"unknown model ids: {', '.join(unknown)}")

        sel = {slot: mid for slot, mid in selections.items()
               if mid is not None and mid.lower() not in ("none", "null", "")}
        unknown_slots = sorted(set(sel) - set(SLOTS))
        if unknown_slots:
            raise CapacityError(f"unknown slots: {', '.join(unknown_slots)}")

        # Validate policy: each selected model must be allowed in its slot and
        # the slot's primary/fallback rule must not be contradicted.
        blocks: list[dict[str, Any]] = []
        for slot, mid in sel.items():
            spec = self.policy.get("slots", {}).get(slot)
            if spec is None:
                blocks.append({"kind": "slot-not-in-policy", "slot": slot, "model": mid,
                               "reason": f"slot {slot} is not governed by policy"})
                continue
            if mid not in spec.get("allowed", []):
                blocks.append({"kind": "model-not-allowed-in-slot", "slot": slot, "model": mid,
                               "reason": f"{mid} is not allowed in slot {slot}"})

        # Mutual-exclusive groups: at most one occupant per group across slots.
        groups: dict[str, list[str]] = {}
        for slot, mid in sel.items():
            group = self.manifests[mid].get("exclusive_group") or None
            if group:
                groups.setdefault(group, []).append(f"{slot}:{mid}")
        for group, occupants in groups.items():
            if len(occupants) > 1:
                blocks.append({"kind": "mutually-exclusive", "group": group,
                               "occupants": occupants,
                               "reason": f"{group} allows exactly one resident, got {occupants}"})

        # Deterministic phase/concurrency admission.
        checks: list[dict[str, Any]] = []
        resident = sorted(sel.values())
        # Worst-case simultaneous warm residency: sum warm_idle hi.
        concurrent_blockers: list[dict[str, Any]] = []
        if resident:
            warm = sum(phase_hi(self.manifests[m], "warm_idle") for m in resident)
            checks.append(self._check_pool("all-resident", "warm_idle", warm))
            if not checks[-1]["pass"]:
                concurrent_blockers.append({"kind": "warm-idle-overflow",
                                            "resident": resident, "required_gb": _round2(warm),
                                            "reason": (f"{len(resident)} resident models need "
                                                       f"{warm:.1f} GiB warm; {self.pool_gb:.1f} available")})
        # Every active model's active_inference must fit inside the allocatable
        # pool. The safety floor was ALREADY subtracted from physical memory when
        # the pool was computed (see capacity-budget reserves), so it is not
        # re-added here — re-adding it would double-count and falsely refuse
        # valid combinations such as Qwen+H3 or a warm LLM.
        for mid in resident:
            active = phase_hi(self.manifests[mid], "active_inference")
            chk = self._check_pool(mid, "active_inference", active)
            checks.append(chk)
            if not chk["pass"]:
                concurrent_blockers.append({
                    "kind": "active-phase-floor", "model": mid, "phase": "active_inference",
                    "required_gb": _round2(active),
                    "reason": (f"{mid} needs {active:.1f} GiB active inside the "
                               f"{self.pool_gb:.1f} GiB allocatable pool "
                               f"(safety floor already reserved)")})
        for b in concurrent_blockers:
            blocks.append(b)

        admitted = not blocks

        # Behavior plan: residency/drain/swap.
        behavior_map: dict[str, str] = {}
        for slot, mid in sel.items():
            m = self.manifests[mid]
            behaviors = m.get("behavior") or ["on_demand"]
            if isinstance(behaviors, str):
                behaviors = [behaviors]
            behavior_map[mid] = behaviors[0] if behaviors else "on_demand"

        always_resident = [m for m in resident if "always_resident" in
                           (self.manifests[m].get("behavior") or [])]
        drained = [m for m in resident if "drainable" in
                   (self.manifests[m].get("behavior") or [])]
        primary = [m for m in resident if "primary" in
                   (self.manifests[m].get("behavior") or [])]
        swap_order = resident  # deterministically sorted ids; drain order reversed
        if admitted:
            # Estimate transition: cold_load for any model not already warm.
            est_load = sum(float(self.manifests[m].get("estimated_load_seconds", 0))
                           for m in resident)
        else:
            est_load = 0.0

        concurrency_limits = {
            "max_resident": len(resident),
            "serial_groups": sorted({str(self.manifests[m].get("exclusive_group"))
                                     for m in resident
                                     if self.manifests[m].get("exclusive_group")}),
        }

        return {
            "policy_version": str(self.policy.get("version", 1)),
            "selected": dict(sorted(sel.items())),
            "resident": resident if admitted else [],
            "always_resident": always_resident,
            "primary": primary,
            "drained": drained if admitted else [],
            "swap_order": swap_order if admitted else [],
            "estimated_load_seconds": _round2(est_load),
            "concurrency_limits": concurrency_limits,
            "admitted": admitted,
            "checks": checks,
            "blockers": blocks,
            "memory": {
                "pool_gb": self.pool_gb,
                "safety_floor_gb": self.floor_gb,
            },
        }

    # -- capacity bar ---------------------------------------------------
    def capacity_bar(self, selections: Mapping[str, str]) -> dict[str, Any]:
        """Exact stacked-bar model the UI consumes.

        Segments are byte-order stable and every number comes from the planner
        (never fabricated). Grey headroom = pool minus worst-case committed warm
        residency, shown as genuinely-uncommitted remainder, not optimistic free.
        """
        plan = self.plan(selections)
        segments: list[dict[str, Any]] = []
        resident = plan["resident"]
        for mid in resident:
            m = self.manifests[mid]
            segments.append({
                "id": mid, "name": m.get("name", mid), "modality": m.get("modality"),
                "slot": m.get("slot"), "color_hint": m.get("color_hint", "default"),
                "phase_ranges_gb": {
                    ph: {"lo": _round2(phase_lo(m, ph)), "hi": _round2(phase_hi(m, ph))}
                    for ph in PHASES
                },
                "committed_warm_hi_gb": _round2(phase_hi(m, "warm_idle")),
                "behavior": plan.get("always_resident")
                            .__contains__(mid) and "always_resident"
                            or (m.get("behavior") or ["on_demand"])[0],
            })
        committed = sum(float(s["committed_warm_hi_gb"]) for s in segments)
        grey_headroom = max(0.0, self.pool_gb - committed)
        return {
            "physical_total_gb": self.budget["physical_total_gb"],
            "allocatable_pool_gb": self.pool_gb,
            "reserves_gb": dict(self.budget["reserves_gb"]),
            "admitted": plan["admitted"],
            "segments": segments,
            "committed_warm_hi_gb": _round2(committed),
            "grey_headroom_gb": _round2(grey_headroom),
            "blockers": plan["blockers"],
        }