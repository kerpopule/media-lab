"""Real / Long is priced from its own measured row, not Sol-H3's whole-box row.

2026-09-26 22:57: a Real / Long take was refused with "h3 decode requires
117.0 GiB including the 2.0 GiB floor; 116.7 GiB would be available": the box
policy prices the h3 slot as Sol-H3 (decode 115 GiB), while Real / Long peaks
at 97 GiB (config/gpu-capacity-receipts.json)."""
import json
from pathlib import Path

from residency import plan_residency

ROOT = Path(__file__).resolve().parents[1]
POLICY = json.loads((ROOT / "config/model-residency-policy.json").read_text())


def actual(available):
    return {"models": {m: {"resident": m == "qwen", "healthy": m == "qwen", "busy": False}
                       for m in POLICY["models"]},
            "memory": {"available_gb": available}, "pool_lease": {}, "inference": {"locked": False}}


def test_sol_row_refuses_at_116_7_but_the_real_long_row_admits():
    import app
    sol = plan_residency(POLICY, actual(116.7), "qwen-h3")
    assert not sol["admitted"]
    assert any("h3 decode requires 117.0 GiB" in b["reason"] for b in sol["blockers"])
    phases = app.singularity_residency_phases()
    assert phases["decode"] == 97.0 and phases["warm_idle"] == 81.0
    real = plan_residency(POLICY, actual(116.7), "qwen-h3", phase_overrides={"h3": phases})
    assert real["admitted"], real["blockers"]
    assert POLICY["models"]["h3"]["phases_gb"]["decode"] == 115   # the policy itself is untouched


def test_real_long_row_still_refuses_when_memory_is_short():
    import app
    real = plan_residency(POLICY, actual(90.0), "qwen-h3",
                          phase_overrides={"h3": app.singularity_residency_phases()})
    assert not real["admitted"]
    assert any("requires 99.0 GiB" in b["reason"] for b in real["blockers"])


def test_ensure_video_residency_prices_only_real_long_jobs(monkeypatch):
    import app
    seen = []

    class R:
        def desired(self): return {"name": "qwen-h3", "models": ["qwen", "h3"]}
        def snapshot(self): return {"models": {"h3": {"resident": True, "healthy": True}}}
        def apply(self, target, slots, commit_desired=True, phase_overrides=None):
            seen.append(phase_overrides)

    monkeypatch.setattr(app, "RESIDENCY", R())
    monkeypatch.setattr(app, "preferred_text_runtime", lambda: "pplx")
    monkeypatch.setattr(app, "save_state", lambda: None)
    monkeypatch.setattr(app, "h3_variant_warm", lambda *a, **k: True)
    monkeypatch.setattr(app._h3ref, "ROUTE_REFERENCES_TO_SINGULARITY", True)
    assert app.ensure_video_residency("h3", {"request": {"h3_engine": "singularity"}}) == "up"
    assert app.ensure_video_residency("h3", {"request": {"prompt": "x"}}) == "up"
    assert seen[0]["h3"]["decode"] == 97.0 and seen[1] is None
