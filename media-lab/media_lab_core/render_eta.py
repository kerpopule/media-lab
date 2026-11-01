"""How long a take will take, per engine, before the user presses Make.

One table (config/render-eta.json) holds the measured numbers: render time
with the engine already loaded, by clip length, plus the spin-up a take pays
when its engine is not resident (evicting the resident engine and the cold
load). The page asks /api/engines/eta with the chosen length and references
and the studio's live residency, and shows for example
"about 12 min - includes ~7 min to load Real / Long".

Completed jobs keep the table honest: record() appends each finished take's
measured wall time and spin-up to pool/render-timings.json, and estimate()
scales the table by the median ratio of real to predicted over the recent
takes of that engine (from 3 takes; clamped to 0.5-2x so one odd take cannot
run away with it). Stdlib only.
"""
from __future__ import annotations

import json
import math
import statistics
import threading
import time
from pathlib import Path

TABLE_FILE = Path(__file__).resolve().parents[1] / "config" / "render-eta.json"
KEEP_PER_ENGINE = 24
MIN_SAMPLES = 3
RATIO_BOUNDS = (0.5, 2.0)

_lock = threading.Lock()


def load_table(path: Path | None = None) -> dict:
    try:
        return json.loads(Path(path or TABLE_FILE).read_text())
    except (OSError, ValueError):
        return {"engines": {}}


def _interp(points, seconds: float) -> float:
    """Piecewise-linear through measured (seconds, render_s) points; beyond the
    ends it extends the nearest segment's slope (never below the first point's
    rate)."""
    pts = sorted((float(a), float(b)) for a, b in points)
    if not pts:
        return 0.0
    if len(pts) == 1:
        s0, t0 = pts[0]
        return t0 if s0 <= 0 or seconds <= 0 else t0 * seconds / s0
    if seconds <= pts[0][0]:
        (s0, t0), (s1, t1) = pts[0], pts[1]
        return max(t0 * 0.5, t0 + (t1 - t0) / (s1 - s0) * (seconds - s0))
    for (s0, t0), (s1, t1) in zip(pts, pts[1:]):
        if seconds <= s1:
            return t0 + (t1 - t0) * (seconds - s0) / (s1 - s0)
    (s0, t0), (s1, t1) = pts[-2], pts[-1]
    return t1 + (t1 - t0) / (s1 - s0) * (seconds - s1)


def clip_seconds(row: dict, seconds) -> float:
    try:
        seconds = float(seconds)
    except (TypeError, ValueError):
        seconds = 5.0
    if not math.isfinite(seconds) or seconds <= 0:
        seconds = 5.0
    if row.get("fixed_seconds"):
        return float(row["fixed_seconds"])
    if row.get("max_seconds"):
        seconds = min(seconds, float(row["max_seconds"]))
    return seconds


def table_render_s(row: dict, seconds: float, image_refs: int = 0, video_refs: int = 0,
                   audio_refs: int = 0, detail: str = "match") -> float:
    base = _interp(row.get("render_points") or [], seconds)
    # measured: +~100 s per max-detail picture at 5 s and at 15 s alike (most
    # of it is encoding the picture), so it does not scale with length
    per_image = (row.get("per_image_ref_s") or {}).get(detail if detail in ("max", "match") else "match", 0)
    extra = float(per_image) * image_refs
    extra += base * float(row.get("per_video_ref_fraction") or 0) * video_refs
    extra += float(row.get("per_audio_ref_s") or 0) * audio_refs
    return base + extra


# ------------------------------------------------------------ measured takes
def _read_timings(path: Path) -> dict:
    try:
        data = json.loads(Path(path).read_text())
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def record(path: Path, engine: str, *, seconds: float, total_s: float, spinup_s: float = 0.0,
           image_refs: int = 0, video_refs: int = 0, audio_refs: int = 0, detail: str = "match",
           table: dict | None = None, now: float | None = None) -> dict | None:
    """Append one finished take. Returns the stored row (None if unusable)."""
    try:
        total_s = float(total_s); spinup_s = max(0.0, float(spinup_s or 0.0))
    except (TypeError, ValueError):
        return None
    if not math.isfinite(total_s) or total_s <= 0 or spinup_s >= total_s:
        return None
    row = (table or load_table()).get("engines", {}).get(engine)
    if not row:
        return None
    secs = clip_seconds(row, seconds)
    predicted_render = table_render_s(row, secs, image_refs, video_refs, audio_refs, detail)
    sample = {"t": round(now or time.time(), 1), "seconds": round(secs, 2),
              "render_s": round(total_s - spinup_s, 1), "spinup_s": round(spinup_s, 1),
              "predicted_render_s": round(predicted_render, 1),
              "image_refs": int(image_refs), "video_refs": int(video_refs),
              "audio_refs": int(audio_refs), "detail": detail}
    if spinup_s > 0 and row.get("spinup_s"):
        sample["predicted_spinup_s"] = float(row["spinup_s"])
    with _lock:
        data = _read_timings(path)
        rows = (data.get(engine) or []) + [sample]
        data[engine] = rows[-KEEP_PER_ENGINE:]
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(f".{path.name}.tmp")
        tmp.write_text(json.dumps(data, indent=1))
        tmp.replace(path)
    return sample


def _ratio(samples, key_actual, key_predicted):
    ratios = [s[key_actual] / s[key_predicted] for s in samples
              if s.get(key_predicted) and s.get(key_actual) and s[key_predicted] > 0]
    if len(ratios) < MIN_SAMPLES:
        return 1.0, len(ratios)
    lo, hi = RATIO_BOUNDS
    return min(hi, max(lo, statistics.median(ratios))), len(ratios)


# ----------------------------------------------------------------- estimate
def estimate(engine: str, seconds=5.0, *, resident: bool, image_refs: int = 0,
             video_refs: int = 0, audio_refs: int = 0, detail: str = "match",
             table: dict | None = None, timings_path: Path | None = None) -> dict | None:
    """Estimated wall time for one take. None for an engine the table lacks."""
    table = table or load_table()
    row = (table.get("engines") or {}).get(engine)
    if not row:
        return None
    secs = clip_seconds(row, seconds)
    render = table_render_s(row, secs, image_refs, video_refs, audio_refs, detail)
    samples = (_read_timings(timings_path).get(engine) or []) if timings_path else []
    r_ratio, r_n = _ratio(samples, "render_s", "predicted_render_s")
    s_ratio, s_n = _ratio([s for s in samples if s.get("spinup_s")], "spinup_s", "predicted_spinup_s")
    render *= r_ratio
    spinup = 0.0 if resident else float(row.get("spinup_s") or 0) * s_ratio
    total = render + spinup
    return {
        "engine": engine, "label": row.get("label") or engine,
        "clip_seconds": round(secs, 2),
        "render_s": int(round(render)), "spinup_s": int(round(spinup)),
        "total_s": int(round(total)), "resident": bool(resident),
        "fixed_length": bool(row.get("fixed_seconds")),
        "max_seconds": row.get("max_seconds"),
        "basis": ("measured + %d recent takes" % r_n) if r_n >= MIN_SAMPLES else "measured",
        "text": describe(total, spinup, row.get("label") or engine),
    }


def human(seconds: float) -> str:
    seconds = max(0, int(round(seconds)))
    if seconds < 90:
        return f"{max(10, int(math.floor(seconds / 10.0 + 0.5)) * 10)} s"
    minutes = seconds / 60.0
    if minutes < 10:
        whole = int(minutes); half = minutes - whole >= 0.25 and minutes - whole < 0.75
        return f"{whole}½ min" if half else f"{int(round(minutes))} min"
    return f"{int(round(minutes))} min"


def describe(total_s: float, spinup_s: float, label: str) -> str:
    text = f"about {human(total_s)}"
    if spinup_s >= 30:
        short = label.split(" (")[0]
        text += f" · includes ~{human(spinup_s)} to load {short}"
    return text
