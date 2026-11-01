"""Bounded vibrant SDR finishing from the same pre-HDR working original.

Never retag HDR pixels. Native HLG is explicitly tone mapped; PQ is refused.
No inference, resizing, sharpening, frame truncation, or soundtrack regeneration.
"""

import hashlib
import re
import subprocess
import tempfile
import time
from fractions import Fraction
from pathlib import Path

import numpy as np

from . import hdr_finish
from .hdr_color import inspection_trim

POLICY = "vibrant-sdr709-v1"


def digest(path):
    value = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            value.update(chunk)
    return value.hexdigest()


def grade(rgb):
    rgb = rgb + 0.08 * rgb * (1 - rgb) * (2 * rgb - 1)
    y = rgb @ np.array([0.2126, 0.7152, 0.0722])
    saturation = np.ptp(rgb, axis=-1) / np.maximum(rgb.max(axis=-1), 1e-8)
    delta = rgb - y[..., None]
    upper = np.where(delta > 1e-8, (1 - y[..., None]) / np.maximum(delta, 1e-8), 100)
    lower = np.where(delta < -1e-8, y[..., None] / np.maximum(-delta, 1e-8), 100)
    gain = np.minimum(
        1 + 0.18 * (1 - saturation), np.minimum(upper.min(axis=-1), lower.min(axis=-1))
    )
    return np.clip(y[..., None] + delta * gain[..., None], 0, 1)


def run(command, *, cwd=None, cancelled=lambda: False, timeout: float = 900):
    """Own just this subprocess; cancel/reap it without touching shared services."""
    if cancelled():
        raise RuntimeError("Video finishing cancelled")
    with tempfile.TemporaryFile() as errors:
        process = subprocess.Popen(
            command, cwd=cwd, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=errors
        )
        start = time.monotonic()
        try:
            while process.poll() is None:
                if cancelled():
                    raise RuntimeError("Video finishing cancelled")
                if time.monotonic() - start > timeout:
                    raise TimeoutError("Video finishing exceeded time limit")
                time.sleep(0.05)
            if process.returncode:
                errors.seek(0, 2)
                errors.seek(max(0, errors.tell() - 4096))
                raise RuntimeError("Video tool failed: " + errors.read().decode(errors="replace"))
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)


def verify(path, *, source=None, cancelled=lambda: False):
    path = Path(path)
    info = hdr_finish.probe(path)
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    expected = {
        "codec_name": "h264",
        "profile": "High",
        "pix_fmt": "yuv420p",
        "color_range": "tv",
        "color_primaries": "bt709",
        "color_transfer": "bt709",
        "color_space": "bt709",
    }
    for key, value in expected.items():
        if video.get(key) != value:
            raise ValueError("Wrong SDR " + key)
    boxes = hdr_finish.colr_atoms(path.read_bytes())
    if not boxes or any(b["values"] != [1, 1, 1] for b in boxes):
        raise ValueError("SDR container signaling mismatch")
    trace = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "verbose",
            "-i",
            str(path),
            "-map",
            "0:v:0",
            "-c:v",
            "copy",
            "-bsf:v",
            "trace_headers",
            "-frames:v",
            "1",
            "-f",
            "null",
            "-",
        ],
        capture_output=True,
        text=True,
        check=True,
        timeout=60,
    ).stderr
    vui = {}
    for key in ("colour_primaries", "transfer_characteristics", "matrix_coefficients"):
        values = sorted(set(map(int, re.findall(r"\b" + key + r"\s+\S+\s*=\s*(\d+)", trace))))
        if values != [1]:
            raise ValueError("SDR VUI signaling mismatch")
        vui[key] = values
    if source is not None:
        original = hdr_finish.probe(source)
        prior = next(s for s in original["streams"] if s["codec_type"] == "video")
        for key in ("width", "height", "nb_frames"):
            if key in prior and video.get(key) != prior[key]:
                raise ValueError("SDR changed " + key)
        if Fraction(video["avg_frame_rate"]) != Fraction(prior["avg_frame_rate"]):
            raise ValueError("SDR changed cadence")
        if abs(float(video["duration"]) - float(prior["duration"])) > 0.05:
            raise ValueError("SDR changed duration")
        before = [s for s in original["streams"] if s["codec_type"] == "audio"]
        after = [s for s in info["streams"] if s["codec_type"] == "audio"]
        if len(before) != len(after):
            raise ValueError("SDR lost audio streams")
        for a, b in zip(before, after):
            if any(a.get(k) != b.get(k) for k in ("codec_name", "sample_rate", "channels")):
                raise ValueError("SDR changed audio format")
            if abs(float(a.get("duration", 0)) - float(b.get("duration", 0))) > 0.05:
                raise ValueError("SDR changed audio duration")
    run(
        ["ffmpeg", "-nostdin", "-v", "error", "-xerror", "-i", str(path), "-f", "null", "-"],
        cancelled=cancelled,
    )
    return {
        "policy": POLICY,
        "sha256": digest(path),
        "probe": info,
        "container_colr": boxes,
        "codec_vui": vui,
        "full_decode_exit": 0,
    }


def encode(source, target, *, assume_sdr709=False, cancelled=lambda: False):
    source, target = Path(source).resolve(), Path(target).resolve()
    if target.exists() or source == target:
        raise ValueError("Refuse existing SDR output/source overwrite")
    if cancelled():
        raise RuntimeError("Video finishing cancelled")
    stream = next(s for s in hdr_finish.probe(source)["streams"] if s["codec_type"] == "video")
    hlg = stream.get("color_transfer") == "arib-std-b67"
    primaries, matrix = ("bt2020", "bt2020nc") if hlg else ("bt709", "bt709")
    if stream.get("color_transfer") not in ("bt709", "arib-std-b67", None, "unknown"):
        raise ValueError("Unsupported SDR source transfer (including PQ)")
    for key, expected in (
        ("color_primaries", primaries),
        ("color_space", matrix),
        ("color_range", "tv"),
    ):
        allowed = (expected,) if hlg or not assume_sdr709 else (expected, None, "unknown")
        if stream.get(key) not in allowed:
            raise ValueError("Incompatible SDR source " + key)
    if not hlg and not assume_sdr709 and stream.get("color_transfer") != "bt709":
        raise ValueError("Ambiguous SDR transfer")
    start = time.perf_counter()
    with tempfile.TemporaryDirectory(prefix="sdr-grade-", dir=target.parent) as temp:
        lut = Path(temp) / "grade.cube"
        b, g, r = np.meshgrid(*[np.linspace(0, 1, 65)] * 3, indexing="ij")
        rgb = np.stack((r, g, b), -1).reshape(-1, 3)
        values = grade(inspection_trim(rgb) if hlg else rgb)
        with lut.open("x") as output:
            output.write(
                'TITLE "Vibrant SDR709 v1"\nLUT_3D_SIZE 65\nDOMAIN_MIN 0 0 0\nDOMAIN_MAX 1 1 1\n'
            )
            np.savetxt(output, values, fmt="%.8f")
        vf = (
            f"scale=in_color_matrix={matrix}:in_range=tv:out_range=pc,format=gbrp16le,"
            "lut3d=file=grade.cube:interp=tetrahedral,scale=out_color_matrix=bt709:out_range=tv,"
            "format=yuv420p,setparams=colorspace=bt709:color_primaries=bt709:color_trc=bt709:range=tv"
        )
        command = [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-n",
            "-i",
            str(source),
            "-map",
            "0:v:0",
            "-map",
            "0:a?",
            "-vf",
            vf,
            "-c:v",
            "libx264",
            "-profile:v",
            "high",
            "-preset",
            "slow",
            "-crf",
            "20",
            "-x264-params",
            "colorprim=bt709:transfer=bt709:colormatrix=bt709",
            "-color_primaries",
            "bt709",
            "-color_trc",
            "bt709",
            "-colorspace",
            "bt709",
            "-color_range",
            "tv",
            "-c:a",
            "copy",
            "-movflags",
            "+faststart",
            str(target),
        ]
        try:
            run(command, cwd=temp, cancelled=cancelled)
            encoded = time.perf_counter()
            receipt = verify(target, source=source, cancelled=cancelled)
            if cancelled():
                raise RuntimeError("Video finishing cancelled")
        except BaseException:
            target.unlink(missing_ok=True)
            raise
    receipt.update(
        source_sha256=digest(source),
        source_transform="HLG tone map + vibrance" if hlg else "Original SDR709 + vibrance",
        encode_seconds=encoded - start,
        verify_seconds=time.perf_counter() - encoded,
    )
    return receipt
