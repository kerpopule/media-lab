"""Director school: the craft rules a storyboard must pass before it is filmed.

A video model renders one shot at a time and forgets the last one. Everything
that makes a sequence read as one film has to be decided before the first
render and carried into every prompt: the continuity bible (who, wearing what,
where, when, in what light and palette, through which lens), the coverage
(shot sizes that change enough between cuts, screen sides that respect the
180-degree line), the dialogue budget (one line per shot that fits its
seconds, or the lips drift), and a transition chosen for each cut.

This module is deterministic and has no model calls:

* ``normalize_bible`` / ``normalize_beat``: the vocabulary.
* ``lint_board``: the exam. Findings with codes, severities and fixes, and a
  grade, for a storyboard or a director's shot list.
* ``choose_transition``: a motivated transition for each cut.
* ``route_engine``: which engine and variant a shot needs, with an estimate
  that includes spin-up.
* ``compose_h3_prompt``: the three-field H3 prompt with every continuity
  constant restated, screen sides, eyelines and a clean end state.
* ``cut_plan``: the stitch plan (``media_lab_core.stitch``) for the board.

``docs/DIRECTOR-SCHOOL.md`` is the human version.
"""
from __future__ import annotations

import re
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

# ------------------------------------------------------- the director's brief

# Media Lab's storyboard director (POST /api/storyboard, app.BOARD_SYS) and
# `tools/director brief` both plan with this prompt.
BOARD_SYS = """You are a film director breaking a story into shots for an AI video generator (LTX-2 or MiniMax H3). Both natively PERFORM spoken dialogue written in double quotes (with lip sync and voices) and sound effects — so dialogue belongs IN the prompts. Neither can paint readable lettering.

WORK IN TWO PASSES. FIRST read the user's whole brief and extract a STORY BIBLE — the constants every
shot must share. THEN write the beats against that bible.

Respond with ONLY a JSON object:
{"title": "short film title",
 "bible": {
   "style": "<ONE sentence naming medium, era, film stock or render look, lighting and mood — this sentence is pasted into EVERY shot>",
   "palette": "<the 3-5 colours that own the frame, e.g. 'sodium orange, teal shadows, off-white tile'>",
   "time_of_day": "<when, precisely: '2 a.m., rain outside'>",
   "characters": [{"name": "<exact name>", "look": "<canonical 30-50 word appearance line: age, build, hair, eyes, skin, distinguishing details — no camera words, no action>", "wardrobe": "<the exact clothes, colours included, worn in every shot unless the story changes them>", "voice": "<ONE canonical voice line, 10-25 words: sex, age, accent, pitch, pace, energy — e.g. 'warm American woman in her mid-30s, medium pitch, bright unhurried delivery'. Invent one if the user gave none and keep it for every shot.>"}],
   "world": "<setting, era and production-design constants shared by every shot>",
   "camera": "<the lens and camera-movement language used throughout, e.g. '35mm spherical lens, eye-level, slow deliberate dolly moves'>"},
 "beats": [{"title": "...", "description": "...", "scene": "<short scene slug, the same for every shot of one continuous place and time, e.g. 'INT. LAUNDROMAT - NIGHT'>", "shot_size": "<EWS|WS|FS|MWS|MS|MCU|CU|ECU|INSERT|OTS|TWO>", "angle": "<eye level|low angle|high angle|overhead|profile>", "screen_side": {"<bible name>": "<left|right|center>"}, "transition": "<how we arrive in this shot: cut|cut_on_action|match_cut|j_cut|l_cut|dissolve|fade_through_black>", "characters": ["<bible names present in THIS shot>"], "speaker": "<bible name of whoever SPEAKS in this shot (on camera OR narrating over it), or "">", "video_prompt": "...", "duration": "5"}]}

BIBLE RULES — these decide whether the clips match each other:
- If the user's brief already gives a cast list, character descriptions, an animation/style statement, or a world, PRESERVE THEIR WORDING VERBATIM in the bible. Copy their sentences across. Do not paraphrase, do not "improve", do not invent a replacement. Their wording IS the quality.
- Only invent a style, world, camera or character look when the user gave none — then commit to it and apply that same invention to every beat.
- Every character who appears anywhere in the story gets EXACTLY ONE entry in bible.characters, under ONE name. NEVER rename a character between beats, never redesign them, never give the same person two looks.
- bible.style, bible.world and bible.camera must be shot-agnostic: no per-scene action, no one-off props.
- bible fields describe what the CAMERA SEES — never the artifact or the edit. FORBIDDEN in style/world: "video", "short-form", "reel", "clip", "montage", "recipe video", "vlog format", and meta-instructions like "in every shot" or "always" — the model PAINTS those words as split-screen video collages. Translate the user's format intent into pure visual language (palette, light, lens, mood) and their every-shot rules into concrete descriptions repeated per beat.

BEAT RULES:
- If the user's brief contains a numbered or bulleted shot list, produce EXACTLY ONE beat per shot, in their order, keeping their shot text. Otherwise give 3 to 8 beats with a beginning, middle and end.
- If the user states a TOTAL runtime (e.g. "a two-minute film"), plan enough beats that the durations sum close to it (each beat is 3-12 seconds, up to 16 beats). If they gave only a few scenes for a longer runtime, invent the missing scenes in the same style so the whole runtime is covered — their scenes stay verbatim, in order.
- "title" is 2-5 words; "description" is one plain-English sentence for the storyboard card.
- "characters" lists the bible names VISIBLE ON SCREEN in that shot, spelled EXACTLY as in bible.characters. Use [] for a shot with nobody in it — insert shots, product shots, food close-ups, scenery. Someone merely narrating over the shot is NOT visible.
- Refer to cast characters BY NAME in every beat they appear in — never as "a woman", "the presenter", "the host". If the cast has exactly one person and the story has a single performer/presenter/narrator on camera, that performer IS the cast character: use their name.
- A shot with characters [] must SAY so in the video_prompt: open with the camera framing (e.g. "Top-down close-up." / "Extreme close-up, hands only.") and include "no people visible" or "hands only" so the camera stays on the subject.
- ONE atomic physical action per beat. "Pour the butter, spread it, then add onions" is three beats, not one — multi-step actions in a single shot come out as physics soup.
DIRECTOR'S GRAMMAR — coverage and cutting. Each clip is filmed alone and the model forgets the last one, so the cut only works if you plan it:
- Open every new place with a WS or EWS that shows where we are; move in (MS, then MCU/CU) for the emotional beats; come back out wide to end or to reset. Put that size in "shot_size" and say it first in the video_prompt.
- Two consecutive shots of the same people must differ by at least two sizes (WS to MCU, not MS to MCU) or by a clearly different angle; otherwise the cut reads as a jump cut. An INSERT (hands, an object) is the classic cutaway between them.
- ONE speaker per shot and ONE line per shot, at most about 2.5 words per second of the shot's duration minus one second. Cover a conversation with singles or over-the-shoulder shots, one per line, alternating.
- The 180-degree rule: once a character is screen-left they stay screen-left for the whole scene. Record it in "screen_side" for every shot they are in, and write it in the prompt ("Maya on the left of frame").
- Eyelines: characters look at each other or at the action, never into the lens, unless the brief is a presenter talking to camera. Write "not looking at the camera" in their shots.
- "transition" is how we arrive in the shot: "cut" by default; "cut_on_action" when a movement carries across the cut; "j_cut" when this shot opens on a reply to the previous line (we hear it a beat early); "l_cut" when the previous line finishes over this shot's reaction; "match_cut" when shapes or motion rhyme; "dissolve" ONLY when time passes or the place changes; "fade_through_black" only for a large time jump or the very end. Never dissolve inside one continuous moment.
- "scene" is identical for every shot of one continuous place and time; change it only when the place or time changes.
- No legible lettering: never ask for signs, logos, captions or words to appear — the model paints gibberish. Keep any sign out of focus, turned away or out of frame.
- Pacing: most shots run 3-6 seconds; a silent shot longer than 6 seconds must have a camera move or an action that earns it.

CRAFT RULES for every video_prompt — the model renders these reliably; break them and the shot comes out wrong. These rules OUTRANK the brief's wording: translate conflicting requests (crowds -> 1-2 faces + faceless background figures; fast cameras -> smooth decisive moves + more, shorter beats) instead of obeying them literally:
- Short declarative sentences, one idea each. Present tense, concrete camera verbs (dolly in, pan, track, push-in).
- ONE pair of hands in any close-up. ONE utensil or container in motion. ONE pour/sprinkle/cut at a time — never "salt and pepper" pouring together (the model fuses the shakers), never two hands from different people, never two simultaneous streams.
- Name the target's STARTING state: "pours the sauce into the empty glass dish", not "the dish of sauce". Describing the finished state alongside the action makes the model render both at once.
- At most TWO people with visible faces per shot; groups appear from behind, in silhouette, or cropped below the shoulders.
- Motion at a natural, deliberate pace — never "frantic" or "rapid"; fast motion tears the image.
- Kill the plastic look: include "shot on a 35mm lens, raw footage, subtle film grain, natural skin texture, 180-degree shutter, natural motion blur" and ONE coherent light source per shot. Never "smooth", "flawless" or "perfect" for skin or hands.
- EXACTLY ONE pair of hands, belonging to one unseen person, in every hands-only shot — never a second person, never a second pair of hands entering.
- "video_prompt" is that one continuous shot — 40-150 words, present tense: subject, action, setting, camera move, lighting.
- Write ONLY what happens in this shot. The style sentence, the world constants and each character's look line are attached to every prompt automatically, so do NOT restate them.
- PRESERVE THE USER'S OWN WORDS. If they wrote the shot, keep their description and dialogue verbatim; split only at natural cut points.
- Dialogue: include the exact spoken words in double quotes with speaker and delivery, e.g.: He turns, smirks, and says in a mocking deep voice: "I am big mad." The model performs quoted lines aloud.
- "speaker" is whoever performs the shot's spoken words — INCLUDING narration over a shot they are not visible in (a cooking step voiced by the host is speaker: host, characters: []). Use "" only for a truly silent shot. The same narrator keeps the same speaker across every shot they voice; their bible voice line is attached automatically, so the voice never changes mid-film.
- "duration": whole seconds 3-12, as a string. If the user says how long a scene runs, use THEIR number (clamped to 3-12). Otherwise: "12" if the beat carries more than one spoken line, "8" for one spoken line or complex action, "5" for everything else.
Return ONLY the JSON object, no markdown fences, no commentary."""


# ------------------------------------------------------------- vocabulary

SHOT_SIZES = ["EWS", "WS", "FS", "MWS", "MS", "MCU", "CU", "ECU"]
EXTRA_SIZES = {"INSERT": 7, "OTS": 4, "TWO": 4}
SIZE_RANK = {name: i for i, name in enumerate(SHOT_SIZES)} | EXTRA_SIZES
SIZE_SYNONYMS = {
    "extreme wide": "EWS", "establishing": "EWS", "ews": "EWS", "els": "EWS",
    "wide": "WS", "long": "WS", "ws": "WS", "ls": "WS",
    "full": "FS", "fs": "FS",
    "medium wide": "MWS", "medium-wide": "MWS", "cowboy": "MWS", "mws": "MWS", "mls": "MWS",
    "medium": "MS", "mid": "MS", "ms": "MS", "waist": "MS",
    "medium close": "MCU", "medium close-up": "MCU", "medium closeup": "MCU", "mcu": "MCU",
    "close": "CU", "close-up": "CU", "closeup": "CU", "cu": "CU",
    "extreme close": "ECU", "extreme close-up": "ECU", "ecu": "ECU", "macro": "ECU",
    "insert": "INSERT", "detail": "INSERT", "cutaway": "INSERT",
    "over the shoulder": "OTS", "over-the-shoulder": "OTS", "ots": "OTS",
    "two shot": "TWO", "two-shot": "TWO", "2-shot": "TWO", "two": "TWO",
}
SIZE_PHRASE = {
    "EWS": "Extreme wide establishing shot, the people small in the frame",
    "WS": "Wide shot, full figures and the space around them",
    "FS": "Full shot, head to toe",
    "MWS": "Medium-wide shot from the knees up",
    "MS": "Medium shot from the waist up",
    "MCU": "Medium close-up, head and chest",
    "CU": "Close-up on the face, eyes on the upper third of the frame",
    "ECU": "Extreme close-up",
    "INSERT": "Insert close-up of the object, hands only, no faces",
    "OTS": "Over-the-shoulder shot, the near person's shoulder soft in the foreground",
    "TWO": "Medium two-shot, both people in frame",
}
ANGLES = {"eye level", "low angle", "high angle", "overhead", "dutch", "profile", "three-quarter"}
IDENTITY_SIZES = {"MCU", "CU", "ECU"}
WIDE_SIZES = {"EWS", "WS", "FS"}

FORMAT_WORDS = re.compile(r"\b(video|reel|montage|vlog|short-?form|clip|split[- ]screen|collage)\b", re.IGNORECASE)
# Lettering the model will be asked to paint: quoted signage or ALL-CAPS brand words.
SIGNAGE = re.compile(r"\b(sign|logo|banner|neon letters|lettering|headline|caption|text reading|reads)\b", re.IGNORECASE)
CAPS_WORDS = re.compile(r"\b[A-Z]{3,}(?:\s+[A-Z]{3,})+\b")
QUOTE = re.compile(r"\"([^\"]{1,400})\"|“([^”]{1,400})”")

# Measured Media Lab timings (receipts: wave2 singularity eval, r14 rollout).
ENGINE_TIMINGS = {
    "sol-t2va": {"warm_s": 75, "switch_s": 0, "spin_up_s": 320, "max_seconds": 5.04,
                 "label": "Sol-H3 (default)"},
    "sol-fl2va": {"warm_s": 80, "switch_s": 320, "spin_up_s": 320, "max_seconds": 5.04,
                  "label": "Sol-H3 from a start frame"},
    "real-long": {"warm_s": 285, "warm_ref_s": 490, "spin_up_s": 460, "switch_s": 460,
                  "per_extra_second_s": 78, "max_seconds": 15.1, "label": "Real / Long (Singularity)"},
}

SEVERITY_POINTS = {"error": 15, "warn": 5, "info": 0}


def _norm(text: Any) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text or "").lower())


def shot_size(value: Any) -> str | None:
    """Canonical shot size from anything a model or a person writes."""
    raw = str(value or "").strip()
    if not raw:
        return None
    upper = raw.upper().replace(" ", "")
    if upper in SIZE_RANK:
        return upper
    low = raw.lower().replace("_", " ")
    for phrase in sorted(SIZE_SYNONYMS, key=len, reverse=True):
        if re.search(r"(^|\b)" + re.escape(phrase) + r"(\b|$)", low):
            return SIZE_SYNONYMS[phrase]
    return None


def infer_shot_size(prompt: str) -> str | None:
    """Best-effort size from prompt wording, for boards made before shot sizes."""
    return shot_size(prompt[:160])


# ------------------------------------------------------------------- bible

def normalize_bible(raw: Mapping[str, Any] | None) -> dict[str, Any]:
    """The continuity bible, accepting the app's older keys (world/camera)."""
    raw = dict(raw or {})
    chars = []
    for c in raw.get("characters") or []:
        if isinstance(c, str):
            c = {"name": c}
        if not isinstance(c, Mapping) or not str(c.get("name") or "").strip():
            continue
        chars.append({
            "name": str(c["name"]).strip()[:80],
            "look": str(c.get("look") or c.get("appearance") or "").strip()[:900],
            "wardrobe": str(c.get("wardrobe") or "").strip()[:400],
            "voice": str(c.get("voice") or "").strip()[:300],
            "reference": str(c.get("reference") or c.get("reference_image") or c.get("still") or "").strip()[:500],
            "char_id": c.get("char_id"),
        })
    return {
        "style": str(raw.get("style") or "").strip()[:900],
        "palette": str(raw.get("palette") or "").strip()[:300],
        "location": str(raw.get("location") or raw.get("world") or "").strip()[:900],
        "time_of_day": str(raw.get("time_of_day") or "").strip()[:200],
        "lighting": str(raw.get("lighting") or "").strip()[:400],
        "lens": str(raw.get("lens") or raw.get("camera") or "").strip()[:600],
        "characters": chars[:12],
    }


def dialogue_lines(beat: Mapping[str, Any]) -> list[dict[str, str]]:
    """Spoken lines of a beat: explicit ``dialogue`` rows or quoted prompt text."""
    rows = []
    for row in beat.get("dialogue") or []:
        if isinstance(row, Mapping) and str(row.get("line") or "").strip():
            rows.append({"speaker": str(row.get("speaker") or beat.get("speaker") or "").strip(),
                         "line": str(row["line"]).strip()})
    if rows:
        return rows
    speaker = str(beat.get("speaker") or "").strip()
    for m in QUOTE.finditer(str(beat.get("video_prompt") or beat.get("prompt") or "")):
        rows.append({"speaker": speaker, "line": (m.group(1) or m.group(2) or "").strip()})
    return rows


def normalize_beat(beat: Mapping[str, Any], index: int = 0) -> dict[str, Any]:
    prompt = str(beat.get("video_prompt") or beat.get("prompt") or "").strip()
    size = shot_size(beat.get("shot_size")) or infer_shot_size(prompt)
    chars = beat.get("characters")
    if chars is None:
        chars = beat.get("cast") or []
    sides = beat.get("screen_side") or beat.get("screen_sides") or {}
    if not isinstance(sides, Mapping):
        sides = {}
    try:
        seconds = float(beat.get("duration") or beat.get("seconds") or 5)
    except (TypeError, ValueError):
        seconds = 5.0
    transition = beat.get("transition") or beat.get("transition_in") or ""
    if isinstance(transition, Mapping):
        transition = transition.get("kind") or ""
    return {
        "normalized": True,
        "index": index,
        "title": str(beat.get("title") or f"Shot {index + 1}")[:120],
        "prompt": prompt,
        "shot_size": size,
        "angle": str(beat.get("angle") or "").strip().lower()[:40],
        "characters": [str(c) for c in chars if str(c or "").strip()],
        "speaker": str(beat.get("speaker") or "").strip(),
        "dialogue": dialogue_lines(beat),
        "screen_side": {str(k): str(v).lower() for k, v in sides.items() if str(v).lower() in {"left", "right", "center"}},
        "scene": str(beat.get("scene") or "").strip()[:80],
        "seconds": seconds,
        "transition": str(transition or "").strip().lower().replace(" ", "_").replace("-", "_"),
        "camera_move": str(beat.get("camera_move") or "").strip()[:160],
        "motion_out": bool(beat.get("motion_out")),
        "match": str(beat.get("match") or "").strip()[:160],
        "references": list(beat.get("references") or []),
        "start_frame": beat.get("start_frame") or beat.get("still_url"),
    }


# -------------------------------------------------------------------- exam

def _finding(code: str, severity: str, beat: int | None, message: str, fix: str) -> dict[str, Any]:
    return {"code": code, "severity": severity, "shot": (beat + 1) if beat is not None else None,
            "message": message, "fix": fix}


def words_per_second_budget(seconds: float) -> float:
    """Words a shot can carry and still land the lips: ~2.5 w/s after a
    0.4 s breath at the head and a 0.4 s settle at the tail."""
    return max(0.0, (float(seconds) - 0.8) * 2.5)


def lint_board(board: Mapping[str, Any], *, references_expected: bool = True) -> dict[str, Any]:
    """Grade a storyboard or shot list against the director rules."""
    bible = normalize_bible(board.get("bible"))
    beats = [normalize_beat(b, i) for i, b in enumerate(board.get("beats") or board.get("shots") or [])]
    findings: list[dict[str, Any]] = []
    for key, label in (("location", "where we are"), ("time_of_day", "time of day"),
                       ("palette", "the colour palette"), ("lens", "lens and camera language")):
        if not bible.get(key):
            findings.append(_finding("BIBLE_GAP", "warn", None, f"The bible does not pin down {label}.",
                                     f"Add bible.{key}; it is pasted into every shot so the look cannot drift."))
    char_by_name = {_norm(c["name"]): c for c in bible["characters"]}
    for c in bible["characters"]:
        if not c["look"]:
            findings.append(_finding("CHARACTER_UNDEFINED", "error", None, f"{c['name']} has no look line.",
                                     "Write a 30-50 word look: age, build, face, hair, skin, distinguishing marks."))
        if not c["wardrobe"] and not re.search(r"\b(wear|wearing|shirt|jacket|dress|coat|sweater|hoodie|uniform)",
                                                c["look"], re.IGNORECASE):
            findings.append(_finding("WARDROBE_UNDEFINED", "warn", None, f"{c['name']} has no fixed wardrobe.",
                                     "Name the exact clothes once in the bible; wardrobe drift is the first thing viewers notice."))
    if not board.get("seed"):
        findings.append(_finding("NO_SEED", "warn", None, "No shared seed for the film.",
                                 "Pin one seed for every shot so grain, colour and faces start from the same place."))
    for b in beats:
        i = b["index"]
        visible = b["characters"]
        size = b["shot_size"]
        if not size:
            findings.append(_finding("NO_SHOT_SIZE", "warn", i, "No shot size.",
                                     "Give every shot a size (EWS/WS/MS/MCU/CU/ECU/INSERT/OTS) so coverage can be checked."))
        if len(visible) > 2:
            sev = "warn" if size in WIDE_SIZES else "error"
            findings.append(_finding("TOO_MANY_FACES", sev, i, f"{len(visible)} people with faces in one shot.",
                                     "Two faces at most. Show a group from behind or out of focus, or split into singles."))
        lines = b["dialogue"]
        speakers = {(_norm(row["speaker"]) or "?") for row in lines}
        if len(speakers) > 2 or (len(speakers) == 2 and b["seconds"] <= 6):
            findings.append(_finding("TOO_MANY_SPEAKERS", "error", i,
                                     f"{len(speakers)} speakers in a {b['seconds']:.0f}-second shot.",
                                     "One speaker per shot. Cover an exchange with singles or over-the-shoulders and J/L cuts."))
        words = sum(len(row["line"].split()) for row in lines)
        budget = words_per_second_budget(b["seconds"])
        if words and words > budget:
            findings.append(_finding("DIALOGUE_TOO_LONG", "error", i,
                                     f"{words} words in {b['seconds']:.1f}s (budget {budget:.0f}).",
                                     "Shorten the line or lengthen the shot; crammed lines are where lip-sync breaks."))
        if FORMAT_WORDS.search(b["prompt"]):
            findings.append(_finding("FORMAT_WORDS", "error", i, "The prompt uses format words (video/clip/montage...).",
                                     "Describe what the camera sees; the model paints format words as panels and captions."))
        if SIGNAGE.search(b["prompt"]) or CAPS_WORDS.search(b["prompt"]):
            findings.append(_finding("LEGIBLE_TEXT", "warn", i, "The shot asks the model to paint lettering.",
                                     "Generated lettering garbles ('WAFITE HOUSS'). Keep signs soft or turned away; add titles in the edit."))
        if not b["dialogue"] and b["seconds"] > 8:
            findings.append(_finding("SHOT_DRAGS", "warn", i, f"A silent {b['seconds']:.0f}-second shot.",
                                     "Silent shots rarely hold past 6 s; split it or give it a move."))
        if references_expected and size in IDENTITY_SIZES:
            for name in visible:
                c = char_by_name.get(_norm(name))
                if c is not None and not c["reference"] and not b["references"] and not b["start_frame"]:
                    findings.append(_finding("IDENTITY_UNANCHORED", "warn", i,
                                             f"{name} is in a {size} with no reference picture or start frame.",
                                             "Identity-critical: film it from a start frame built from the character's reference, or on Real/Long."))
                    break
    # sequence rules
    scenes = _scene_labels(beats)
    for k in range(1, len(beats)):
        a, b = beats[k - 1], beats[k]
        same_scene = scenes[k] == scenes[k - 1]
        trans = b["transition"] or "cut"
        if same_scene and trans in {"dissolve", "fade_through_black", "crossfade"}:
            findings.append(_finding("UNMOTIVATED_DISSOLVE", "warn", k,
                                     "A dissolve inside one continuous scene.",
                                     "Dissolves say 'time passed'. Inside a scene, cut (on action, or J/L on dialogue)."))
        if not same_scene:
            continue
        if (a["shot_size"] and b["shot_size"] and a["characters"] and set(map(_norm, a["characters"])) == set(map(_norm, b["characters"]))
                and abs(SIZE_RANK[a["shot_size"]] - SIZE_RANK[b["shot_size"]]) < 2
                and (a["angle"] or "eye level") == (b["angle"] or "eye level")
                and trans not in {"match_cut"}):
            findings.append(_finding("JUMP_CUT", "warn", k,
                                     f"{a['shot_size']} to {b['shot_size']} on the same people from the same angle.",
                                     "Change the size by two steps or the angle by 30 degrees or more, or cut away to an insert."))
        for name, side in b["screen_side"].items():
            prev = a["screen_side"].get(name)
            if prev and side != prev and side != "center" and prev != "center":
                both = [n for n in b["screen_side"] if n in a["screen_side"]]
                if len(both) >= 2:
                    findings.append(_finding("AXIS_BREAK", "error", k,
                                             f"{name} jumps from screen {prev} to screen {side}.",
                                             "Keep everyone on their side of the 180-degree line; cross it only on screen, with a move."))
                    break
    if beats and beats[0]["shot_size"] and beats[0]["shot_size"] not in WIDE_SIZES \
            and not any(b["shot_size"] in WIDE_SIZES for b in beats[:3]):
        findings.append(_finding("NO_ESTABLISHING", "warn", 0, "The film never establishes where we are.",
                                 "Open (or return early) on a wide shot of the location."))
    penalty = sum(SEVERITY_POINTS[f["severity"]] for f in findings)
    score = max(0, 100 - penalty)
    grade = "A" if score >= 90 else "B" if score >= 80 else "C" if score >= 70 else "D" if score >= 55 else "F"
    return {"grade": grade, "score": score, "shots": len(beats),
            "errors": sum(1 for f in findings if f["severity"] == "error"),
            "warnings": sum(1 for f in findings if f["severity"] == "warn"),
            "findings": findings}


def _scene_labels(beats: Sequence[Mapping[str, Any]], *, from_transitions: bool = False) -> list[str]:
    """Scene of each beat. Explicit ``scene`` labels win. Without labels the
    exam assumes one continuous scene (so a dissolve inside it is flagged);
    the cut plan may instead read a dissolve as the start of a new scene."""
    labels, current, count = [], "scene1", 1
    for i, b in enumerate(beats):
        if b.get("scene"):
            current = b["scene"]
        elif from_transitions and i and b.get("transition") in {"dissolve", "fade_through_black"}:
            count += 1
            current = f"scene{count}"
        labels.append(current)
    return labels


# -------------------------------------------------------------- transitions

def choose_transition(prev: Mapping[str, Any] | None, beat: Mapping[str, Any], *,
                      same_scene: bool = True) -> dict[str, Any]:
    """A transition with a reason. The director's explicit choice wins when valid."""
    b = beat if beat.get("normalized") else normalize_beat(beat)
    explicit = b.get("transition")
    valid = {"cut", "cut_on_action", "match_cut", "smash_cut", "j_cut", "l_cut", "dissolve", "fade_through_black"}
    if explicit in valid:
        return {"kind": explicit, "why": "the director's choice"}
    if prev is None:
        return {"kind": "cut", "why": "opening shot"}
    a = prev if prev.get("normalized") else normalize_beat(prev)
    if not same_scene:
        return {"kind": "dissolve", "why": "new place or time: a dissolve tells the viewer time has passed"}
    if b.get("match"):
        return {"kind": "match_cut", "why": f"graphic match: {b['match']}"}
    b_lines, a_lines = b["dialogue"], a["dialogue"]
    b_speaker = _norm(b_lines[0]["speaker"]) if b_lines else ""
    a_speaker = _norm(a_lines[-1]["speaker"]) if a_lines else ""
    if b_lines and a_lines and b_speaker and b_speaker != a_speaker:
        return {"kind": "j_cut", "audio_offset_frames": -8,
                "why": "the reply is heard a beat before we see who says it"}
    if a_lines and not b_lines:
        return {"kind": "l_cut", "audio_offset_frames": 8,
                "why": "the line finishes over the listener's reaction"}
    if a.get("motion_out") or re.search(r"\b(turns|stands|sits|reaches|opens|throws|walks|lifts|slides)\b",
                                        a["prompt"][-160:], re.IGNORECASE):
        return {"kind": "cut_on_action", "why": "cut inside the movement so the eye follows it"}
    return {"kind": "cut", "why": "straight cut"}


# ------------------------------------------------------------------ routing

def estimate_seconds(variant: str, seconds: float, *, warm: bool, references: int = 0,
                     switch: bool = False) -> int:
    """Wall-clock estimate for one shot, spin-up included when the engine is cold."""
    t = ENGINE_TIMINGS[variant]
    if variant == "real-long":
        base = t["warm_ref_s"] if references else t["warm_s"]
        base += max(0.0, seconds - 5.2) * t["per_extra_second_s"]
    else:
        base = t["warm_s"]
    if not warm:
        base += t["spin_up_s"]
    elif switch:
        base += t["switch_s"]
    return int(round(base))


def route_engine(beat: Mapping[str, Any], bible: Mapping[str, Any] | None = None, *,
                 real_long_available: bool = False, warm_variant: str | None = "sol-t2va") -> dict[str, Any]:
    """Which engine a shot needs.

    Sol-H3 is the default: fast, warm, 5-second shots. A shot whose identity
    matters (a named person in MCU/CU/ECU, or a character with a reference
    picture) needs anchoring: Real/Long when it is installed (references +
    up to 15 s), otherwise Sol-H3 from a start frame built from the reference.
    A shot longer than Sol's 5 s either goes to Real/Long or is split.
    """
    b = beat if beat.get("normalized") else normalize_beat(beat)
    bible = normalize_bible(bible)
    by_name = {_norm(c["name"]): c for c in bible["characters"]}
    refs = [by_name[_norm(n)]["reference"] for n in b["characters"]
            if _norm(n) in by_name and by_name[_norm(n)]["reference"]]
    identity = bool(b["characters"]) and (b["shot_size"] in IDENTITY_SIZES or bool(refs))
    long_shot = b["seconds"] > ENGINE_TIMINGS["sol-t2va"]["max_seconds"] + 0.2
    if (identity and refs or long_shot) and real_long_available:
        variant = "real-long"
        why = ("identity-critical shot with references" if identity else "longer than a Sol shot") + ": Real/Long"
    elif identity or b["start_frame"]:
        variant = "sol-fl2va"
        why = "identity-critical: Sol-H3 from a start frame that carries the reference face and set"
    else:
        variant = "sol-t2va"
        why = "no identity at stake: Sol-H3 text-to-video"
    notes = []
    if long_shot and variant != "real-long":
        notes.append(f"{b['seconds']:.1f}s is longer than Sol's 5 s; split the shot or install Real/Long")
    warm = warm_variant is not None
    switch = warm and warm_variant != variant
    return {"variant": variant, "label": ENGINE_TIMINGS[variant]["label"], "why": why,
            "references": refs, "notes": notes,
            "estimate_s": estimate_seconds(variant, min(b["seconds"], ENGINE_TIMINGS[variant]["max_seconds"]),
                                           warm=warm, references=len(refs), switch=switch)}


# ------------------------------------------------------------------ prompts

def _sentence(text: str) -> str:
    text = str(text or "").strip()
    if text and not text.endswith((".", "!", "?")):
        text += "."
    return text


SPEECH_LEAD = re.compile(r"[^.!?\"\u201c]*\b(?:says|asks|shouts|whispers|replies|adds|answers)\b[^.!?\"\u201c]*[:,]?\s*(?=[\"\u201c])",
                         re.IGNORECASE)


def compose_h3_prompt(bible: Mapping[str, Any], beat: Mapping[str, Any], *,
                      soundscape: str | None = None, music: str | None = None,
                      start_frame: bool = False) -> str:
    """The H3 three-field prompt with every continuity constant restated."""
    bible = normalize_bible(bible)
    b = beat if beat.get("normalized") else normalize_beat(beat)
    parts: list[str] = []
    if start_frame:
        parts.append("The first frame is the given image; keep its people, clothes, set and light exactly.")
    if b["shot_size"]:
        size = SIZE_PHRASE[b["shot_size"]]
        parts.append(_sentence(size + (f", {b['angle']}" if b["angle"] else "")))
    for key in ("location", "time_of_day", "lighting"):
        if bible[key]:
            parts.append(_sentence(bible[key]))
    by_name = {_norm(c["name"]): c for c in bible["characters"]}
    speakers: dict[str, str] = {}
    for n in b["characters"]:
        c = by_name.get(_norm(n))
        if not c:
            continue
        look = c["look"]
        if c["wardrobe"] and c["wardrobe"].lower() not in look.lower():
            look = f"{look.rstrip('.')}; wearing {c['wardrobe']}"
        parts.append(_sentence(f"{c['name']} is {look}" if not look.lower().startswith(c["name"].lower()) else look))
    sides = [f"{name} is on the {side} of the frame" for name, side in b["screen_side"].items() if side != "center"]
    if sides:
        parts.append(_sentence("; ".join(sides)))
    # the line is re-sent as a <d> block below; drop the words that introduced it
    # ("She says in a calm, weary voice: ...") or the model reads a dangling "says:"
    action = SPEECH_LEAD.sub("", b["prompt"])
    action = QUOTE.sub("", action).strip()
    action = re.sub(r"\b(says|asks|shouts|whispers|replies)\s*[:,]?\s*$", "", action, flags=re.IGNORECASE).strip()
    action = re.sub(r"\s{2,}", " ", action)
    if action:
        parts.append(_sentence(action))
    for row in b["dialogue"]:
        who = row["speaker"] or (b["characters"][0] if b["characters"] else "")
        if who not in speakers:
            speakers[who] = f"(S{len(speakers) + 1})"
        tag = speakers[who]
        c = by_name.get(_norm(who))
        if c:
            parts.append(f"{tag} is {c['name']}.")
        parts.append(f"{tag} says <d>[English] {row['line']}</d>")
    if b["characters"]:
        if len(b["characters"]) > 1:
            parts.append("They look at each other, never into the camera.")
        else:
            parts.append("Eyes stay off the lens; never look into the camera.")
    if b["camera_move"]:
        parts.append(_sentence(b["camera_move"]))
    if b["dialogue"]:
        parts.append("When the line ends the lips close and the face holds a natural expression.")
    for key in ("style", "palette", "lens"):
        if bible[key]:
            parts.append(_sentence(bible[key]))
    parts.append("One continuous shot. No on-screen text, captions, logos or lettering.")
    body = " ".join(p for p in parts if p)
    sound = soundscape or ("natural ambient sound matching the scene; realistic physical sounds of the visible actions"
                           + ("; clear close dialogue" if b["dialogue"] else ""))
    return (f"integrated_multimodal_description: [Shot 1] {body}\n\n"
            f"overall_soundscape: {sound}\n\n"
            f"non_diegetic_music: {music or 'N/A'}")


def still_prompt(bible: Mapping[str, Any], beat: Mapping[str, Any]) -> str:
    """The start-frame picture for a shot: the scene built in the still."""
    bible = normalize_bible(bible)
    b = beat if beat.get("normalized") else normalize_beat(beat)
    parts = []
    if b["shot_size"]:
        parts.append(SIZE_PHRASE[b["shot_size"]])
    by_name = {_norm(c["name"]): c for c in bible["characters"]}
    for n in b["characters"]:
        c = by_name.get(_norm(n))
        if c:
            look = c["look"] + (f"; wearing {c['wardrobe']}" if c["wardrobe"] and c["wardrobe"].lower() not in c["look"].lower() else "")
            parts.append(f"{c['name']}: {look}")
    sides = [f"{name} on the {side} of the frame" for name, side in b["screen_side"].items() if side != "center"]
    if sides:
        parts.append(", ".join(sides))
    parts.append(QUOTE.sub("", b["prompt"]).strip())
    for key in ("location", "time_of_day", "lighting", "style", "palette", "lens"):
        if bible[key]:
            parts.append(bible[key])
    parts.append("one single continuous photographic frame, nobody looking into the camera, "
                 "absolutely no on-screen text, no lettering, no panels")
    return ". ".join(_sentence(p).rstrip(".") for p in parts if p) + "."


# ---------------------------------------------------------------- cut plan

def cut_plan(board: Mapping[str, Any], clips: Sequence[str], *, width: int, height: int,
             fps: int = 24, quality: str = "high", music: str | None = None) -> dict[str, Any]:
    """A ``media_lab_core.stitch`` plan with a transition chosen for every cut."""
    beats = [normalize_beat(b, i) for i, b in enumerate(board.get("beats") or board.get("shots") or [])]
    if len(beats) != len(clips):
        raise ValueError("one clip per shot is required")
    scenes = _scene_labels(beats, from_transitions=True)
    shots = []
    for k, (b, clip) in enumerate(zip(beats, clips)):
        prev = beats[k - 1] if k else None
        trans = choose_transition(prev, b, same_scene=(k == 0 or scenes[k] == scenes[k - 1]))
        shot = {"path": str(clip), "id": b["title"], "scene": scenes[k],
                "dialogue": bool(b["dialogue"]), "target_seconds": b["seconds"],
                "transition_in": {"kind": trans["kind"], "why": trans["why"],
                                  **({"audio_offset_frames": trans["audio_offset_frames"]}
                                     if "audio_offset_frames" in trans else {})}}
        shots.append(shot)
    plan = {"schema": "media_lab.director_cut.v1", "shots": shots, "width": width, "height": height,
            "fps": fps, "quality": quality, "fade_in_frames": 6, "fade_out_frames": 18}
    if beats and beats[0]["transition"] in {"fade_in", "fade_from_black", "fade_up"}:
        plan["fade_in_frames"] = 12
    if music:
        plan["music"] = {"path": music, "gain_db": 0.0, "beat_align": True}
        plan["clip_audio"] = False
    return plan


def plan_summary(board: Mapping[str, Any], *, real_long_available: bool = False) -> dict[str, Any]:
    """The whole pre-production packet: exam, routes and an estimate."""
    beats = [normalize_beat(b, i) for i, b in enumerate(board.get("beats") or board.get("shots") or [])]
    exam = lint_board(board)
    routes = []
    warm = "sol-t2va"
    total = 0
    for b in beats:
        r = route_engine(b, board.get("bible"), real_long_available=real_long_available, warm_variant=warm)
        warm = r["variant"]
        total += r["estimate_s"]
        routes.append({"shot": b["index"] + 1, **r})
    return {"exam": exam, "routes": routes, "estimate_s": total,
            "estimate_minutes": round(total / 60.0, 1)}


def iter_findings(exam: Mapping[str, Any], severity: str | None = None) -> Iterable[Mapping[str, Any]]:
    for f in exam.get("findings") or []:
        if severity is None or f["severity"] == severity:
            yield f
