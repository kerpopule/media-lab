import copy
import json
import shutil
import subprocess

import pytest

from media_lab_core.cut import (
    load_storyboard_project,
    render_single_source_export,
    validate_export_request,
)


def _project(tmp_path, duration=3):
    source = tmp_path / "storyboard.json"
    source.write_text(
        json.dumps(
            {
                "schema": "media_lab.storyboard.v1",
                "project": "ffmpeg-fixture",
                "title": "Generated fixture qualification",
                "private_internal_only": True,
                "candidate_not_final_until_owner_approves": True,
                "publication_authorized": False,
                "format": {
                    "resolution": "640x360",
                    "aspect_ratio": "16:9",
                    "fps": 24,
                    "shot_duration_seconds": duration,
                    "shot_count": 1,
                    "estimated_total_seconds": duration,
                },
                "shots": [
                    {
                        "id": "FIXTURE01",
                        "kind": "generated-fixture",
                        "narration": "Synthetic tone only.",
                        "visual": "Deterministic solid color fixture.",
                        "engine_plan": "ffmpeg lavfi",
                        "qa": ["synthetic-only"],
                    }
                ],
            },
            sort_keys=True,
        )
    )
    return load_storyboard_project(source)


def _generate_source(path):
    result = subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-y",
            "-fflags",
            "+bitexact",
            "-f",
            "lavfi",
            "-i",
            "color=c=0x281810:s=640x360:r=24:d=3",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=48000:duration=3",
            "-c:v",
            "libx264",
            "-threads",
            "1",
            "-flags:v",
            "+bitexact",
            "-pix_fmt",
            "yuv420p",
            "-c:a",
            "aac",
            "-flags:a",
            "+bitexact",
            "-map_metadata",
            "-1",
            "-metadata",
            "creation_time=1970-01-01T00:00:00Z",
            "-shortest",
            str(path),
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def _generate_long_source(path):
    result = subprocess.run(
        [
            "ffmpeg", "-nostdin", "-v", "error", "-y", "-fflags", "+bitexact",
            "-f", "lavfi", "-i", "testsrc2=s=320x180:r=30000/1001:d=74",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=74",
            "-c:v", "libx264", "-preset", "ultrafast", "-threads", "1",
            "-flags:v", "+bitexact", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-flags:a", "+bitexact", "-map_metadata", "-1",
            "-shortest", str(path),
        ],
        capture_output=True, text=True, timeout=120, check=False,
    )
    assert result.returncode == 0, result.stderr


def _generate_one_frame_short_source(path):
    """Three seconds of audio with only 71 of the expected 72 video frames."""
    result = subprocess.run(
        [
            "ffmpeg", "-nostdin", "-v", "error", "-y", "-fflags", "+bitexact",
            "-f", "lavfi", "-i", "testsrc2=s=320x180:r=24:d=3",
            "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=48000:duration=3",
            "-map", "0:v", "-map", "1:a", "-frames:v", "71",
            "-c:v", "libx264", "-preset", "ultrafast", "-threads", "1",
            "-flags:v", "+bitexact", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-flags:a", "+bitexact", "-map_metadata", "-1",
            str(path),
        ],
        capture_output=True, text=True, timeout=60, check=False,
    )
    assert result.returncode == 0, result.stderr


def _sixteen_clip_project(tmp_path):
    from media_lab_core import cut

    source = tmp_path / "source.mp4"
    _generate_long_source(source)
    item = cut.probe_gallery_file(source)
    item.update({"job_id": "synthetic", "title": "Synthetic", "prompt": ""})
    project = cut.build_gallery_project("cut-1607", "1,607 frames", [item])
    # Deliberately conform a 29.97 fps source to a 24 fps project. Fractional
    # source timestamps reproduce the per-seek rounding that caused the tail.
    project["settings"]["fps"] = 24
    template = project["timeline"]["tracks"][0]["clips"][0]
    ranges = [
        (0, 111), (118, 211), (217, 310), (319, 465), (477, 528),
        (536, 727), (733, 876), (884, 995), (1005, 1046),
        (1055, 1142), (1171, 1232), (1240, 1292), (1298, 1410),
        (1418, 1493), (1500, 1605), (1617, 1752),
    ]
    clips = []
    cursor = 0
    for number, (trim_in, trim_out) in enumerate(ranges):
        clip = copy.deepcopy(template)
        clip.update({
            "id": f"clip-{number:02d}", "start_frame": cursor,
            "trim_in_frame": trim_in, "trim_out_frame": trim_out,
            "duration_frames": trim_out - trim_in,
        })
        clips.append(clip)
        cursor += trim_out - trim_in
    project["timeline"]["tracks"][0]["clips"] = clips
    project["timeline"]["captions"]["items"] = [
        {"id": "caption-1", "text": "Synthetic caption", "start_frame": 0, "end_frame": 48}
    ]
    project["duration_frames"] = cursor
    project["duration_seconds"] = cursor / 24
    cut.validate_manifest(project)
    assert cursor == 1607
    return cut, project, source


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe are required for the CPU finishing tracer",
)
def test_cpu_ffmpeg_range_export_decodes_with_audio(tmp_path):
    source = tmp_path / "source.mp4"
    output = tmp_path / "range.mp4"
    _generate_source(source)
    project = _project(tmp_path)
    export_request = validate_export_request(
        project,
        {
            "range_mode": "selection",
            "range_start_seconds": 0.5,
            "range_end_seconds": 2.25,
            "format": "mp4",
            "quality": "preview",
            "include_audio": True,
        },
    )
    receipt = render_single_source_export(
        source=source,
        output=output,
        export_request=export_request,
        timeout_seconds=60,
    )

    assert receipt["exit_code"] == 0
    assert receipt["candidate_not_final"] is True
    assert receipt["publication_authorized"] is False
    assert receipt["bytes"] == output.stat().st_size
    assert not (tmp_path / ".range.partial.mp4").exists()
    data = receipt["ffprobe"]
    assert {stream["codec_type"] for stream in data["streams"]} == {"video", "audio"}
    video = next(stream for stream in data["streams"] if stream["codec_type"] == "video")
    assert video["codec_name"] == "h264"
    assert (video["width"], video["height"]) == (640, 360)
    assert 1.7 <= float(data["format"]["duration"]) <= 1.85


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe are required for reproducibility qualification",
)
def test_two_cpu_preview_runs_are_byte_and_probe_reproducible(tmp_path):
    source = tmp_path / "generated-source.mp4"
    _generate_source(source)
    request = validate_export_request(
        _project(tmp_path),
        {
            "range_mode": "selection",
            "range_start_seconds": 0.25,
            "range_end_seconds": 1.75,
            "format": "mp4",
            "quality": "preview",
            "include_audio": True,
        },
    )
    first = render_single_source_export(
        source=source,
        output=tmp_path / "preview-a.mp4",
        export_request=request,
        timeout_seconds=60,
    )
    second = render_single_source_export(
        source=source,
        output=tmp_path / "preview-b.mp4",
        export_request=request,
        timeout_seconds=60,
    )

    assert first["sha256"] == second["sha256"]
    assert first["bytes"] == second["bytes"]
    assert first["ffprobe"] == second["ffprobe"]
    assert first["command"][:-1] == second["command"][:-1]


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe are required for the CPU finishing tracer",
)
@pytest.mark.parametrize("burn_captions", [False, True])
def test_sixteen_clip_render_has_exact_frame_count_and_caption_input_order(tmp_path, burn_captions):
    cut, project, source = _sixteen_clip_project(tmp_path)
    output = tmp_path / f"sixteen-{burn_captions}.mp4"
    request = cut.validate_export_request(project, {
        "quality": "preview", "format": "mp4", "include_audio": True,
        "burn_captions": burn_captions,
    })
    receipt = cut.render_timeline(
        project, media_dir=tmp_path, output=output, export_request=request,
        work_dir=tmp_path / f"work-{burn_captions}", timeout_seconds=180,
    )

    video = next(s for s in receipt["ffprobe"]["streams"] if s["codec_type"] == "video")
    filter_graph = receipt["command"][receipt["command"].index("-filter_complex") + 1]
    assert f"trim=end_frame=1607,setpts=N/(24*TB)[vframes]" in filter_graph
    assert receipt["expected_seconds"] == pytest.approx(1607 / 24)
    assert int(video["nb_frames"]) == 1607
    assert float(video["duration"]) == pytest.approx(1607 / 24, abs=0.001)
    assert receipt["captions"] == ("burned" if burn_captions else "embedded")
    if not burn_captions:
        command = receipt["command"]
        assert command.index(str(tmp_path / "work-False" / "captions.srt")) < command.index("-filter_complex")


@pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe are required for the CPU finishing tracer",
)
@pytest.mark.parametrize("burn_captions", [False, True])
def test_timeline_pads_a_one_frame_decode_shortfall_to_manifest_contract(tmp_path, burn_captions):
    from media_lab_core import cut

    source = tmp_path / "short-source.mp4"
    _generate_one_frame_short_source(source)
    item = cut.probe_gallery_file(source)
    item.update({"job_id": "synthetic", "title": "Synthetic", "prompt": ""})
    project = cut.build_gallery_project("cut-short", "Decode shortfall", [item])
    clip = project["timeline"]["tracks"][0]["clips"][0]
    clip["trim_out_frame"] = 72
    clip["duration_frames"] = 72
    project["duration_frames"] = 72
    project["duration_seconds"] = 3.0
    project["timeline"]["captions"]["items"] = [
        {"id": "caption-1", "text": "Synthetic caption", "start_frame": 0, "end_frame": 48}
    ]
    assert project["duration_frames"] == 72

    output = tmp_path / f"short-{burn_captions}.mp4"
    request = cut.validate_export_request(project, {
        "quality": "preview", "format": "mp4", "include_audio": True,
        "burn_captions": burn_captions,
    })
    receipt = cut.render_timeline(
        project, media_dir=tmp_path, output=output, export_request=request,
        work_dir=tmp_path / f"short-work-{burn_captions}", timeout_seconds=60,
    )

    video = next(s for s in receipt["ffprobe"]["streams"] if s["codec_type"] == "video")
    assert int(video["nb_frames"]) == 72
    assert float(video["duration"]) == pytest.approx(3.0, abs=0.001)
    assert receipt["captions"] == ("burned" if burn_captions else "embedded")
