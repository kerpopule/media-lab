#!/usr/bin/env python3
"""Safe trimming for visible dialogue: one source timeline, coupled audio and video."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path


def probe(path: Path) -> dict:
    return json.loads(subprocess.check_output([
        "ffprobe", "-v", "error", "-show_entries",
        "format=duration:stream=codec_type,codec_name,width,height,r_frame_rate,sample_rate,channels",
        "-of", "json", str(path),
    ], text=True))


def has_audio(path: Path) -> bool:
    data = probe(path)
    return any(s.get("codec_type") == "audio" for s in data.get("streams", []))


def build_command(source: Path, output: Path, trim_in: float, duration: float, *, width: int = 1280, height: int = 704, fps: int = 24) -> list[str]:
    if trim_in < 0 or duration <= 0:
        raise ValueError(f"invalid coupled trim window: in={trim_in}, duration={duration}")
    fade_out = max(0.0, duration - 0.04)
    return [
        "ffmpeg", "-nostdin", "-v", "error", "-y",
        "-ss", f"{trim_in:.6f}", "-i", str(source),
        "-map", "0:v:0", "-map", "0:a:0",
        "-vf", f"fps={fps},scale={width}:{height}:flags=lanczos,setsar=1,setpts=PTS-STARTPTS,format=yuv420p",
        "-af", f"asetpts=PTS-STARTPTS,afade=t=in:st=0:d=0.030,afade=t=out:st={fade_out:.6f}:d=0.040,loudnorm=I=-16:TP=-2:LRA=11",
        "-t", f"{duration:.6f}",
        "-c:v", "libx264", "-preset", "slow", "-crf", "18", "-g", "48", "-keyint_min", "24",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", "-ac", "1", "-movflags", "+faststart",
        str(output),
    ]


def coupled_av_trim(source: Path, output: Path, trim_in: float, duration: float, *, width: int = 1280, height: int = 704, fps: int = 24) -> dict:
    source = Path(source); output = Path(output)
    if not source.is_file():
        raise FileNotFoundError(source)
    if not has_audio(source):
        raise RuntimeError(f"visible-dialogue source has no embedded audio: {source}")
    output.parent.mkdir(parents=True, exist_ok=True)
    command = build_command(source, output, trim_in, duration, width=width, height=height, fps=fps)
    subprocess.run(command, check=True)
    data = probe(output)
    streams = data.get("streams", [])
    if not any(s.get("codec_type") == "video" for s in streams) or not any(s.get("codec_type") == "audio" for s in streams):
        raise RuntimeError(f"coupled trim lost a stream: {output}")
    return {
        "coupled_av_trim": True,
        "single_input_timeline": True,
        "source": str(source),
        "output": str(output),
        "trim_in_seconds": round(trim_in, 6),
        "requested_duration_seconds": round(duration, 6),
        "duration_seconds": float(data["format"]["duration"]),
        "has_audio": True,
        "audio_map": "0:a:0",
        "video_map": "0:v:0",
        "command": command,
    }
