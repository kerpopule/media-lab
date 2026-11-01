"""The critic pass: look at every seam of an assembled cut before anyone else does.

Two halves:

* **Measured** (always): at each cut, the last frame before and the first
  frame after — colour difference (CIE76 on the frame means), brightness
  jump, how alike the two framings are (a near-identical framing across a
  hard cut is a jump cut), the sound level either side of the audio edit and
  any click at it. Across the film: loudness, true peak, frozen or black
  stretches, and the pacing (shot lengths).
* **Looked at** (when a vision model answers): the before/after pair goes to
  the studio's own multimodal model with the continuity bible, which answers
  in JSON — same people? same clothes? same light? screen direction? anyone
  looking into the lens? garbled lettering, melted faces, extra fingers? —
  and a verdict: ok, fix colour, or re-render the shot on either side.

``review`` returns a report with a verdict per seam and the list of shots to
re-render with the reason, which the director feeds back into the next take
(``rerender_patch``). Nothing is delivered with a re-render verdict open
unless a person overrides it.
"""
from __future__ import annotations

import base64
import io
import json
import math
import os
import re
import subprocess
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from pathlib import Path
from typing import Any

from . import stitch

np = stitch.np

REPORT_SCHEMA = "media_lab.seam_critic.v1"

COLOR_DE_WARN = 14.0          # mean-frame CIE76 difference that reads as a jump
LUMA_JUMP_WARN = 22.0         # 0-255 mean-luma jump at a cut
JUMP_CUT_SIMILARITY = 0.90    # grayscale correlation across a HARD cut
AUDIO_JUMP_WARN_DB = 9.0
CLICK_CREST_DB = 20.0
SILENCE_DB = -55.0

VISION_SYSTEM = """You are the continuity supervisor on a film. You check one cut at a time.
The image has two frames side by side: LEFT is the last frame before the cut, RIGHT is the first frame after it.
Judge them against the continuity bible you are given. Reply with ONLY a JSON object:
{"people_consistent": true|false|null, "wardrobe_consistent": true|false|null,
 "light_and_colour_consistent": true|false, "set_consistent": true|false|null,
 "screen_direction_ok": true|false|null, "jump_cut": true|false,
 "left_problems": [], "right_problems": [],
 "verdict": "ok"|"fix_colour"|"rerender_left"|"rerender_right",
 "reason": "one short sentence"}
Use null when the question does not apply (for example nobody is in one of the frames).
Problems use these words when they apply: "different person", "different clothes", "different set",
"looks into the camera", "garbled lettering", "melted face", "extra or missing fingers", "panels or collage",
"blurry", "frozen", "wrong number of people".
A cut between two different shot sizes of the same scene is normal; do not call it inconsistent.
Say "rerender_left" or "rerender_right" only for a problem a viewer would notice. /no_think"""


# ----------------------------------------------------------- measurements

def _srgb_to_lab(rgb: Sequence[float]) -> tuple[float, float, float]:
    def lin(c):
        c = c / 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (lin(v) for v in rgb)
    x = (0.4124 * r + 0.3576 * g + 0.1805 * b) / 0.95047
    y = (0.2126 * r + 0.7152 * g + 0.0722 * b)
    z = (0.0193 * r + 0.1192 * g + 0.9505 * b) / 1.08883

    def f(t):
        return t ** (1 / 3) if t > 0.008856 else 7.787 * t + 16 / 116
    fx, fy, fz = f(x), f(y), f(z)
    return 116 * fy - 16, 500 * (fx - fy), 200 * (fy - fz)


def delta_e(rgb_a: Sequence[float], rgb_b: Sequence[float]) -> float:
    la, lb = _srgb_to_lab(rgb_a), _srgb_to_lab(rgb_b)
    return math.sqrt(sum((p - q) ** 2 for p, q in zip(la, lb)))


def _frame_at(path: str | Path, t: float, width: int = 96, height: int = 54):
    frames = stitch.rgb_samples(path, [t], width=width, height=height)
    return frames[0] if frames else None


def _similarity(a, b) -> float | None:
    if np is None or a is None or b is None:
        return None
    ga = a.mean(axis=2).ravel()
    gb = b.mean(axis=2).ravel()
    ga = ga - ga.mean()
    gb = gb - gb.mean()
    denom = float(np.sqrt((ga * ga).sum() * (gb * gb).sum()))
    if denom <= 1e-6:
        return None
    return round(float((ga * gb).sum() / denom), 4)


def _save_jpeg(path: str | Path, t: float, out: Path, width: int = 640) -> Path | None:
    result = stitch._run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", f"{max(0.0, t):.6f}",
                          "-i", str(path), "-frames:v", "1", "-vf", f"scale={width}:-2",
                          "-q:v", "3", str(out)], timeout=60)
    return out if result.returncode == 0 and out.is_file() else None


def _pair_image(left: Path, right: Path, out: Path, label: str) -> Path | None:
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return None
    a, b = Image.open(left).convert("RGB"), Image.open(right).convert("RGB")
    h = max(a.height, b.height)
    canvas = Image.new("RGB", (a.width + b.width + 12, h + 28), (18, 18, 18))
    canvas.paste(a, (0, 28))
    canvas.paste(b, (a.width + 12, 28))
    draw = ImageDraw.Draw(canvas)
    draw.text((8, 7), f"LEFT: before the cut   |   RIGHT: after the cut   ({label})", fill=(235, 235, 235))
    canvas.save(out, quality=88)
    return out


def _audio_around(env_t, env_db, t: float, before: bool, span: float = 0.25) -> float | None:
    if env_t is None or np is None:
        return None
    if before:
        mask = (env_t >= t - span) & (env_t < t - 0.02)
    else:
        mask = (env_t > t + 0.02) & (env_t <= t + span)
    if not mask.any():
        return None
    # power mean in dB
    power = np.mean(10 ** (env_db[mask] / 10))
    return round(float(10 * np.log10(power + 1e-12)), 2)


def _click_at(pcm, rate: int, t: float) -> float | None:
    if pcm is None:
        return None
    i = int(t * rate)
    near = pcm[max(0, i - int(0.008 * rate)): i + int(0.008 * rate)]
    wide = pcm[max(0, i - int(0.15 * rate)): i + int(0.15 * rate)]
    if len(near) == 0 or len(wide) == 0:
        return None
    peak = float(np.max(np.abs(near)))
    rms = float(np.sqrt(np.mean(wide * wide)) + 1e-9)
    if peak < 10 ** (-30 / 20):
        return 0.0
    return round(20 * math.log10(peak / rms), 2)


def seam_points(receipt: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Where to look, from a stitch receipt: picture cut and audio edit per seam."""
    plan = receipt.get("plan") or receipt
    fps = plan["fps"]
    shots = plan["shots"]
    points = []
    for k in range(1, len(shots)):
        s = shots[k]
        trans = s["transition_in"]
        start = s["timeline_start"]
        overlap = trans.get("frames", 0) / fps
        audio = s.get("audio") or {}
        points.append({
            "seam": k, "from_shot": k, "to_shot": k + 1, "kind": trans["kind"],
            # ffmpeg's accurate seek returns the first frame at or after t, so
            # half a frame before a frame boundary lands exactly on that frame
            "before_t": max(0.0, start - 1.5 / fps) if not overlap else max(0.0, start - 0.5 / fps),
            "after_t": max(0.0, start + overlap - 0.5 / fps),
            "cut_t": start, "audio_edit_t": audio.get("edit_point", start),
            "same_scene": s.get("scene") == shots[k - 1].get("scene"),
        })
    return points


def measure(cut: str | Path, receipt: Mapping[str, Any], frames_dir: str | Path | None = None) -> dict[str, Any]:
    """The measured half of the critic."""
    cut = Path(cut)
    plan = receipt.get("plan") or receipt
    points = seam_points(receipt)
    env_t, env_db = stitch.audio_envelope(cut, hop_s=0.01)
    rate = 16000
    pcm = stitch.pcm_mono(cut, rate=rate) if np is not None else None
    frames_dir = Path(frames_dir) if frames_dir else None
    if frames_dir:
        frames_dir.mkdir(parents=True, exist_ok=True)
    seams = []
    for p in points:
        a = _frame_at(cut, p["before_t"])
        b = _frame_at(cut, p["after_t"])
        row: dict[str, Any] = {k: p[k] for k in ("seam", "from_shot", "to_shot", "kind", "cut_t",
                                                  "audio_edit_t", "same_scene")}
        flags = []
        if a is not None and b is not None:
            ma, mb = a.reshape(-1, 3).mean(axis=0), b.reshape(-1, 3).mean(axis=0)
            row["colour_delta_e"] = round(delta_e(ma, mb), 2)
            row["luma_jump"] = round(float(abs(a.mean() - b.mean())), 2)
            row["framing_similarity"] = _similarity(a, b)
            if p["same_scene"] and row["colour_delta_e"] > COLOR_DE_WARN:
                flags.append("colour jump")
            if p["same_scene"] and row["luma_jump"] > LUMA_JUMP_WARN:
                flags.append("brightness jump")
            if p["kind"] in stitch.HARD_CUTS and p["kind"] != "match_cut" and row["framing_similarity"] is not None \
                    and row["framing_similarity"] >= JUMP_CUT_SIMILARITY:
                flags.append("jump cut: nearly the same framing either side of a hard cut")
        before = _audio_around(env_t, env_db, p["audio_edit_t"], True)
        after = _audio_around(env_t, env_db, p["audio_edit_t"], False)
        row["audio_db_before"], row["audio_db_after"] = before, after
        if before is not None and after is not None:
            row["audio_jump_db"] = round(abs(after - before), 2)
            if row["audio_jump_db"] > AUDIO_JUMP_WARN_DB:
                flags.append(f"sound level jumps {row['audio_jump_db']:.0f} dB")
            if max(before, after) < SILENCE_DB:
                flags.append("dead air at the seam")
        crest = [c for c in (_click_at(pcm, rate, p["cut_t"]), _click_at(pcm, rate, p["audio_edit_t"])) if c is not None]
        row["click_crest_db"] = max(crest) if crest else None
        if row["click_crest_db"] is not None and row["click_crest_db"] > CLICK_CREST_DB:
            flags.append("click at the edit")
        if frames_dir:
            left = _save_jpeg(cut, p["before_t"], frames_dir / f"seam{p['seam']:02d}-a.jpg")
            right = _save_jpeg(cut, p["after_t"], frames_dir / f"seam{p['seam']:02d}-b.jpg")
            if left and right:
                pair = _pair_image(left, right, frames_dir / f"seam{p['seam']:02d}-pair.jpg",
                                   f"seam {p['seam']}: shot {p['from_shot']} to {p['to_shot']}, {p['kind']}")
                row["frames"] = {"before": left.name, "after": right.name, "pair": pair.name if pair else None}
        row["flags"] = flags
        seams.append(row)
    film = _film_measures(cut, plan)
    return {"seams": seams, "film": film}


def _film_measures(cut: Path, plan: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    lufs = stitch.measure_loudness(cut, 0.0, stitch.probe(cut)["duration"])
    out["integrated_lufs"] = lufs
    lengths = [s["kept_seconds"] for s in plan["shots"]]
    out["shots"] = len(lengths)
    out["average_shot_seconds"] = round(sum(lengths) / len(lengths), 2) if lengths else None
    out["longest_shot_seconds"] = max(lengths) if lengths else None
    notes = []
    if lengths and max(lengths) > 7.5:
        notes.append("a shot runs past 7.5 s; check it earns the time")
    if lufs is not None and abs(lufs - stitch.TARGET_LUFS) > 2.0:
        notes.append(f"loudness {lufs:.1f} LUFS is off the {stitch.TARGET_LUFS:.0f} target")
    frames = stitch.gray_frames(cut)
    diffs = stitch._motion_series(frames)
    if diffs is not None and len(diffs) > 24:
        fps = plan["fps"]
        still = diffs < 0.15
        runs = [(s, e) for s, e in stitch._runs(still) if (e - s) / fps >= 0.75]
        out["frozen_stretches"] = [{"start": round(s / fps, 2), "seconds": round((e - s) / fps, 2)} for s, e in runs]
        if runs:
            notes.append("frozen picture for 0.75 s or more")
        cuts = stitch.find_internal_cuts(diffs, fps)
        planned = [c["seconds"] for c in plan["timeline"]["cuts"]]
        extra = [c for c in cuts if not any(abs(c["time"] - t) <= 1.5 / fps for t in planned)]
        out["unplanned_cuts"] = extra
        if extra:
            notes.append("the picture jumps where no cut was planned (a cut inside a take)")
    out["notes"] = notes
    return out


# ------------------------------------------------------------ vision half

def default_vision_chat(url: str, model: str, timeout: int = 180) -> Callable[[str, str, bytes], str]:
    """A chat function for an OpenAI-compatible multimodal endpoint, or for an
    Ollama ``/api/chat`` endpoint (thinking off: through Ollama's OpenAI door a
    thinking model spends the whole budget thinking and answers nothing)."""
    if url.rstrip("/").endswith("/api/chat"):
        def ollama(system: str, text: str, jpeg: bytes) -> str:
            payload = {"model": model, "stream": False, "think": False,
                       "options": {"temperature": 0, "num_predict": 700},
                       "messages": [{"role": "system", "content": system.replace(" /no_think", "")},
                                    {"role": "user", "content": text.replace(" /no_think", ""),
                                     "images": [base64.b64encode(jpeg).decode()]}]}
            req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return str((json.loads(resp.read()).get("message") or {}).get("content") or "").strip()
        return ollama

    def chat(system: str, text: str, jpeg: bytes) -> str:
        payload = {"model": model, "temperature": 0, "max_tokens": 700, "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": [
                {"type": "text", "text": text},
                {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + base64.b64encode(jpeg).decode()}},
            ]}]}
        req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            env = json.loads(resp.read())
        msg = env["choices"][0]["message"]
        return (msg.get("content") or msg.get("reasoning_content") or "").strip()
    return chat


def vision_probe(chat: Callable[[str, str, bytes], str]) -> bool:
    """One tiny picture with a known answer. A text-only engine that silently
    drops images (the Spark 2 native engine did on 2026-09-26: "no image was
    provided") fails here, so the report says the look did not run instead of
    reading an empty answer as a pass."""
    try:
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (64, 64), (220, 20, 20)).save(buf, format="JPEG")
        answer = chat("Answer with one word.", "What colour fills this picture? One word. /no_think",
                      buf.getvalue())
    except Exception:
        return False
    return "red" in (answer or "").lower()


def bible_text(bible: Mapping[str, Any] | None) -> str:
    from .director_school import normalize_bible
    b = normalize_bible(bible)
    lines = []
    for key in ("location", "time_of_day", "lighting", "palette", "style", "lens"):
        if b[key]:
            lines.append(f"{key.replace('_', ' ')}: {b[key]}")
    for c in b["characters"]:
        wardrobe = f"; wardrobe: {c['wardrobe']}" if c["wardrobe"] else ""
        lines.append(f"character {c['name']}: {c['look']}{wardrobe}")
    return "\n".join(lines) or "(no bible supplied)"


def _parse_json(text: str) -> dict[str, Any] | None:
    text = re.sub(r"<think>.*?</think>", "", text or "", flags=re.DOTALL)
    m = re.search(r"\{.*\}", text, re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def look(measured: Mapping[str, Any], frames_dir: str | Path, bible: Mapping[str, Any] | None,
         chat: Callable[[str, str, bytes], str]) -> list[dict[str, Any]]:
    """Ask the vision model about every seam pair image."""
    frames_dir = Path(frames_dir)
    context = bible_text(bible)
    answers = []
    for row in measured["seams"]:
        pair = (row.get("frames") or {}).get("pair")
        if not pair:
            answers.append({"seam": row["seam"], "error": "no pair image"})
            continue
        text = (f"Continuity bible:\n{context}\n\nThis is seam {row['seam']}: shot {row['from_shot']} (LEFT) "
                f"cuts to shot {row['to_shot']} (RIGHT) with a {row['kind'].replace('_', ' ')}. "
                f"{'Both shots are in the same scene.' if row['same_scene'] else 'The scene changes here.'} "
                "Answer with the JSON object only. /no_think")
        try:
            raw = chat(VISION_SYSTEM, text, (frames_dir / pair).read_bytes())
            data = _parse_json(raw)
            answers.append({"seam": row["seam"], **(data or {"error": "unreadable answer", "raw": raw[:300]})})
        except Exception as exc:  # the report says the look failed; it never passes silently
            answers.append({"seam": row["seam"], "error": f"vision model unavailable: {type(exc).__name__}"})
    return answers


# --------------------------------------------------------------- lip-sync

LIPSYNC_PASS_OFFSET = 1       # frames; the private delivery gate's bar
LIPSYNC_PASS_CONFIDENCE = 5.0
LIPSYNC_FAIL_OFFSET = 3
LIPSYNC_FAIL_CONFIDENCE = 2.5


def syncnet_runner() -> Callable[[str], dict] | None:
    """SyncNet on the CPU through a LatentSync checkout, when the host has one
    (MEDIA_LAB_SYNCNET_PYTHON + MEDIA_LAB_LATENTSYNC_ROOT); None otherwise."""
    python = os.environ.get("MEDIA_LAB_SYNCNET_PYTHON", "").strip()
    root = os.environ.get("MEDIA_LAB_LATENTSYNC_ROOT", "").strip()
    if not python or not root or not Path(python).exists() or not Path(root).is_dir():
        return None
    script = Path(__file__).resolve().parents[1] / "runner" / "syncnet_measure.py"

    def run(video: str) -> dict:
        # LatentSync's S3FD opens checkpoints/auxiliary/sfd_face.pth by a path
        # relative to the checkout, so run there (with an absolute video path)
        result = subprocess.run([python, str(script), str(Path(video).resolve()), "--root", root],
                                capture_output=True, text=True, timeout=900, check=False, cwd=root)
        lines = [ln for ln in (result.stdout or "").splitlines() if ln.strip().startswith("{")]
        if result.returncode != 0 or not lines:
            return {"error": (result.stderr or "syncnet failed").strip()[-200:]}
        return json.loads(lines[-1])
    return run


def lipsync(plan: Mapping[str, Any], runner: Callable[[str], dict] | None) -> list[dict[str, Any]]:
    """Source-level lip-sync for every shot that speaks (SyncNet offset/confidence)."""
    rows = []
    for i, shot in enumerate(plan["shots"], 1):
        if not shot.get("dialogue"):
            continue
        row = {"shot": i}
        if runner is None:
            row["verdict"] = "not measured"
        else:
            m = runner(str(shot.get("source_path") or shot.get("source")))
            row.update(m)
            off, conf = m.get("av_offset_frames"), m.get("confidence")
            if m.get("error") or not m.get("face_track", True):
                row["verdict"] = "not measured" if m.get("error") else "no face found"
            elif abs(off) >= LIPSYNC_FAIL_OFFSET or conf < LIPSYNC_FAIL_CONFIDENCE:
                row["verdict"] = "off"
            elif abs(off) <= LIPSYNC_PASS_OFFSET and conf >= LIPSYNC_PASS_CONFIDENCE:
                row["verdict"] = "in sync"
            else:
                row["verdict"] = "borderline"
        rows.append(row)
    return rows


# ---------------------------------------------------------------- verdicts

_RERENDER_WORDS = ("different person", "different clothes", "different set", "looks into the camera",
                   "garbled lettering", "melted face", "extra or missing fingers", "panels or collage",
                   "wrong number of people")


def verdicts(measured: Mapping[str, Any], vision: Sequence[Mapping[str, Any]] | None,
             plan: Mapping[str, Any], lips: Sequence[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """Merge both halves into per-seam verdicts and a re-render list."""
    by_seam = {v["seam"]: v for v in (vision or []) if "seam" in v}
    seams = []
    rerender: dict[int, list[str]] = {}
    for row in measured["seams"]:
        v = by_seam.get(row["seam"], {})
        verdict = "ok"
        reasons = list(row.get("flags") or [])
        if v and not v.get("error"):
            vv = str(v.get("verdict") or "ok").lower()
            for side, shot in (("left", row["from_shot"]), ("right", row["to_shot"])):
                probs = [str(p).lower() for p in (v.get(f"{side}_problems") or [])]
                serious = [p for p in probs if any(w in p for w in _RERENDER_WORDS)]
                if serious or vv == f"rerender_{side}":
                    rerender.setdefault(shot, []).extend(serious or [str(v.get("reason") or "vision critic")])
                    verdict = "rerender"
            if verdict == "ok" and vv in {"fix_colour", "fix_color"}:
                verdict = "fix_colour"
            if v.get("reason"):
                reasons.append("vision: " + str(v["reason"])[:200])
        if verdict == "ok" and any(f.startswith(("colour", "brightness")) for f in row.get("flags") or []):
            verdict = "fix_colour"
        if verdict == "ok" and any(f.startswith(("click", "sound level", "dead air", "jump cut")) for f in row.get("flags") or []):
            verdict = "fix_edit"
        seams.append({"seam": row["seam"], "from_shot": row["from_shot"], "to_shot": row["to_shot"],
                      "kind": row["kind"], "verdict": verdict, "reasons": reasons,
                      "vision": {k: v[k] for k in v if k != "seam"} if v else None,
                      "measures": {k: row.get(k) for k in ("colour_delta_e", "luma_jump", "framing_similarity",
                                                            "audio_jump_db", "click_crest_db")},
                      "frames": row.get("frames")})
    for i, shot in enumerate(plan["shots"], 1):
        if shot.get("mid_cuts"):
            rerender.setdefault(i, []).append("the take contains a cut the director did not ask for")
    for row in lips or []:
        if row.get("verdict") == "off":
            rerender.setdefault(row["shot"], []).append(
                f"lip-sync off by {row.get('av_offset_frames')} frames (SyncNet confidence {row.get('confidence', 0):.1f})")
    film = measured.get("film") or {}
    for c in film.get("unplanned_cuts") or []:
        idx = _shot_at(plan, c["time"])
        if idx:
            rerender.setdefault(idx, []).append(f"unplanned cut at {c['time']:.2f}s")
    vision_ran = bool(vision) and any(not v.get("error") for v in vision)
    ok = not rerender and all(s["verdict"] == "ok" for s in seams)
    return {"seams": seams,
            "rerender": [{"shot": k, "reasons": sorted(set(v))} for k, v in sorted(rerender.items())],
            "film": film, "vision_ran": vision_ran, "lipsync": list(lips or []),
            "pass": ok and vision_ran,
            "summary": ("clean" if ok else f"{len(rerender)} shot(s) to re-render, "
                        f"{sum(1 for s in seams if s['verdict'] != 'ok')} seam(s) flagged")
                       + ("" if vision_ran else " (vision check did not run: measured checks only)")}


def _shot_at(plan: Mapping[str, Any], t: float) -> int | None:
    for i, s in enumerate(plan["shots"], 1):
        if s["timeline_start"] <= t < s["timeline_start"] + s["kept_seconds"]:
            return i
    return None


def review(cut: str | Path, receipt: Mapping[str, Any], *, frames_dir: str | Path,
           bible: Mapping[str, Any] | None = None,
           chat: Callable[[str, str, bytes], str] | None = None,
           syncnet: Callable[[str], dict] | None = None) -> dict[str, Any]:
    """Measure, optionally look, and decide. Writes seam frames to ``frames_dir``."""
    measured = measure(cut, receipt, frames_dir)
    vision = look(measured, frames_dir, bible, chat) if chat else None
    plan = receipt.get("plan") or receipt
    lips = lipsync(plan, syncnet) if any(s.get("dialogue") for s in plan["shots"]) else []
    report = verdicts(measured, vision, plan, lips)
    report["schema"] = REPORT_SCHEMA
    report["cut"] = str(cut)
    return report


def rerender_patch(reasons: Sequence[str]) -> str:
    """Words added to the next take's prompt for each problem the critic named."""
    fixes = []
    joined = " ".join(reasons).lower()
    if "camera" in joined:
        fixes.append("Nobody looks into the lens; eyes stay on each other or the action.")
    if "lettering" in joined or "text" in joined:
        fixes.append("No readable lettering anywhere: signs are out of focus or turned away.")
    if "different person" in joined or "wrong number" in joined:
        fixes.append("Exactly the people named here, matching their descriptions exactly; nobody else has a visible face.")
    if "clothes" in joined:
        fixes.append("Wardrobe exactly as described, unchanged.")
    if "set" in joined:
        fixes.append("The same room, furniture and light as the rest of the scene.")
    if "finger" in joined or "melted" in joined:
        fixes.append("Hands relaxed and simple; slow, small movements; natural faces.")
    if "cut" in joined:
        fixes.append("One continuous shot with no cuts.")
    return " ".join(fixes)


def waveform_png(cut: str | Path, receipt: Mapping[str, Any], out: str | Path, title: str = "") -> Path | None:
    """The cut's sound level over time with every picture cut (white) and
    audio edit (orange) marked, so a seam's level jump is visible at a glance."""
    try:
        from PIL import Image, ImageDraw
    except Exception:
        return None
    env_t, env_db = stitch.audio_envelope(cut, hop_s=0.02)
    if env_t is None:
        return None
    plan = receipt.get("plan") or receipt
    duration = float(env_t[-1]) + 0.02
    width, height, top, floor_db = 1400, 240, 28, -60.0
    img = Image.new("RGB", (width, height), (16, 16, 20))
    draw = ImageDraw.Draw(img)

    def x(t):
        return int(t / duration * (width - 1))

    def y(db):
        db = max(floor_db, min(0.0, float(db)))
        return int(top + (height - top - 8) * (db / floor_db))
    points = [(x(t), y(db)) for t, db in zip(env_t, env_db)]
    draw.polygon([(0, height - 8)] + points + [(width - 1, height - 8)], fill=(70, 150, 220))
    for db in (-12, -24, -36, -48):
        draw.line([(0, y(db)), (width, y(db))], fill=(45, 45, 55))
        draw.text((4, y(db) - 11), f"{db} dB", fill=(120, 120, 130))
    for k, shot in enumerate(plan["shots"]):
        start = float(shot.get("timeline_start") or 0.0)
        if k:
            draw.line([(x(start), top), (x(start), height)], fill=(240, 240, 240), width=2)
            edit = (shot.get("audio") or {}).get("edit_point")
            if edit is not None and abs(edit - start) > 0.03:
                draw.line([(x(edit), top), (x(edit), height)], fill=(255, 150, 40), width=2)
        label = f"{k + 1}"
        if shot.get("lufs") is not None:
            label += f"  {shot['lufs']:.0f} LUFS in"
        draw.text((x(start) + 5, top + 3), label, fill=(230, 230, 230))
    draw.text((6, 6), title or "Sound level; white = picture cut, orange = audio edit", fill=(235, 235, 235))
    out = Path(out)
    img.save(out)
    return out


def markdown(report: Mapping[str, Any], title: str = "Seam critique") -> str:
    lines = [f"# {title}", "", f"**Result:** {report['summary']}", ""]
    film = report.get("film") or {}
    if film:
        lines.append(f"Film: {film.get('shots')} shots, average {film.get('average_shot_seconds')} s, "
                     f"loudness {film.get('integrated_lufs')} LUFS.")
        for n in film.get("notes") or []:
            lines.append(f"- {n}")
        lines.append("")
    lines.append("| Seam | Shots | Transition | Verdict | Colour ΔE | Framing sim. | Sound jump dB | Notes |")
    lines.append("|---|---|---|---|---|---|---|---|")
    for s in report["seams"]:
        m = s["measures"]
        lines.append(f"| {s['seam']} | {s['from_shot']}→{s['to_shot']} | {s['kind'].replace('_', ' ')} | "
                     f"{s['verdict']} | {m.get('colour_delta_e')} | {m.get('framing_similarity')} | "
                     f"{m.get('audio_jump_db')} | {'; '.join(s['reasons'])[:300]} |")
    if report.get("lipsync"):
        lines += ["", "Lip-sync (SyncNet, source takes):"]
        for row in report["lipsync"]:
            detail = ("" if row.get("av_offset_frames") is None else
                      f": offset {row['av_offset_frames']} frames, confidence {row.get('confidence', 0):.1f}")
            lines.append(f"- shot {row['shot']}: {row['verdict']}{detail}")
    if report.get("rerender"):
        lines += ["", "Re-render:"]
        for r in report["rerender"]:
            lines.append(f"- shot {r['shot']}: {'; '.join(r['reasons'])}")
    return "\n".join(lines) + "\n"
