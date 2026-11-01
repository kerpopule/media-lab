"""Deterministic overlap removal, lyric timing, and screenshot-video assembly.

The vision model extracts literal blocks; this module deliberately owns the parts
that must not be left to a generative model: adjacent-screenshot deduplication,
cue calculation, and pixel-preserving FFmpeg assembly.
"""
from __future__ import annotations

import json
import math
import re
import subprocess
from difflib import SequenceMatcher
from pathlib import Path
from typing import Iterable, Sequence


def clean_blocks(blocks: Iterable[object]) -> list[str]:
    """Normalize OCR container noise without changing visible wording."""
    out: list[str] = []
    for value in blocks:
        if isinstance(value, dict):
            value = value.get("text", "")
        text = str(value or "").replace("\r\n", "\n").replace("\r", "\n").strip()
        text = re.sub(r"[ \t]+", " ", text)
        text = re.sub(r"\n{3,}", "\n\n", text)
        if text:
            out.append(text)
    return out


def normalized_block(text: str) -> str:
    text = str(text or "").casefold()
    text = text.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    return " ".join(re.findall(r"[\w']+", text, flags=re.UNICODE))


def _same_boundary_block(left: str, right: str) -> bool:
    a, b = normalized_block(left), normalized_block(right)
    if not a or not b:
        return False
    if a == b:
        return True
    # OCR can vary one character between overlapping screenshots. Only allow a
    # fuzzy single-block match when it is long and nearly identical; short
    # messages such as "yes" may legitimately occur again and are exact-only.
    if min(len(a), len(b)) < 12:
        return False
    return SequenceMatcher(None, a, b, autojunk=False).ratio() >= 0.96


def boundary_overlap(previous: Sequence[str], current: Sequence[str], max_blocks: int = 20) -> int:
    """Largest suffix/prefix overlap between adjacent screenshots.

    This is intentionally not global deduplication. A later repeated message is
    preserved unless it occurs exactly at the boundary created by overlapping
    screenshots.
    """
    limit = min(len(previous), len(current), max_blocks)
    for size in range(limit, 0, -1):
        left, right = previous[-size:], current[:size]
        if all(_same_boundary_block(a, b) for a, b in zip(left, right)):
            return size
    return 0


def merge_screenshot_blocks(screenshots: Sequence[Sequence[object]]) -> dict:
    merged: list[str] = []
    rows: list[dict] = []
    for index, raw in enumerate(screenshots):
        blocks = clean_blocks(raw)
        overlap = boundary_overlap(merged, blocks) if merged else 0
        unique = blocks[overlap:]
        merged.extend(unique)
        rows.append({
            "index": index,
            "blocks": blocks,
            "unique_blocks": unique,
            "unique_text": "\n".join(unique).strip(),
            "overlap_removed": overlap,
            "included": bool(unique),
        })
    return {"screenshots": rows, "blocks": merged, "lyrics": "\n".join(merged).strip()}


_SMALL_NUMBERS = {
    0: "zero", 1: "one", 2: "two", 3: "three", 4: "four", 5: "five",
    6: "six", 7: "seven", 8: "eight", 9: "nine", 10: "ten",
    11: "eleven", 12: "twelve", 13: "thirteen", 14: "fourteen",
    15: "fifteen", 16: "sixteen", 17: "seventeen", 18: "eighteen",
    19: "nineteen", 20: "twenty", 30: "thirty", 40: "forty",
    50: "fifty", 60: "sixty", 70: "seventy", 80: "eighty", 90: "ninety",
}


def _spoken_number_tokens(token: str) -> list[str]:
    """Normalize simple written numbers to how Whisper commonly transcribes them."""
    if not token.isdecimal():
        return [token]
    value = int(token)
    if value in _SMALL_NUMBERS:
        return [_SMALL_NUMBERS[value]]
    if 20 < value < 100:
        tens, ones = divmod(value, 10)
        return [_SMALL_NUMBERS[tens * 10], _SMALL_NUMBERS[ones]]
    return [token]


def lyric_tokens(text: str) -> list[str]:
    raw = re.findall(r"[\w']+", str(text or "").casefold(), flags=re.UNICODE)
    return [spoken for token in raw for spoken in _spoken_number_tokens(token)]


def auto_music_seconds(text: str, block_count: int | None = None) -> int:
    """Readable default runtime for literal text, rounded to a musical five seconds.

    1.65 words/second is deliberately slower than ordinary speech so Music 3 can
    articulate prose instead of racing it. Per-block and intro/outro allowances
    become instrumental breathing room; they never duplicate or invent lyrics.
    Empty/freeform song ideas retain a useful 90-second default.
    """
    words = len(lyric_tokens(text))
    if not words:
        return 90
    if block_count is None:
        block_count = len([part for part in re.split(r"\n\s*\n", str(text).strip())
                           if part.strip()])
    blocks = max(1, int(block_count or 1))
    raw = words / 1.65 + blocks * 1.5 + 12.0
    return max(30, min(180, int(math.ceil(raw / 5.0) * 5)))


def song_first_word_budget(source_text: str) -> int:
    """How much reviewed prose to retain when musicality is the priority."""
    words = len(lyric_tokens(source_text))
    if not words:
        return 90
    return max(60, min(180, int(round(words * 0.48))))


def song_first_auto_seconds(text: str, block_count: int | None = None,
                            adapted: bool = False) -> int:
    """Best-fit Music 3 ceiling with room for melody and instrumental passages.

    Before songwriting, ``text`` is source prose and is compressed to a safe
    lyric budget. After songwriting, ``adapted=True`` uses the actual generated
    lyric count. The value remains a ceiling because Music 3 may end naturally
    before it without making the render invalid.
    """
    words = len(lyric_tokens(text))
    singable_words = words if adapted else song_first_word_budget(text)
    if block_count is None:
        block_count = len([part for part in re.split(r"\n\s*\n", str(text).strip())
                           if part.strip()])
    sections = max(4, min(8, int(block_count or 1) + 3))
    raw = singable_words / 1.2 + sections * 2.0 + 16.0
    minimum = 30 if adapted else 75
    return max(minimum, min(180, int(math.ceil(raw / 5.0) * 5)))


EXACT_SONG_MAX_WORDS = 60
EXACT_SONG_WORDS_PER_LINE = 7
EXACT_SONG_LINES_PER_VERSE = 3


def format_exact_song_lyrics(source: str,
                             words_per_line: int = EXACT_SONG_WORDS_PER_LINE,
                             lines_per_verse: int = EXACT_SONG_LINES_PER_VERSE) -> str:
    """Reflow immutable words into short Music 3 phrases without rewriting them.

    Music 3 treats a prose paragraph like narration even when the caption says
    "sing". Short lines and small verse sections give the model places to hold
    notes and insert bars while preserving every reviewed token in order.
    """
    units = str(source or "").split()
    if not units:
        return ""
    words_per_line = max(3, min(10, int(words_per_line)))
    lines_per_verse = max(1, min(6, int(lines_per_verse)))
    lines = [" ".join(units[index:index + words_per_line])
             for index in range(0, len(units), words_per_line)]
    sections = []
    for index in range(0, len(lines), lines_per_verse):
        verse = index // lines_per_verse + 1
        sections.append(f"[Verse {verse}]\n" +
                        "\n".join(lines[index:index + lines_per_verse]))
    return "\n\n".join(sections)


def melodic_delivery_qa(metrics: dict | None) -> dict:
    """Fail closed when the rendered vocal behaves like recitation, not a song.

    Thresholds are calibrated against rejected recitative outputs and
    known melodic Music 3 renders. They intentionally favor rerendering over
    publishing a monotone take.
    """
    metrics = dict(metrics or {})
    try:
        analyzed = float(metrics.get("analyzed_seconds") or 0.0)
        pitch_iqr = float(metrics.get("pitch_iqr_semitones") or 0.0)
        pitch_range = float(metrics.get("pitch_p90_p10_semitones") or 0.0)
    except (TypeError, ValueError):
        analyzed = pitch_iqr = pitch_range = 0.0
    minimum_iqr = 8.0
    minimum_range = 18.0
    passed = (analyzed >= 5.0 and pitch_iqr >= minimum_iqr and
              pitch_range >= minimum_range)
    reasons = []
    if analyzed < 5.0:
        reasons.append("not enough analyzable audio")
    if pitch_iqr < minimum_iqr:
        reasons.append("vocal pitch stayed too narrow")
    if pitch_range < minimum_range:
        reasons.append("vocal lacked a melodic pitch range")
    return {
        "passed": passed,
        "reason": ("melodic singing contract passed" if passed else
                   "recitation detected: " + "; ".join(reasons)),
        "analyzed_seconds": round(analyzed, 3),
        "pitch_iqr_semitones": round(pitch_iqr, 3),
        "pitch_p90_p10_semitones": round(pitch_range, 3),
        "minimum_pitch_iqr_semitones": minimum_iqr,
        "minimum_pitch_p90_p10_semitones": minimum_range,
    }


def _split_oversized_frame(frame: dict, max_words: int) -> list[dict]:
    """Split one long screenshot at word boundaries without changing its words."""
    text = str(frame.get("text") or "").strip()
    matches = list(re.finditer(r"[\w']+", text, re.UNICODE))
    if len(matches) <= max_words:
        return [dict(frame, text=text)]
    chunks: list[dict] = []
    for begin in range(0, len(matches), max_words):
        end_index = min(begin + max_words, len(matches))
        start_char = matches[begin].start()
        end_char = (matches[end_index].start() if end_index < len(matches)
                    else len(text))
        chunks.append(dict(frame, text=text[start_char:end_char].strip()))
    return chunks


def plan_exact_song_parts(frames: Sequence[dict],
                          max_words: int = EXACT_SONG_MAX_WORDS) -> list[dict]:
    """Balance exact screenshot lyrics into the fewest contiguous singable parts.

    Every source word is preserved in order. Normal screenshots stay whole; only a
    single screenshot that exceeds the ceiling is split at a word boundary and
    retains the same source image for each resulting chunk.
    """
    if max_words < 20:
        raise ValueError("exact-song word ceiling must be at least 20")
    expanded: list[dict] = []
    for frame in frames:
        if str(frame.get("text") or "").strip():
            expanded.extend(_split_oversized_frame(frame, max_words))
    if not expanded:
        raise ValueError("exact-song planning needs at least one non-empty screenshot")
    weights = [len(lyric_tokens(frame["text"])) for frame in expanded]
    if any(weight > max_words for weight in weights):
        raise ValueError("exact-song planner produced an oversized part")
    total = sum(weights)
    prefix = [0]
    for weight in weights:
        prefix.append(prefix[-1] + weight)

    selected: tuple[int, list[list[float]], list[list[int | None]]] | None = None
    minimum_parts = max(1, int(math.ceil(total / max_words)))
    for part_count in range(minimum_parts, len(expanded) + 1):
        target = total / part_count
        costs = [[math.inf] * (len(expanded) + 1) for _ in range(part_count + 1)]
        previous: list[list[int | None]] = [
            [None] * (len(expanded) + 1) for _ in range(part_count + 1)
        ]
        costs[0][0] = 0.0
        for group in range(1, part_count + 1):
            for end in range(group, len(expanded) + 1):
                for begin in range(group - 1, end):
                    group_words = prefix[end] - prefix[begin]
                    if group_words > max_words or math.isinf(costs[group - 1][begin]):
                        continue
                    cost = costs[group - 1][begin] + (group_words - target) ** 2
                    if cost < costs[group][end]:
                        costs[group][end] = cost
                        previous[group][end] = begin
        if not math.isinf(costs[part_count][len(expanded)]):
            selected = (part_count, costs, previous)
            break
    if selected is None:
        raise RuntimeError("could not partition exact screenshot lyrics")

    part_count, _costs, previous = selected
    ranges: list[tuple[int, int]] = []
    end = len(expanded)
    for group in range(part_count, 0, -1):
        begin = previous[group][end]
        if begin is None:
            raise RuntimeError("exact-song partition reconstruction failed")
        ranges.append((begin, end))
        end = begin
    ranges.reverse()
    parts: list[dict] = []
    for index, (begin, end) in enumerate(ranges, start=1):
        rows = expanded[begin:end]
        lyrics = "\n\n".join(str(row["text"]).strip() for row in rows)
        parts.append({
            "index": index,
            "count": part_count,
            "frames": rows,
            "lyrics": lyrics,
            "word_count": len(lyric_tokens(lyrics)),
        })
    return parts


_SONG_SECTION = re.compile(
    r"^\[(Intro|Verse(?:\s+\d+)?|Pre-Chorus|Chorus(?:\s+\d+)?|Bridge|Outro)\]$",
    re.IGNORECASE,
)


def song_first_lyrics_qa(lyrics: str, max_words: int = 180) -> dict:
    """Deterministic preflight that rejects prose-shaped 'songs'."""
    lines = [line.strip() for line in str(lyrics or "").splitlines() if line.strip()]
    sections = [match.group(1).casefold() for line in lines
                if (match := _SONG_SECTION.fullmatch(line))]
    lyric_lines = [line for line in lines if not _SONG_SECTION.fullmatch(line)]
    words = len(lyric_tokens("\n".join(lyric_lines)))
    verse_count = sum(section.startswith("verse") for section in sections)
    chorus_count = sum(section.startswith("chorus") for section in sections)
    max_line_words = max((len(lyric_tokens(line)) for line in lyric_lines), default=0)
    reasons: list[str] = []
    if not 40 <= words <= max_words:
        reasons.append(f"song-first lyrics must contain 40 to {max_words} words")
    if verse_count < 2:
        reasons.append("song-first lyrics need at least two verses")
    if chorus_count < 2:
        reasons.append("song-first lyrics need at least two written-out choruses")
    if max_line_words > 16:
        reasons.append("song-first lyric lines must stay at or below 16 words")
    return {
        "passed": not reasons,
        "reason": "; ".join(reasons) if reasons else "song-first structure passed",
        "word_count": words,
        "verse_count": verse_count,
        "chorus_count": chorus_count,
        "max_line_words": max_line_words,
    }


def literal_vocal_qa(target_text: str, transcript_words: Sequence[dict],
                     requested_duration: float | None = None,
                     actual_duration: float | None = None,
                     duration_policy: str = "target",
                     delivery_policy: str = "literal") -> dict:
    """Fail-closed ASR evidence for one ordered, verbatim vocal pass.

    The comparison is deterministic. The Director may rerender from this receipt,
    but no language model is allowed to rewrite the approved words or waive the
    gate. Small ASR uncertainty is tolerated; long additions, repeated lyric runs,
    missing passages, and materially short renders are not.
    """
    target = lyric_tokens(target_text)
    heard: list[str] = []
    heard_probabilities: list[float | None] = []
    for row in transcript_words or []:
        tokens = lyric_tokens(str(row.get("word", "")))
        heard.extend(tokens)
        try:
            probability = float(row["probability"])
        except (KeyError, TypeError, ValueError):
            probability = None
        heard_probabilities.extend([probability] * len(tokens))
    if not target or not heard:
        return {
            "passed": False,
            "reason": "no target words" if not target else "no usable vocal transcript",
            "target_words": len(target), "heard_words": len(heard),
            "matched_words": 0, "matched_fraction": 0.0,
            "omitted_words": len(target), "extra_words": len(heard),
            "max_extra_run": len(heard), "extra_samples": heard[:16],
            "duration_ok": False if requested_duration else True,
        }

    matcher = SequenceMatcher(None, target, heard, autojunk=False)
    matched_target: set[int] = set()
    matched_heard: set[int] = set()
    matched = 0
    for block in matcher.get_matching_blocks():
        for offset in range(block.size):
            matched_target.add(block.a + offset)
            matched_heard.add(block.b + offset)
            matched += 1

    extra_runs_indexed: list[list[tuple[int, str]]] = []
    run: list[tuple[int, str]] = []
    for index, token in enumerate(heard):
        if index not in matched_heard:
            run.append((index, token))
        elif run:
            extra_runs_indexed.append(run); run = []
    if run:
        extra_runs_indexed.append(run)

    ignored_tail: list[str] = []
    if extra_runs_indexed and matched_heard:
        tail = extra_runs_indexed[-1]
        probabilities = [heard_probabilities[index] for index, _token in tail]
        if (tail[0][0] > max(matched_heard) and probabilities and
                all(value is not None for value in probabilities) and
                sum(value for value in probabilities if value is not None) /
                len(probabilities) < 0.65):
            ignored_tail = [token for _index, token in tail]
            extra_runs_indexed.pop()

    extra_runs = [[token for _index, token in run] for run in extra_runs_indexed]
    extras = [token for run in extra_runs for token in run]
    omitted = len(target) - len(matched_target)
    coverage = matched / max(1, len(target))
    max_extra_run = max((len(run) for run in extra_runs), default=0)
    if delivery_policy == "exact":
        # Exact Auto-fit parts are intentionally short (<=110 words), so two
        # missing or substituted words are material and must trigger a retry.
        # One isolated token remains available for unavoidable ASR uncertainty.
        minimum_coverage = 0.985
        allowed_omissions = 1
        allowed_extras = 1
        allowed_extra_run = 1
    elif delivery_policy == "song":
        minimum_coverage = 0.88
        allowed_omissions = max(2, int(math.floor(len(target) * 0.12)))
        allowed_extras = max(3, int(math.floor(len(target) * 0.08)))
        allowed_extra_run = 3
    elif delivery_policy == "literal":
        minimum_coverage = 0.90
        allowed_omissions = max(1, int(math.floor(len(target) * 0.08)))
        allowed_extras = max(2, int(math.floor(len(target) * 0.04)))
        allowed_extra_run = 2
    else:
        raise ValueError(f"unknown delivery policy: {delivery_policy}")

    duration_ok = True
    duration_reason = ""
    actual = 0.0
    if requested_duration:
        requested = float(requested_duration)
        try:
            actual = float(actual_duration) if actual_duration is not None else 0.0
        except (TypeError, ValueError):
            actual = 0.0
        if actual <= 0 or not math.isfinite(actual):
            duration_ok = False
            duration_reason = "rendered duration could not be measured"
        else:
            maximum = requested + 8.0
            if duration_policy == "maximum":
                duration_ok = 1.0 <= actual <= maximum
            elif duration_policy == "target":
                minimum = max(1.0, requested * 0.88, requested - 12.0)
                duration_ok = minimum <= actual <= maximum
            else:
                raise ValueError(f"unknown duration policy: {duration_policy}")
            if not duration_ok:
                duration_reason = f"rendered {actual:.1f}s for a {requested:.1f}s request"

    passed = (coverage >= minimum_coverage and omitted <= allowed_omissions and
              len(extras) <= allowed_extras and
              max_extra_run <= allowed_extra_run and duration_ok)
    reasons: list[str] = []
    if coverage < minimum_coverage or omitted > allowed_omissions:
        reasons.append(f"only {coverage:.0%} of reviewed words matched")
    if len(extras) > allowed_extras or max_extra_run > allowed_extra_run:
        reasons.append(f"{len(extras)} unapproved words detected")
    if duration_reason:
        reasons.append(duration_reason)
    return {
        "passed": passed,
        "reason": "; ".join(reasons) if reasons else (
            "exact lyric contract passed" if delivery_policy == "exact"
            else ("song-first vocal contract passed" if delivery_policy == "song"
                  else "verbatim vocal contract passed")),
        "target_words": len(target), "heard_words": len(heard),
        "matched_words": matched, "matched_fraction": round(coverage, 4),
        "omitted_words": omitted, "extra_words": len(extras),
        "max_extra_run": max_extra_run,
        "extra_samples": [" ".join(run[:12]) for run in extra_runs[:4]],
        "ignored_low_confidence_tail": ignored_tail,
        "allowed_omissions": allowed_omissions, "allowed_extras": allowed_extras,
        "duration_ok": duration_ok,
        "duration_policy": duration_policy,
        "delivery_policy": delivery_policy,
        "minimum_coverage": minimum_coverage,
        "allowed_extra_run": allowed_extra_run,
        "requested_duration": round(float(requested_duration), 3) if requested_duration else None,
        "actual_duration": (round(actual, 3) if requested_duration and actual > 0 and
                            math.isfinite(actual) else None),
    }


def lyric_weighted_starts(texts: Sequence[str], duration: float) -> list[float]:
    if not texts:
        return []
    duration = max(float(duration), 0.1)
    weights = [max(1, len(lyric_tokens(text))) for text in texts]
    total = sum(weights)
    starts = [0.0]
    elapsed = 0
    for weight in weights[:-1]:
        elapsed += weight
        starts.append(round(duration * elapsed / total, 3))
    return starts


def aligned_starts(texts: Sequence[str], transcript_words: Sequence[dict], duration: float) -> dict:
    """Map each screenshot's first lyric to Whisper word time.

    SequenceMatcher aligns the complete intended lyric stream to the ASR stream,
    so occasional missing/misheard sung words do not shift every later cue. Any
    unresolved boundary falls back to its lyric-weighted position and is reported.
    """
    fallback = lyric_weighted_starts(texts, duration)
    if len(texts) < 2:
        return {"starts": fallback, "method": "single-frame", "matched_fraction": 1.0,
                "fallback_boundaries": []}

    target: list[str] = []
    ranges: list[tuple[int, int]] = []
    for text in texts:
        begin = len(target)
        target.extend(lyric_tokens(text))
        ranges.append((begin, len(target)))
    heard = [lyric_tokens(str(row.get("word", ""))) for row in transcript_words]
    heard_flat = [parts[0] if parts else "" for parts in heard]
    usable_heard = [(i, token) for i, token in enumerate(heard_flat) if token]
    heard_tokens = [token for _i, token in usable_heard]

    if not target or not heard_tokens:
        return {"starts": fallback, "method": "lyric-weighted", "matched_fraction": 0.0,
                "fallback_boundaries": list(range(1, len(texts)))}

    matcher = SequenceMatcher(None, target, heard_tokens, autojunk=False)
    target_to_heard: dict[int, int] = {}
    matched = 0
    for block in matcher.get_matching_blocks():
        for offset in range(block.size):
            target_to_heard[block.a + offset] = usable_heard[block.b + offset][0]
            matched += 1

    starts = [0.0]
    unresolved: list[int] = []
    for shot_index, (begin, end) in enumerate(ranges[1:], start=1):
        candidates = [target_to_heard[i] for i in range(begin, end) if i in target_to_heard]
        if candidates:
            value = float(transcript_words[min(candidates)].get("start", fallback[shot_index]))
        else:
            value = fallback[shot_index]
            unresolved.append(shot_index)
        # Keep cues strictly monotonic and leave enough time for a visible frame.
        value = max(starts[-1] + 0.35, min(float(duration) - 0.35, value))
        starts.append(round(value, 3))

    fraction = matched / max(1, len(target))
    method = "whisper-word-aligned" if fraction >= 0.35 else "lyric-weighted"
    if method == "lyric-weighted":
        starts = fallback
        unresolved = list(range(1, len(texts)))
    return {"starts": starts, "method": method,
            "matched_fraction": round(fraction, 4), "fallback_boundaries": unresolved}


def _run(command: Sequence[str], timeout: int = 1800) -> None:
    result = subprocess.run(list(command), capture_output=True, text=True, timeout=timeout)
    if result.returncode:
        detail = (result.stderr or result.stdout or "ffmpeg failed").strip()[-2000:]
        raise RuntimeError(detail)


def render_screenshot_video(images: Sequence[Path], starts: Sequence[float], duration: float,
                            audio: Path, output: Path, poster: Path,
                            orientation: str = "portrait", motion: bool = True,
                            workdir: Path | None = None) -> dict:
    """Build a readable screenshot sequence with optional restrained motion.

    The screenshots themselves are never regenerated. Motion is a six-pixel
    deterministic drift over a blurred copy, preserving every source glyph.
    """
    if not images or len(images) != len(starts):
        raise ValueError("images and starts must be non-empty and the same length")
    if orientation == "landscape":
        width, height = 1920, 1080
    elif orientation == "square":
        width, height = 1080, 1080
    else:
        width, height = 1080, 1920
    duration = max(float(duration), 0.5)
    root = Path(workdir or output.parent / f".{output.stem}-work")
    root.mkdir(parents=True, exist_ok=True)
    clips: list[Path] = []
    cue_rows: list[dict] = []
    completed = False

    try:
        for index, (image, start) in enumerate(zip(images, starts)):
            end = float(starts[index + 1]) if index + 1 < len(starts) else duration
            seconds = max(0.35, end - float(start))
            clip = root / f"shot-{index + 1:02d}.mp4"
            inner_w, inner_h = width - 54, height - 54
            if motion:
                cycle = max(seconds, 1.0)
                vf = (
                    f"split=2[bgsrc][fgsrc];"
                    f"[bgsrc]scale={width}:{height}:force_original_aspect_ratio=increase:flags=lanczos,"
                    f"crop={width}:{height},gblur=sigma=36,eq=brightness=-0.38[bg];"
                    f"[fgsrc]scale={inner_w}:{inner_h}:force_original_aspect_ratio=decrease:flags=lanczos[fg];"
                    f"[bg][fg]overlay=x=(W-w)/2:y=(H-h)/2+6*sin(2*PI*t/{cycle:.3f}),format=yuv420p"
                )
            else:
                vf = (f"scale={inner_w}:{inner_h}:force_original_aspect_ratio=decrease:flags=lanczos,"
                      f"pad={width}:{height}:(ow-iw)/2:(oh-ih)/2:color=0x0B0806,format=yuv420p")
            _run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-loop", "1", "-i", str(image),
                  "-t", f"{seconds:.3f}", "-vf", vf, "-r", "30", "-an", "-c:v", "libx264",
                  "-preset", "fast", "-crf", "20", "-pix_fmt", "yuv420p", str(clip)])
            clips.append(clip)
            cue_rows.append({"image": str(image), "start": round(float(start), 3),
                              "end": round(min(duration, end), 3)})

        concat_file = root / "clips.txt"
        concat_file.write_text("".join(f"file '{clip.as_posix()}'\n" for clip in clips), encoding="utf-8")
        silent = root / "silent.mp4"
        _run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "concat", "-safe", "0",
              "-i", str(concat_file), "-c", "copy", str(silent)])
        _run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-i", str(silent), "-i", str(audio),
              "-map", "0:v:0", "-map", "1:a:0", "-c:v", "copy", "-c:a", "aac", "-b:a", "192k",
              "-shortest", "-movflags", "+faststart", str(output)])
        _run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", "0.2", "-i", str(output),
              "-frames:v", "1", str(poster)], timeout=300)
        receipt = {"width": width, "height": height, "duration": round(duration, 3),
                   "motion": bool(motion), "cues": cue_rows, "output": str(output),
                   "poster": str(poster)}
        (root / "receipt.json").write_text(json.dumps(receipt, indent=2), encoding="utf-8")
        completed = True
        return receipt
    finally:
        # The receipt is durable evidence; per-shot clips and concat files are not.
        for pattern in ("shot-*.mp4", "clips.txt", "silent.mp4"):
            for intermediate in root.glob(pattern):
                intermediate.unlink(missing_ok=True)
        if not completed:
            output.unlink(missing_ok=True)
            poster.unlink(missing_ok=True)
