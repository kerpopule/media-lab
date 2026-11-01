"""Governed model-residency policy, planning, receipts, and recovery.

This module has no FastAPI or process side effects.  The private app supplies a
small runtime hook object for exact engine/container operations; tests use a
fake hook.  The pool ownership lease is intentionally observational here.  It
must never be reused as the inference transaction lock.
"""
from __future__ import annotations

import json
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Mapping


SLOT_NAMES = ("text_primary", "video_primary", "video_secondary")
VIDEO_MODELS = {"ltx", "h3"}


class ResidencyError(RuntimeError):
    """A fail-closed policy, admission, transaction, or recovery error."""


class ResidencyRefused(ResidencyError):
    """The planner refused before the transaction did anything.

    Raised only for a plan that was not admitted, before any receipt, lock or
    runtime hook: nothing was drained, evicted or loaded. ``memory_only`` is
    true when every blocker is a measured phase-floor (memory) shortfall."""

    def __init__(self, message: str, blockers: list[dict[str, Any]]):
        super().__init__(message)
        self.blockers = list(blockers)
        self.memory_only = bool(self.blockers) and all(
            b.get("kind") == "phase-floor" for b in self.blockers)


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
    tmp.write_text(json.dumps(value, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def load_policy(path: Path) -> dict[str, Any]:
    try:
        policy = json.loads(path.read_text())
    except Exception as exc:
        raise ResidencyError(f"residency policy unreadable: {exc}") from exc
    if policy.get("version") != 1:
        raise ResidencyError(f"unsupported residency policy version: {policy.get('version')!r}")
    if float(policy.get("operational_floor_gb") or 0) <= 0:
        raise ResidencyError("operational_floor_gb must be positive")
    models = policy.get("models") or {}
    if set(models) != {"qwen", "ltx", "h3"}:
        raise ResidencyError("policy must declare exactly qwen, ltx, and h3")
    for model, spec in models.items():
        phases = spec.get("phases_gb") or {}
        if "warm_idle" not in phases or not phases or any(float(v) <= 0 for v in phases.values()):
            raise ResidencyError(f"{model} has an invalid phase budget")
    profiles = policy.get("profiles") or {}
    required = {"qwen-ltx-default", "qwen-h3", "dual-video-ltx-h3", "qwen-only", "custom"}
    if set(profiles) != required:
        raise ResidencyError(f"policy profiles must be exactly {sorted(required)}")
    if policy.get("default_profile") != "qwen-ltx-default":
        raise ResidencyError("qwen-ltx-default must remain the default profile")
    for name, slots in profiles.items():
        if name != "custom":
            validate_slots(policy, slots)
    return policy


def validate_slots(policy: Mapping[str, Any], slots: Mapping[str, Any]) -> dict[str, Any]:
    unknown = set(slots) - set(SLOT_NAMES) - {"constraints"}
    if unknown:
        raise ResidencyError(f"unknown custom slots: {sorted(unknown)}")
    out = {name: slots.get(name) for name in SLOT_NAMES}
    if out["text_primary"] not in (None, "qwen"):
        raise ResidencyError("text_primary must be qwen or null")
    for name in ("video_primary", "video_secondary"):
        if out[name] not in VIDEO_MODELS | {None}:
            raise ResidencyError(f"{name} must be ltx, h3, or null")
    if out["video_secondary"] and not out["video_primary"]:
        raise ResidencyError("video_secondary requires video_primary")
    if out["video_primary"] and out["video_primary"] == out["video_secondary"]:
        raise ResidencyError("video slots cannot contain the same model twice")
    if not any(out.values()):
        raise ResidencyError("a custom profile cannot evict every managed model")
    return out


def resolve_profile(policy: Mapping[str, Any], profile: str,
                    custom_slots: Mapping[str, Any] | None = None) -> dict[str, Any]:
    if profile not in policy["profiles"]:
        raise ResidencyError(f"unknown residency profile: {profile}")
    if profile == "custom":
        if custom_slots is None:
            raise ResidencyError("custom profile requires explicit slots")
        slots = validate_slots(policy, custom_slots)
    else:
        if custom_slots is not None:
            raise ResidencyError("custom slots are only accepted for the custom profile")
        slots = validate_slots(policy, policy["profiles"][profile])
    models = [slots[name] for name in SLOT_NAMES if slots.get(name)]
    return {"name": profile, "slots": slots, "models": models}


def _model_state(actual: Mapping[str, Any], model: str) -> Mapping[str, Any]:
    return (actual.get("models") or {}).get(model) or {}


def plan_residency(policy: Mapping[str, Any], actual: Mapping[str, Any], profile: str,
                   custom_slots: Mapping[str, Any] | None = None,
                   phase_overrides: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """Plan a residency change. ``phase_overrides`` prices one transaction's load
    of a model with another measured phase row (Real / Long is a variant of the
    ``h3`` slot with its own, smaller envelope); the policy itself is unchanged."""
    if phase_overrides:
        policy = dict(policy)
        policy["models"] = {m: (dict(row, phases_gb=dict(phase_overrides[m])) if m in phase_overrides else row)
                            for m, row in policy["models"].items()}
    desired = resolve_profile(policy, profile, custom_slots)
    desired_set = set(desired["models"])
    actual_set = {m for m in policy["models"] if _model_state(actual, m).get("resident")}
    healthy_set = {m for m in actual_set if _model_state(actual, m).get("healthy")}
    # A resident-but-unhealthy required video model is not retained.  Qwen is
    # different: every Qwen-retaining profile is forbidden from evicting the
    # resident text runtime, even when its health probe is degraded.  The
    # controller's health-check then reports the degraded state instead of
    # making a destructive stop/load attempt.
    qwen_retaining = "qwen" in desired_set
    replace = desired_set & (actual_set - healthy_set)
    if qwen_retaining:
        replace.discard("qwen")
    retain_set = desired_set & healthy_set
    if qwen_retaining and "qwen" in actual_set:
        retain_set.add("qwen")
    retain = sorted(retain_set)
    evict = sorted((actual_set - desired_set) | replace)
    load = [m for m in desired["models"]
            if m not in healthy_set and not
            (m == "qwen" and qwen_retaining and m in actual_set)]
    blockers: list[dict[str, Any]] = []

    for model in evict:
        if _model_state(actual, model).get("busy"):
            blockers.append({"kind": "busy-engine", "model": model,
                             "reason": f"{model} is busy and cannot be evicted"})
    if (evict or load) and (actual.get("inference") or {}).get("locked"):
        blockers.append({"kind": "inference-transaction-active",
                         "reason": "the inference transaction lock is active; residency will not mutate"})

    # MemAvailable already excludes current residents. Count only memory returned
    # by exact planned evictions, then reserve each loaded model's warm footprint
    # before admitting the next. Phase budgets are measured incremental floors.
    available = float((actual.get("memory") or {}).get("available_gb") or 0)
    models = policy["models"]
    available += sum(float(models[m]["phases_gb"]["warm_idle"]) for m in evict)
    floor = float(policy["operational_floor_gb"])
    phase_checks: list[dict[str, Any]] = []
    for model in load:
        phases = models[model]["phases_gb"]
        for phase, budget in phases.items():
            if phase == "warm_idle":
                continue
            need = float(budget) + floor
            passed = available >= need
            check = {"model": model, "phase": phase, "available_gb": round(available, 2),
                     "budget_gb": float(budget), "floor_gb": floor,
                     "required_gb": need, "pass": passed}
            phase_checks.append(check)
            if not passed:
                blockers.append({"kind": "phase-floor", "model": model, "phase": phase,
                                 "reason": (f"{model} {phase} requires {need:.1f} GiB "
                                            f"including the {floor:.1f} GiB floor; "
                                            f"{available:.1f} GiB would be available")})
        available -= float(phases["warm_idle"])

    if ("qwen" in actual_set and "qwen" not in desired_set and
            profile != "dual-video-ltx-h3"):
        blockers.append({"kind": "silent-qwen-eviction", "model": "qwen",
                         "reason": "Qwen eviction is allowed only by dual-video-ltx-h3"})

    actions: list[dict[str, Any]] = []
    actions.extend({"action": "retain", "model": m} for m in retain)
    if any(m in VIDEO_MODELS for m in load):
        actions.append({"action": "release-image-weights", "reason": "video residency admission"})
    actions.extend({"action": "drain", "model": m} for m in evict)
    actions.extend({"action": "evict", "model": m,
                    "intentional": m != "qwen" or profile == "dual-video-ltx-h3"}
                   for m in evict)
    actions.extend({"action": "load", "model": m} for m in load)
    actions.extend({"action": "health-check", "model": m} for m in desired["models"])
    actions.append({"action": "commit", "profile": profile})
    return {
        "policy_version": policy["version"], "profile": desired, "actual_models": sorted(actual_set),
        "retain": retain, "evict": evict, "load": load, "actions": actions,
        "memory": {"available_after_planned_evictions_gb": round(
            float((actual.get("memory") or {}).get("available_gb") or 0) +
            sum(float(models[m]["phases_gb"]["warm_idle"]) for m in evict), 2),
            "operational_floor_gb": floor, "phase_checks": phase_checks},
        "admitted": not blockers, "blockers": blockers,
    }


class ResidencyController:
    """Serialize plans into exact, crash-visible transactions through runtime hooks."""

    def __init__(self, policy_path: Path, state_dir: Path, hooks: Any):
        self.policy_path = Path(policy_path)
        self.state_dir = Path(state_dir)
        self.hooks = hooks
        self._lock = threading.Lock()
        self.policy = load_policy(self.policy_path)
        self.desired_path = self.state_dir / "desired.json"
        self.active_path = self.state_dir / "active-transaction.json"
        self.receipts_dir = self.state_dir / "receipts"
        self.state_dir.mkdir(parents=True, exist_ok=True)

    def desired(self) -> dict[str, Any]:
        try:
            data = json.loads(self.desired_path.read_text())
            return resolve_profile(self.policy, data["profile"], data.get("slots"))
        except FileNotFoundError:
            return resolve_profile(self.policy, self.policy["default_profile"])
        except ResidencyError:
            raise
        except Exception as exc:
            raise ResidencyError(f"desired residency state unreadable: {exc}") from exc

    def snapshot(self) -> dict[str, Any]:
        return self.hooks.snapshot()

    def profiles(self) -> dict[str, Any]:
        return {"policy_version": self.policy["version"],
                "default_profile": self.policy["default_profile"],
                "operational_floor_gb": self.policy["operational_floor_gb"],
                "profiles": self.policy["profiles"], "models": self.policy["models"]}

    def plan(self, profile: str, slots: Mapping[str, Any] | None = None,
             phase_overrides: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, Any]:
        return plan_residency(self.policy, self.snapshot(), profile, slots, phase_overrides)

    def state(self) -> dict[str, Any]:
        actual = self.snapshot()
        desired = self.desired()
        active = None
        try:
            active = json.loads(self.active_path.read_text())
        except Exception:
            pass
        receipts = sorted(self.receipts_dir.glob("*.json"), reverse=True)
        last = None
        if receipts:
            try:
                last = json.loads(receipts[0].read_text())
            except Exception:
                pass
        required = set(desired["models"])
        models = actual.get("models") or {}
        degraded = [m for m in required if not models.get(m, {}).get("resident") or
                    not models.get(m, {}).get("healthy")]
        return {"policy_version": self.policy["version"], "desired": desired, "actual": actual,
                "active_transaction": active, "last_receipt": last,
                "status": "degraded" if degraded else "healthy", "degraded_models": sorted(degraded)}

    def _save_receipt(self, receipt: dict[str, Any], final: bool = False) -> None:
        _atomic_json(self.active_path, receipt)
        if final:
            _atomic_json(self.receipts_dir / f"{receipt['started_at']}-{receipt['id']}.json", receipt)

    def apply(self, profile: str, slots: Mapping[str, Any] | None = None,
              *, commit_desired: bool = True,
              phase_overrides: Mapping[str, Mapping[str, Any]] | None = None) -> dict[str, Any]:
        if not self._lock.acquire(blocking=False):
            raise ResidencyError("another residency transaction is active")
        inference_token = None
        try:
            if self.active_path.exists():
                old = json.loads(self.active_path.read_text())
                if old.get("status") not in ("committed", "rolled-back", "failed"):
                    raise ResidencyError(f"unfinished residency transaction {old.get('id')} requires recovery")
            actual = self.snapshot()
            plan = plan_residency(self.policy, actual, profile, slots, phase_overrides)
            if not plan["admitted"]:
                raise ResidencyRefused("; ".join(b["reason"] for b in plan["blockers"]),
                                       plan["blockers"])
            # The pool lease establishes ownership; this separate non-blocking
            # transaction claim closes the planner-to-mutation race with live
            # text, image, or video inference.  Runtime hooks may omit it for
            # offline/fake use, but the production hook implements it with the
            # shared media-lab-inference.lock.
            begin = getattr(self.hooks, "begin_residency_transaction", None)
            if begin is not None:
                inference_token = begin()
                if inference_token is None:
                    raise ResidencyError("could not claim the inference transaction lock")
            receipt: dict[str, Any] = {
                "version": 1, "id": uuid.uuid4().hex, "started_at": int(time.time()),
                "status": "applying", "requested": {"profile": profile, "slots": slots,
                                                        "commit_desired": commit_desired},
                "pre_state": actual, "plan": plan, "completed": [], "rollback": [],
            }
            self._save_receipt(receipt)
            try:
                for step in plan["actions"]:
                    action, model = step["action"], step.get("model")
                    detail: Any = None
                    if action in ("retain", "commit"):
                        pass
                    elif action == "drain":
                        drain = getattr(self.hooks, "drain_model", None)
                        if drain is None:
                            raise ResidencyError(
                                f"{model} requires a bounded drain hook before eviction")
                        detail = drain(model)
                        if not isinstance(detail, Mapping) or detail.get("state") != "idle":
                            raise ResidencyError(
                                f"{model} drain did not prove running=0 and waiting=0")
                    elif action == "release-image-weights":
                        detail = self.hooks.release_image_weights(step["reason"])
                        if detail is None:
                            raise ResidencyError("image engine would not release weights")
                    elif action == "evict":
                        detail = self.hooks.stop_model(model)
                        receipt["rollback"].insert(0, {"action": "load", "model": model,
                                                       "detail": detail})
                    elif action == "load":
                        detail = self.hooks.start_model(model, None)
                        if not detail:
                            raise ResidencyError(f"{model} failed to load")
                        receipt["rollback"].insert(0, {"action": "evict", "model": model})
                    elif action == "health-check":
                        if not self.hooks.model_healthy(model):
                            raise ResidencyError(f"{model} did not become healthy")
                    receipt["completed"].append({**step, "detail": detail, "at": int(time.time())})
                    self._save_receipt(receipt)
                if commit_desired:
                    desired = resolve_profile(self.policy, profile, slots)
                    _atomic_json(self.desired_path, {"profile": profile,
                                                     "slots": desired["slots"] if profile == "custom" else None,
                                                     "committed_at": int(time.time()),
                                                     "receipt_id": receipt["id"]})
                receipt["status"] = "committed"
                receipt["finished_at"] = int(time.time())
                self._save_receipt(receipt, final=True)
                self.active_path.unlink(missing_ok=True)
                return receipt
            except Exception as exc:
                receipt["error"] = str(exc)
                self._save_receipt(receipt)
                self._rollback(receipt)
                raise ResidencyError(str(exc)) from exc
        finally:
            if inference_token is not None:
                end = getattr(self.hooks, "end_residency_transaction", None)
                if end is not None:
                    end(inference_token)
            self._lock.release()

    def _rollback(self, receipt: dict[str, Any]) -> dict[str, Any]:
        receipt["status"] = "rolling-back"
        receipt["rollback_completed"] = []
        self._save_receipt(receipt)
        errors = []
        for step in receipt.get("rollback", []):
            try:
                if step["action"] == "evict":
                    self.hooks.stop_model(step["model"])
                else:
                    if not self.hooks.start_model(step["model"], step.get("detail")):
                        raise ResidencyError(f"could not restore {step['model']}")
                receipt["rollback_completed"].append({**step, "at": int(time.time())})
            except Exception as exc:
                errors.append(f"{step.get('model')}: {exc}")
            self._save_receipt(receipt)
        receipt["status"] = "failed" if errors else "rolled-back"
        receipt["rollback_errors"] = errors
        receipt["finished_at"] = int(time.time())
        self._save_receipt(receipt, final=True)
        if not errors:
            self.active_path.unlink(missing_ok=True)
        return receipt

    def recover(self) -> dict[str, Any]:
        """Rollback an interrupted transaction; never claim health from process liveness."""
        if not self.active_path.exists():
            return {"status": "clean"}
        if not self._lock.acquire(blocking=False):
            return {"status": "recovery-pending",
                    "error": "another residency transaction is active"}
        inference_token = None
        try:
            receipt = json.loads(self.active_path.read_text())
            if receipt.get("status") in ("committed", "rolled-back", "failed"):
                return receipt
            # Recovery mutates the same model processes as apply(). It must own
            # the same cross-process inference transaction first. If another
            # process is rendering or serving, preserve the active receipt and
            # retry later instead of stopping anything underneath live work.
            begin = getattr(self.hooks, "begin_residency_transaction", None)
            try:
                if begin is not None:
                    inference_token = begin()
                    if inference_token is None:
                        raise ResidencyError("could not claim the inference transaction lock")
            except Exception as exc:
                receipt["status"] = "recovery-pending"
                receipt["recovery_error"] = str(exc)
                receipt["recovery_checked_at"] = int(time.time())
                self._save_receipt(receipt)
                return receipt
            return self._rollback(receipt)
        finally:
            if inference_token is not None:
                end = getattr(self.hooks, "end_residency_transaction", None)
                if end is not None:
                    end(inference_token)
            self._lock.release()
