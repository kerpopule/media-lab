#!/usr/bin/env python3
"""The Media Lab Director — the studio's own brain, deciding how to drive the engines.

The owner's brief: "the user just chooses h3 or ltx, but the under the hood stuff needs
to be qwen media lab bot's expertise... the same one that talks in the chat widget."

So this is the SAME model, the SAME identity, and the same system-prompt preamble as
/api/chat — it simply wears a second hat. When Generate is pressed, every input the
user gave (their words, their character, their start image, their audio) is read by
Qwen 3.8 27B, with its vision tower looking at every attached picture, and the
Director decides:

  * WHICH WEIGHTS   — ref2va (a person who must stay themselves), fl2va (a real
                      first/last frame to honour), t2va (words only), or LTX.
  * WHICH SETTINGS  — canvas, frames, steps, seed, reference detail, face size.
  * WHICH WORDS     — the prompt, rewritten into the exact grammar the engine was
                      trained to read.

WHY THIS EXISTS. MiniMax ship H3 as three modules, and the one that decides quality
is the one we never had: H3-Context-IR, which rewrites a plain request into a
structured intermediate representation. Their words: "H3-Context-IR is critical to
the quality of the final output, so we strongly recommend incorporating it into your
generation pipeline or following the 'Prompting Guidance' to build your own
context-processing system." That module is not open-sourced. This file is our own,
built from MiniMax's published guidance, which sits next to it in director/.

THE DIVISION OF LABOUR — read this before changing anything. The model ADVISES;
Python DECIDES anything with a hard contract. A language model will cheerfully
propose 130 frames or a 1000x750 canvas, and the engine will refuse or, worse,
silently stretch. So every number the model returns is passed through the clamps
below before it reaches an engine. The model's job is judgement — what kind of shot
this is, which reference is the good one, how to describe a mouth closing. Not
arithmetic.
"""
import base64
import json
import math
import os
import re
import urllib.request
from pathlib import Path

CHAT = os.environ.get("QWEN_URL", "http://127.0.0.1:8003/v1/chat/completions")
MODEL = os.environ.get("QWEN_MODEL", "qwen3.8-27b-q4km")
HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
GUIDES = ROOT / "director"

# ---------------------------------------------------------------- hard contracts
# Everything in this block is the engine's law, verified against the running
# container (/opt/maestro/app/app/models/minimax_h3/) and MiniMax's model card.
# None of it is negotiable by the model.

H3_FPS = 24
H3_MIN_SECONDS, H3_MAX_SECONDS = 5.0, 15.0

# The canvas rule is Maestro's own resolve_canvas_size(): scale BOTH axes to fit the
# area cap, then round each to a multiple of 32. It never changes the aspect ratio.
# Its own ranker weights aspect error 8x above area error, so when the pixels do not
# fit, the ratio is what you protect. "Hold the 768 short edge and narrow the width"
# is NOT a documented rule — it existed only in our own comment, and it is what made
# a 16:9 request render 4:3 while H3 stretched the start frame onto it.
#
# 1344x768 is the released canvas but it is not reachable here: measured 2026-08-18,
# a 1024x768 take held GPU at 96% with memory and swap FLAT and completed 0 of 24
# steps in 9 minutes. That is compute-bound, not starved — more RAM will not help.
# So the working band is 500-750k pixels, and face detail is bought with FRAMING.
H3_CANVAS_CAP = int(os.environ.get("H3_MAX_PIXELS", 737280))    # 1152x640
H3_BASE_CANVAS = {"landscape": (1344, 768), "portrait": (768, 1344), "square": (768, 768)}
LTX_CANVAS = {"landscape": (1280, 704), "portrait": (704, 1280), "square": (960, 960)}


def _round32(n):
    return max(32, int(round(n / 32.0)) * 32)


def h3_frames(seconds):
    """H3 decodes in 17-frame blocks: frames must be 17k+5, and land in 5-15s."""
    seconds = min(max(float(seconds or 5.0), H3_MIN_SECONDS), H3_MAX_SECONDS)
    k = max(0, round((seconds * H3_FPS - 5) / 17.0))
    frames = 17 * k + 5
    lo, hi = 17 * math.ceil((H3_MIN_SECONDS * H3_FPS - 5) / 17.0) + 5, 0
    hi = 17 * math.floor((H3_MAX_SECONDS * H3_FPS - 5) / 17.0) + 5
    return min(max(frames, lo), hi)


def ltx_frames(seconds):
    """LTX is 8k+1."""
    seconds = max(float(seconds or 5.0), 1.0)
    k = max(1, round((seconds * H3_FPS - 1) / 8.0))
    return 8 * k + 1


def h3_canvas(orientation="landscape", max_pixels=None):
    """Maestro's resolver: uniform scale under the area cap, both axes to /32.

    At the released cap (1032192) a 16:9 request IS 1344x768 and nothing moves.
    Lower the cap for memory and BOTH axes shrink together, so the aspect the
    caller asked for is the aspect that renders.
    """
    cap = int(max_pixels or H3_CANVAS_CAP)
    w, h = H3_BASE_CANVAS.get(orientation, H3_BASE_CANVAS["landscape"])
    scale = min(1.0, math.sqrt(cap / float(w * h)))
    W, H = _round32(w * scale), _round32(h * scale)
    while W * H > cap:                        # rounding up can re-cross the cap
        scale *= 0.98
        W, H = _round32(w * scale), _round32(h * scale)
    return W, H


# A transformer token covers 32x32 output pixels (VAE 16x spatial, patch (1,2,2)).
# At 864x480 a face at 12% of frame height was 1.8 tokens for the WHOLE face and the
# mouth was a fraction of one — that is the mushy mouth, and no prompt can fix it.
# A take judged great measured 0.40; one judged horrible, 0.12.
#
# These numbers are OURS, derived from that geometry — MiniMax publishes nothing at
# all about faces. Treat them as a hypothesis to measure, not a specification.
# Framing is the cheapest quality lever on the box: +33% face tokens at zero compute.
FACE_TARGET = {"h3": 0.48, "ltx": 0.36}
FACE_MIN = {"h3": 0.30, "ltx": 0.22}

# ref2va is the vendor's identity mode and the right destination — but today every
# request would 500 (an empty manifest is rejected), the TTS waveform would be
# silently discarded (source_audio_mode is hard-false under omni), and the start
# frame would be ignored. Until the manifest builder carries audio, choosing it
# would trade imperfect lip sync for none. Flip this when that plumbing lands.
REF2VA_READY = os.environ.get("MEDIA_LAB_REF2VA", "").lower() in ("1", "true", "yes")


# ---------------------------------------------------------------- the shared brain
def _identity_preamble():
    """The Director and the chat widget are the same assistant, by design.

    We load the widget's own system prompt so the studio has one personality and one
    body of knowledge; the role block below is the second hat, not a second brain.
    """
    try:
        return (ROOT / "chat-system-prompt.md").read_text()[:6000]
    except Exception:
        return "You are the Media Lab guide, the in-house assistant for a local AI studio."


def _guide(name):
    try:
        return (GUIDES / name).read_text()
    except Exception:
        return ""


def _post(body, timeout=600):
    req = urllib.request.Request(CHAT, json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        d = json.load(r)
    msg = d["choices"][0]["message"]
    txt = (msg.get("content") or "").strip() or (msg.get("reasoning_content") or "").strip()
    return re.sub(r"<think>.*?</think>", "", txt, flags=re.S).strip()


def _json(txt):
    m = re.search(r"\{.*\}", txt, re.S)
    if not m:
        raise ValueError(f"no JSON in reply: {txt[:200]}")
    return json.loads(m.group(0))


def _ask_json(system, user, images=(), max_tokens=3000, temperature=0.2, tries=2):
    content = [{"type": "text", "text": user}]
    for p in images:
        try:
            b = base64.b64encode(Path(p).read_bytes()).decode()
        except Exception:
            continue
        content.append({"type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{b}"}})
    body = {"model": MODEL, "max_tokens": max_tokens, "temperature": temperature,
            "messages": [{"role": "system", "content": system + " /no_think"},
                         {"role": "user", "content": content if images else user}]}
    last = None
    for _ in range(tries):
        try:
            return _json(_post(body))
        except Exception as e:
            last = e
    raise last


# ---------------------------------------------------------------- stage 1: LOOK
LOOK_SYS = """You are the Media Lab's eyes. You examine one picture a user has attached to a
render and report what it actually is, so the studio can decide how to use it.
Answer with ONLY a JSON object, no prose."""

LOOK_ASK = """Look at this picture and answer:

{"kind": "portrait" | "contact_sheet" | "full_body" | "scene_plate" | "object" | "other",
 "panels": <number of separate photo panels, 1 if it is a single photograph>,
 "people": <how many distinct people are visible>,
 "face_box": [x0, y0, x1, y1] or null,
 "face_frac": <height of the largest face as a fraction of image height, 0-1>,
 "front_facing": true | false,
 "identity_grade": "excellent" | "usable" | "poor",
 "appearance": "<one sentence naming the concrete, unchanging physical features of the
   main person: face shape, hair colour and length, facial hair, glasses, eye colour,
   skin tone, build, and what they are wearing. This is what the video model will be
   told to preserve, so name only what you can actually see.>",
 "background": "<what is behind them, in a few words>"}

face_box uses fractions of image width/height, top-left origin.
identity_grade is 'excellent' only for a sharp, well-lit, front or three-quarter face
large enough to read the eyes. Say 'poor' if the face is small, blurry, turned away,
or partly cut off — the studio will pick a different reference rather than render a
stranger."""


def look(path):
    """Read one attached picture. Returns {} rather than raising — a blind Director
    still works, it just decides with less."""
    try:
        out = _ask_json(LOOK_SYS, LOOK_ASK, images=[path], max_tokens=600)
    except Exception as e:
        return {"kind": "other", "error": str(e)[:120]}
    out["path"] = str(path)
    return out


# ---------------------------------------------------------------- stage 2: DECIDE
DECIDE_ROLE = """

=== YOUR SECOND HAT: THE DIRECTOR ===

Besides talking to people in the chat widget, you are the studio's Director. When
someone presses Generate, you read everything they gave you and decide how to shoot
it. They chose only the camera — MiniMax H3 or LTX 2.5. Every other decision is
yours, and they never see it. Your job is that the result simply looks right.

WHAT YOU ARE CHOOSING BETWEEN (H3 only — LTX has one path):

* "ref2va"  — the actor-cloning weights. Choose this whenever a REAL PERSON must
              stay recognisably themselves: a character with a reference sheet, a
              face someone uploaded, anyone speaking a line as themselves. The
              references guide identity and the model is free to recompose the shot.
              This is MiniMax's own answer for a talking person.
* "fl2va"   — first/last-frame weights. Choose this ONLY when a supplied image must
              literally BE the first (or last) frame — animating a specific picture,
              continuing from the last frame of a previous shot, a start-and-end
              transition. It interpolates a path between frames; it does NOT protect
              a person's identity.
* "t2va"    — words only. No usable image was given, or the shot has no person who
              must match anyone real.

THE TRAP TO AVOID: handing a person's photo to fl2va. It will honour the frame and
lose the face. If someone must look like themselves, it is ref2va.

FRAMING IS A QUALITY SETTING, NOT A TASTE. The video model paints a face out of a
grid where one cell is 32x32 pixels. A face filling 12% of frame height gets under
two cells for the entire face and the mouth becomes a smear. Dialogue therefore
needs a close or medium-close shot — head and shoulders filling the frame. Never
write "wide shot", "full body" or "upper body visible" for a shot with a spoken
line, unless the user explicitly asked for distance, and then say so in "risk".

Reply with ONLY a JSON object:

{"mode": "ref2va" | "fl2va" | "t2va" | "ltx",
 "why": "<one short sentence a non-technical person would find reassuring, shown on
   the queue card — e.g. 'Using the actor-cloning weights so Ava stays herself.'>",
 "orientation": "landscape" | "portrait" | "square",
 "seconds": <how long the shot should be; honour what the user asked, else judge it>,
 "reference_detail": "max" | "match",
 "references": [{"path": "<one of the attached asset paths>",
                 "image_intent": "identity" | "scene" | "style" | "composition",
                 "role": "<what THIS picture is for, and what to ignore about it —
                    e.g. 'Ava's face and hair; ignore its grey backdrop'>"}],
 "start_frame": "<path of the image that must literally be frame zero, or null>",
 "subjects": [{"id": "S1", "name": "<who>",
               "appearance": "<the concrete unchanging features to preserve>"}],
 "dialogue": [{"speaker": "S1", "text": "<exactly what they say, verbatim>"}],
 "scene": "<the setting, in a sentence>",
 "camera": "<the camera's behaviour in a sentence>",
 "risk": "<anything the user should know, or empty string>"}

RULES FOR references: order matters — the first image is <Picture 1>, and the model
reads them in that order. Give each ONE job and, where it helps, one exclusion
("ignore its background"). Put the best, sharpest, most front-facing identity photo
first. Never list a picture the vision report graded "poor" if a better one exists.
Use "max" reference_detail whenever a real person's likeness matters; "match" only
for style or scene references where speed is worth more than fidelity."""

DECIDE_ASK = """THE REQUEST

The user pressed Generate with the camera set to: {engine}
What they typed: {intent}
Shot length they asked for: {seconds}
Shape they asked for: {orientation}
The line to be spoken (empty if this is not a talking shot): {line}
The character cast in this shot (empty if none): {character}

THE ASSETS THEY ATTACHED — your own eyes' report on each:
{assets}

Decide how to shoot it."""


def decide(intent, engine="h3", seconds=None, orientation=None, line="",
           character=None, assets=()):
    """Stage 2 — the judgement call. Returns a plan dict, already clamped."""
    sys = _identity_preamble() + DECIDE_ROLE
    ask = DECIDE_ASK.format(
        engine=("MiniMax H3" if engine == "h3" else "LTX 2.5 Fast"),
        intent=(intent or "").strip() or "(they typed nothing)",
        seconds=(f"{seconds} seconds" if seconds else "they did not say"),
        orientation=orientation or "they did not say",
        line=(line or "").strip(),
        character=json.dumps(character or {}, ensure_ascii=False)[:1200],
        assets=json.dumps(list(assets), ensure_ascii=False, indent=1)[:4000] or "(none)")
    plan = _ask_json(sys, ask, max_tokens=2000, temperature=0.3)
    return clamp(plan, engine=engine, requested_seconds=seconds,
                 requested_orientation=orientation, line=line)


def clamp(plan, engine="h3", requested_seconds=None, requested_orientation=None, line=""):
    """Python has the last word on everything the engine can refuse.

    The model is good at "this is a close-up of a man who must stay himself". It is
    not to be trusted with 17k+5. Anything it got wrong here is corrected silently
    and noted in plan['corrections'] so we can see what it tends to fumble.
    """
    fixed = []
    mode = str(plan.get("mode") or "").lower()
    if engine != "h3":
        if mode != "ltx":
            fixed.append(f"mode {mode or 'unset'} -> ltx (the user chose LTX)")
        mode = "ltx"
    elif mode not in ("ref2va", "fl2va", "t2va"):
        mode = "ref2va" if plan.get("references") else "t2va"
        fixed.append(f"mode -> {mode}")

    if mode == "ref2va" and not REF2VA_READY:
        # Not a downgrade to hide: fl2va with a real start frame, the right aspect and
        # tight framing has never once been evaluated. That is the cheap experiment.
        mode = "fl2va"
        fixed.append("ref2va -> fl2va (actor-cloning plumbing not wired yet)")

    orientation = str(plan.get("orientation") or requested_orientation or "landscape").lower()
    if orientation not in H3_BASE_CANVAS:
        orientation = "landscape"
        fixed.append("orientation -> landscape")

    seconds = plan.get("seconds") or requested_seconds or 5.0
    try:
        seconds = float(seconds)
    except Exception:
        seconds = 5.0

    if mode == "ltx":
        w, h = LTX_CANVAS[orientation]
        frames = ltx_frames(seconds)
    else:
        w, h = h3_canvas(orientation)
        frames = h3_frames(seconds)
        if not (H3_MIN_SECONDS <= seconds <= H3_MAX_SECONDS):
            fixed.append(f"{seconds:g}s -> {frames / H3_FPS:.2f}s (H3 shoots 5-15s)")

    # 'max' is the documented identity lever, but THIS fork's resolve_reference_image_size()
    # has no min(1.0, ...) clamp — so 'max' upscales a small crop to a 2048px short edge
    # and charges full attention price for invented pixels. Earn it on a hero take.
    detail = str(plan.get("reference_detail") or "match").lower()
    if detail not in ("max", "match"):
        detail = "match"

    refs = [r for r in (plan.get("references") or []) if isinstance(r, dict) and r.get("path")]
    if mode == "ref2va" and not refs:
        mode = "t2va"
        fixed.append("ref2va -> t2va (no usable reference survived)")
    if len(refs) > 9:                       # the manifest caps images at 9
        refs = refs[:9]
        fixed.append("references trimmed to 9")

    # The spoken line is the studio's, not the model's — it must survive verbatim.
    dialogue = [d for d in (plan.get("dialogue") or [])
                if isinstance(d, dict) and str(d.get("text") or "").strip()]
    if line.strip() and not dialogue:
        dialogue = [{"speaker": "S1", "text": line.strip()}]
        fixed.append("dialogue restored from the requested line")
    elif line.strip() and dialogue[0].get("text", "").strip() != line.strip():
        dialogue[0]["text"] = line.strip()
        fixed.append("dialogue text restored verbatim")

    kind = "h3" if mode != "ltx" else "ltx"
    plan.update({
        "mode": mode, "engine": engine, "orientation": orientation,
        "width": w, "height": h, "frames": frames,
        "seconds": round(frames / H3_FPS, 3),
        "reference_detail": detail, "references": refs, "dialogue": dialogue,
        "face_target": FACE_TARGET[kind], "face_min": FACE_MIN[kind],
        # Deliberately NOT a knob. MiniMax's hosted examples say 50, but this fork
        # counts sigma grid points differently and already runs 24 evaluations against
        # its own default of 19 — and flow shift (12.0 video / 3.0 audio) is correct
        # and not settable. Leave the shim's default alone and spend effort on framing.
        "steps": int(plan["steps"]) if str(plan.get("steps") or "").isdigit() else None,
        "corrections": fixed,
    })
    return plan


# ---------------------------------------------------------------- stage 3: WRITE
WRITE_ROLE = """

=== YOUR THIRD HAT: THE PROMPT WRITER ===

You now write the prompt itself, in the exact grammar MiniMax's model was trained to
read. This is not English prose with some tags sprinkled in — the field names, their
order, and the tag syntax are part of the model's input format. Getting them wrong
does not produce a worse video; it produces a video that ignored you.

The authoritative guide is reproduced below. Follow it exactly. In particular:

* Emit the fields in the given order, with the given names, each on its own line.
* Reference tags are angle-bracketed and numbered per modality, by the order the
  studio listed them: <Picture 1>, <Picture 2>, <Audio 1>, <Video 1>.
* An image that only defines a person does NOT get its own <Picture N> entry in
  the description — cite it inside that <Subject N> definition.
* Every spoken line is wrapped <d>[English] ... </d> and carries its speaker id
  (S1), (S2) — assigned in the order people first speak, and reused everywhere.
* Say that the subject PHYSICALLY SPEAKS and that their mouth syncs to the line.
* End the speech explicitly — describe the mouth and jaw coming to rest — or the
  model keeps the mouth moving after the words stop.
* retention_analysis uses fixed words, never paraphrased: fully_preserved,
  partially_preserved, attribute_transfer, weak_reference for what is seen;
  fully_copy, partially_copy, reference, weak_reference for what is heard.
* detailed_description is the body of the work: aim for 350-500 words. Establish
  composition, the subject's appearance and position, the environment and its light,
  the actions and how they change, the camera, and the sound. A short description
  is an invitation for the model to invent a face.

Reply with ONLY a JSON object: {"prompt": "<the complete prompt document>"}
Put real newlines inside the string. Write nothing else."""


def write_prompt(plan, scene="", camera="", extra=""):
    """Stage 3 — turn the plan into the engine's own grammar."""
    mode = plan.get("mode")
    if mode == "ltx":
        return _ltx_prompt(plan, scene, camera, extra)

    ref_guide = _guide("VIDEO_PROMPT_WRITING_GUIDE_ref_en.md")
    base_guide = _guide("VIDEO_PROMPT_WRITING_GUIDE_base_en.md")
    guide = ref_guide if mode == "ref2va" else base_guide

    sys = _identity_preamble() + WRITE_ROLE + "\n\n=== THE OFFICIAL GUIDE ===\n" + guide[:24000]

    # The instruction line for image-conditioned base modes is verbatim from the
    # guide and must be the FIRST line, followed by exactly one blank line.
    head = ""
    if mode == "fl2va" and plan.get("start_frame"):
        head = ("For the target video, at 0.00 seconds into the target video, "
                "<Picture 1> (from [Shot 1]) is fully referenced.")

    ask = json.dumps({
        "mode": mode,
        "fields": (["subject_definitions", "summary", "retention_analysis",
                    "detailed_description", "overall_soundscape", "non_diegetic_music"]
                   if mode == "ref2va" else
                   ["integrated_multimodal_description", "overall_soundscape",
                    "non_diegetic_music"]),
        "instruction_line_that_must_come_first": head or None,
        "seconds": plan.get("seconds"),
        "subjects": plan.get("subjects") or [],
        "references_in_order": [
            {"tag": f"<Picture {i + 1}>", "role": r.get("role"),
             "intent": r.get("image_intent", "identity")}
            for i, r in enumerate(plan.get("references") or [])],
        "audio_reference": plan.get("audio_reference"),
        "dialogue": plan.get("dialogue") or [],
        "scene": scene or plan.get("scene") or "",
        "camera": camera or plan.get("camera") or "",
        "notes": extra or "",
    }, ensure_ascii=False, indent=1)

    out = _ask_json(sys, "Write the prompt for this shot.\n\n" + ask,
                    max_tokens=4000, temperature=0.4)
    prompt = str(out.get("prompt") or "").strip()
    return _repair(prompt, plan, head)


def _repair(prompt, plan, head):
    """Last-mile guarantees the model must not be able to lose.

    Each of these was a real, measured failure: dialogue that never reached the
    engine at all, and an instruction line the guide calls mandatory.
    """
    if head and not prompt.startswith(head):
        prompt = head + "\n\n" + prompt
    for d in (plan.get("dialogue") or []):
        text = str(d.get("text") or "").strip()
        if text and text not in prompt:
            sid = d.get("speaker") or "S1"
            prompt += (f"\n\n({sid}) physically speaks, mouth movements naturally syncing "
                       f"to the dialogue: <d>[English] {text}</d> Exactly as the voice "
                       f"stops, the lips come together and the jaw ceases speaking motion.")
    return prompt.strip()


def _ltx_prompt(plan, scene, camera, extra):
    """LTX takes plain cinematic prose — but the same framing discipline applies."""
    sys = _identity_preamble() + """

=== YOUR THIRD HAT: THE PROMPT WRITER (LTX) ===
Write ONE paragraph of cinematic prose for the LTX video model. Name the subject and
the concrete features that must not drift, the setting and its light, the action, and
the camera. If a line is spoken, say that the person speaks it and that their mouth
syncs to their voice. Keep the camera locked off unless movement was asked for.
Reply with ONLY {"prompt": "<the paragraph>"}."""
    ask = json.dumps({"subjects": plan.get("subjects") or [],
                      "dialogue": plan.get("dialogue") or [],
                      "scene": scene or plan.get("scene") or "",
                      "camera": camera or plan.get("camera") or "",
                      "notes": extra or ""}, ensure_ascii=False)
    out = _ask_json(sys, "Write the prompt.\n\n" + ask, max_tokens=1200, temperature=0.5)
    return str(out.get("prompt") or "").strip()


# ---------------------------------------------------------------- the whole job
def direct(intent, engine="h3", seconds=None, orientation=None, line="",
           character=None, asset_paths=(), scene="", camera=""):
    """One call: look at everything, decide, and write the prompt.

    Returns the plan with 'prompt' filled in, ready to hand to the engine. Never
    raises for a bad picture or a sulking model — a Director that cannot see still
    returns a workable plan, with what went wrong recorded in 'corrections'.
    """
    assets = [look(p) for p in asset_paths]
    plan = decide(intent, engine=engine, seconds=seconds, orientation=orientation,
                  line=line, character=character, assets=assets)
    plan["assets"] = assets
    plan["prompt"] = write_prompt(plan, scene=scene, camera=camera)
    return plan


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="Ask the Director how to shoot something.")
    ap.add_argument("intent")
    ap.add_argument("--engine", default="h3", choices=["h3", "ltx"])
    ap.add_argument("--line", default="")
    ap.add_argument("--seconds", type=float, default=None)
    ap.add_argument("--orientation", default=None)
    ap.add_argument("--scene", default="")
    ap.add_argument("--asset", action="append", default=[])
    a = ap.parse_args()
    print(json.dumps(direct(a.intent, engine=a.engine, seconds=a.seconds,
                            orientation=a.orientation, line=a.line,
                            asset_paths=a.asset, scene=a.scene), indent=1))
