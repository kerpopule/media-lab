"""Contracts for the queue-owned H3 -> LTX 2.5 refinement path."""
from __future__ import annotations

import ast
import hashlib
import importlib
import json
import shutil
import subprocess
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).parents[1]
APP = ROOT / "app.py"
ENGINE = ROOT / "runner" / "engine_server.py"


def function(name, **namespace):
    tree = ast.parse(APP.read_text())
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(APP), "exec"), namespace)
    return namespace[name]


def media_validator():
    fingerprint = function("_artifact_fingerprint", Path=Path, hashlib=hashlib)
    validate = function(
        "_validate_media_artifact",
        Path=Path,
        subprocess=subprocess,
        json=json,
        math=__import__("math"),
        _artifact_fingerprint=fingerprint,
    )
    return fingerprint, validate


def make_video(path, video_filter, *, audio=True):
    command = [
        "ffmpeg", "-nostdin", "-v", "error", "-y",
        "-f", "lavfi", "-i", video_filter,
    ]
    if audio:
        command += ["-f", "lavfi", "-i", "sine=frequency=440:sample_rate=8000:d=1"]
    command += ["-t", "1", "-c:v", "libx264", "-threads", "1", "-pix_fmt", "yuv420p"]
    if audio:
        command += ["-c:a", "aac", "-shortest"]
    else:
        command += ["-an"]
    command.append(str(path))
    result = subprocess.run(command, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stderr


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
                    reason="ffmpeg and ffprobe are required")
def test_media_validation_accepts_decoded_motion_and_audio(tmp_path):
    _, validate = media_validator()
    artifact = tmp_path / "valid.mp4"
    make_video(artifact, "testsrc2=s=320x180:r=8:d=1")
    metrics = validate(artifact, require_audio=True, expected_dimensions=(320, 180))
    assert metrics["video_frames_decoded"] >= 4
    assert metrics["audio_bytes_decoded"] > 0
    assert metrics["width"] == 320 and metrics["height"] == 180


@pytest.mark.skipif(not shutil.which("ffmpeg") or not shutil.which("ffprobe"),
                    reason="ffmpeg and ffprobe are required")
@pytest.mark.parametrize("kind", ["broken", "black", "constant", "noise", "missing-audio", "stale-cache"])
def test_media_validation_rejects_bad_artifacts(tmp_path, kind):
    fingerprint, validate = media_validator()
    artifact = tmp_path / f"{kind}.mp4"
    prior = None
    if kind == "broken":
        artifact.write_bytes(b"not an mp4")
    elif kind == "black":
        make_video(artifact, "color=black:s=64x36:r=8:d=1")
    elif kind == "constant":
        make_video(artifact, "color=blue:s=64x36:r=8:d=1")
    elif kind == "noise":
        make_video(artifact, "nullsrc=s=64x36:r=8:d=1,geq=random(1)*255:128:128")
    elif kind == "missing-audio":
        make_video(artifact, "testsrc2=s=320x180:r=8:d=1", audio=False)
    else:
        make_video(artifact, "testsrc2=s=320x180:r=8:d=1")
        prior = fingerprint(artifact)
    expected = (320, 180) if kind in ("missing-audio", "stale-cache") else (64, 36)
    with pytest.raises(RuntimeError, match={
        "broken": "probe|decode",
        "black": "black",
        "constant": "constant",
        "noise": "noise",
        "missing-audio": "audio",
        "stale-cache": "stale",
    }[kind]):
        validate(artifact, require_audio=True, expected_dimensions=expected, prior=prior)


def make_composite_job(request):
    make = function(
        "make_video_job",
        normalize_video_source=lambda value: value,
        STYLES={"none": {"prefix": ""}},
        fal_ready=lambda: True,
        _h3ref=SimpleNamespace(
            wants_singularity=lambda value: False, H3_SINGULARITY_VARIANT="singularity",
            required_turbo_preset=lambda value: None,
            normalize_references=lambda value: value,
            assert_ref_count_ok=lambda value: None,
            resolve_reference_detail=lambda value: value,
            normalize_video_references=lambda value: value,
        ),
        engine_frames=lambda engine, seconds: 124 if engine == "h3" else 121,
        SIZES={"landscape": (1280, 704)},
        H3_SIZES={"landscape": (1344, 768)},
        cast_lines=lambda value: [],
        engine_up=lambda value: False,
        h3_variant_warm=lambda singularity: False,
        engine_licences=importlib.import_module("media_lab_core.engine_licences"),
        submit_job=lambda kind, request, extra: {"kind": kind, "request": request, **extra},
    )
    return make(request)


def test_composite_job_uses_h3_geometry_and_one_queue_identity():
    job = make_composite_job({
        "model": "h3-ltx25", "prompt": "A paper bird takes flight.",
        "duration": "5", "orientation": "landscape", "seed": 424242,
    })
    assert job["engine"] == "h3-ltx25"
    assert job["frames"] >= 124
    assert (job["w"], job["h"]) == (1344, 768)
    assert job["request"]["model"] == "h3-ltx25"


def test_composite_job_is_scheduled_as_h3_first():
    job_engine = function("job_engine")
    assert job_engine({"kind": "video", "engine": "h3-ltx25",
                       "request": {"model": "h3-ltx25"}}) == "h3"


def test_two_stage_runner_preserves_stage_a_and_passes_same_seed(tmp_path):
    pool = tmp_path / "pool"
    jobs_dir = tmp_path / "jobs"
    h3_out = pool / "h3-out"
    ltx_out = pool / "ltx-out"
    h3_out.mkdir(parents=True)
    ltx_out.mkdir(parents=True)
    calls = []
    finished = []

    def generate(engine, body, job, timeout=0):
        calls.append((engine, dict(body), job["stage"]))
        out_dir = h3_out if engine == "h3" else ltx_out
        name = f"job-{job['id']}.mp4"
        (out_dir / name).write_bytes((engine + "-artifact").encode())
        return {"ok": True, "file": name, "seed": body["seed"]}

    def mux_audio(refined_video, stage_a, output):
        output.write_bytes(b"ltx-video+h3-audio")
        return output

    def finish(job, out, **kwargs):
        finished.append(Path(out))
        job.update(status="done", url=f"/media/{job['id']}.mp4")
        return {"sha256": "published", "video_frames_decoded": 40,
                "audio_bytes_decoded": 80000}

    run = function(
        "_run_h3_ltx_video",
        _h3ref=SimpleNamespace(wants_singularity=lambda request: False),
        Path=Path,
        base64=__import__("base64"),
        json=json,
        shutil=__import__("shutil"),
        hashlib=hashlib,
        subprocess=__import__("subprocess"),
        POOL_DIR=pool,
        JOBS_DIR=jobs_dir,
        H3_LTX_RETAKE_STRENGTH=0.35,
        H3_LTX_REFINEMENT_PROMPT="Preserve continuity.",
        h3_prompt=lambda prompt, **kwargs: prompt,
        engine_generate=generate,
        touch_engine=lambda engine: None,
        fail=lambda job, message, detail="": job.update(status="error", message=message, detail=str(detail)),
        save_state=lambda: None,
        _finish_video=finish,
        _mux_h3_audio_onto_ltx=mux_audio,
        _artifact_inventory=lambda directory: {},
        _validate_media_artifact=lambda path, **kwargs: {
            "sha256": hashlib.sha256(Path(path).read_bytes()).hexdigest(),
            "video_frames_decoded": 40,
            "audio_bytes_decoded": 80000 if kwargs.get("require_audio") else 0,
        },
        _sha256_file=lambda path: hashlib.sha256(Path(path).read_bytes()).hexdigest(),
        _write_h3_ltx_receipt=lambda job_dir, payload: (job_dir / "h3-ltx-receipt.json").write_text(
            json.dumps(payload, sort_keys=True)),
    )
    job = {
        "id": "abc123", "engine": "h3-ltx25", "frames": 124, "w": 1344, "h": 768,
        "full_prompt": "A paper bird takes flight.", "request": {"seed": 424242},
    }
    run(job, references=[], staged_video_refs=[], start_b64="")

    assert [call[0] for call in calls] == ["h3", "ltx"]
    assert calls[0][1]["seed"] == calls[1][1]["seed"] == 424242
    assert calls[1][1]["input_video_file"] == "abc123-stage-a.mp4"
    assert calls[1][1]["retake_strength"] == pytest.approx(0.35)
    assert calls[1][1]["regenerate_audio"] is False
    assert calls[1][1]["reference_pipeline"] is True
    assert (jobs_dir / "abc123" / "stage-a-h3.mp4").read_bytes() == b"h3-artifact"
    receipt = json.loads((jobs_dir / "abc123" / "h3-ltx-receipt.json").read_text())
    assert receipt["seed"] == 424242
    assert receipt["status"] == "complete"
    assert receipt["stages"][0]["engine"] == "h3"
    assert receipt["stages"][1]["engine"] == "ltx25"
    assert receipt["stages"][1]["raw_ltx_artifact"] == "job-abc123.mp4"
    assert receipt["stages"][1]["audio_source_artifact"] == "stage-a-h3.mp4"
    assert receipt["stages"][1]["artifact"] == "stage-b-ltx-with-h3-audio.mp4"
    assert receipt["stages"][0]["validation"]["video_frames_decoded"] == 40
    assert receipt["stages"][1]["raw_ltx_validation"]["video_frames_decoded"] == 40
    assert receipt["stages"][1]["validation"]["audio_bytes_decoded"] == 80000
    assert receipt["published"]["validation"]["sha256"] == "published"
    muxed = jobs_dir / "abc123" / "stage-b-ltx-with-h3-audio.mp4"
    assert muxed.read_bytes() == b"ltx-video+h3-audio"
    assert finished == [muxed]


def test_h3_audio_remux_copies_refined_video_and_stage_a_audio(tmp_path):
    refined = tmp_path / "refined.mp4"
    stage_a = tmp_path / "stage-a.mp4"
    output = tmp_path / "muxed.mp4"
    refined.write_bytes(b"video")
    stage_a.write_bytes(b"audio")
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        Path(command[-1]).write_bytes(b"muxed")
        return SimpleNamespace(returncode=0, stderr="")

    mux = function(
        "_mux_h3_audio_onto_ltx",
        Path=Path,
        subprocess=SimpleNamespace(run=run),
        os=__import__("os"),
    )
    assert mux(refined, stage_a, output) == output
    command, kwargs = calls[0]
    assert command[:6] == ["ffmpeg", "-nostdin", "-v", "error", "-y", "-i"]
    assert command[6] == str(refined)
    assert command[7:9] == ["-i", str(stage_a)]
    assert ["-map", "0:v:0"] == command[9:11]
    assert ["-map", "1:a:0"] == command[11:13]
    assert ["-c:v", "copy", "-c:a", "copy"] == command[13:17]
    assert "-shortest" not in command
    assert kwargs == {"capture_output": True, "text": True}
    assert output.read_bytes() == b"muxed"


def test_stage_a_failure_never_starts_ltx(tmp_path):
    calls = []
    run = function(
        "_run_h3_ltx_video",
        _h3ref=SimpleNamespace(wants_singularity=lambda request: False),
        Path=Path,
        base64=__import__("base64"), json=json, shutil=__import__("shutil"),
        hashlib=hashlib, subprocess=__import__("subprocess"),
        POOL_DIR=tmp_path / "pool", JOBS_DIR=tmp_path / "jobs",
        H3_LTX_RETAKE_STRENGTH=0.35,
        H3_LTX_REFINEMENT_PROMPT="Preserve continuity.",
        h3_prompt=lambda prompt, **kwargs: prompt,
        engine_generate=lambda engine, body, job, timeout=0: calls.append(engine) or
            {"ok": False, "error": "stage-a exploded"},
        touch_engine=lambda engine: None,
        fail=lambda job, message, detail="": job.update(status="error", message=message, detail=str(detail)),
        save_state=lambda: None, _finish_video=lambda *args: None,
        _artifact_inventory=lambda directory: {},
        _validate_media_artifact=lambda path, **kwargs: {},
        _sha256_file=lambda path: "unused",
        _write_h3_ltx_receipt=lambda *args: None,
    )
    job = {"id": "failed", "engine": "h3-ltx25", "frames": 124,
           "w": 1344, "h": 768, "full_prompt": "fixture", "request": {"seed": 7}}
    run(job, references=[], staged_video_refs=[], start_b64="")
    assert calls == ["h3"]
    assert job["status"] == "error"
    assert "H3 draft failed" in job["message"]


def test_stage_a_decoded_media_failure_never_starts_ltx(tmp_path):
    pool = tmp_path / "pool"
    (pool / "h3-out").mkdir(parents=True)
    calls = []

    def generate(engine, body, job, timeout=0):
        calls.append(engine)
        output = pool / "h3-out" / "bad.mp4"
        output.write_bytes(b"nonempty-but-broken")
        return {"ok": True, "file": output.name}

    run = function(
        "_run_h3_ltx_video",
        _h3ref=SimpleNamespace(wants_singularity=lambda request: False),
        Path=Path, base64=__import__("base64"), json=json,
        shutil=__import__("shutil"), hashlib=hashlib,
        subprocess=subprocess, POOL_DIR=pool, JOBS_DIR=tmp_path / "jobs",
        H3_LTX_RETAKE_STRENGTH=0.35, H3_LTX_REFINEMENT_PROMPT="Preserve continuity.",
        h3_prompt=lambda prompt, **kwargs: prompt, engine_generate=generate,
        touch_engine=lambda engine: None, save_state=lambda: None,
        fail=lambda job, message, detail="": job.update(status="error", message=message, detail=str(detail)),
        _artifact_inventory=lambda directory: {},
        _validate_media_artifact=lambda path, **kwargs: (_ for _ in ()).throw(RuntimeError("video decode failed")),
        _write_h3_ltx_receipt=lambda *args: None,
        _sha256_file=lambda path: "unused", _finish_video=lambda *args, **kwargs: None,
        _mux_h3_audio_onto_ltx=lambda *args: None,
    )
    job = {"id": "badmedia", "engine": "h3-ltx25", "frames": 124,
           "w": 1344, "h": 768, "full_prompt": "fixture", "request": {"seed": 9}}
    run(job, references=[], staged_video_refs=[], start_b64="")
    assert calls == ["h3"]
    assert job["status"] == "error"
    assert "decoded video/audio QA" in job["message"]


def test_ltx_engine_accepts_only_staged_basename_video_inputs():
    source = ENGINE.read_text()
    assert "input_video_file" in source
    assert "Path(str(req.get('input_video_file') or '')).name" in source
    assert "LTX_INPUT_DIR" in source
    assert "extra['retake_video']" in source
    assert "extra['retake_strength']" in source
    assert "extra['regenerate_audio']" in source


def test_decord_compat_reader_uses_bounded_installed_pyav(monkeypatch):
    class Frame:
        def __init__(self, value):
            self.value = value

        def to_ndarray(self, format):
            assert format == "rgb24"
            return SimpleNamespace(shape=(16, 24, 3), value=self.value)

    class Container:
        streams = SimpleNamespace(video=[SimpleNamespace(average_rate=24)])

        def decode(self, video):
            assert video == 0
            return iter([Frame(1), Frame(2)])

        def close(self):
            return None

    fake_av = types.ModuleType("av")
    setattr(fake_av, "open", lambda path: Container())
    monkeypatch.setitem(sys.modules, "av", fake_av)
    monkeypatch.delitem(sys.modules, "decord", raising=False)
    tree = ast.parse(ENGINE.read_text())
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                and n.name == "_install_decord_compat")
    namespace = {"importlib": importlib, "sys": sys, "types": types}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(ENGINE), "exec"), namespace)
    install = namespace["_install_decord_compat"]
    assert install() == "pyav-compat"
    decord = importlib.import_module("decord")
    reader = decord.VideoReader("stage-a.mp4")
    assert len(reader) == 2
    assert reader.get_avg_fps() == pytest.approx(24.0)
    assert reader[1].shape == (16, 24, 3)


def test_retake_dtype_compat_casts_hidden_states_at_both_projection_boundaries(monkeypatch):
    class Tensor:
        def __init__(self, dtype):
            self.dtype = dtype

        def to(self, *, dtype):
            return Tensor(dtype)

    class FeatureExtractor:
        def parameters(self):
            return iter([SimpleNamespace(dtype="bfloat16")])

    calls = []
    base_module = types.ModuleType("base_encoder")
    connector_module = types.ModuleType("embeddings_connector")

    def original(hidden_states, attention_mask, padding_side, feature_extractor):
        calls.append((hidden_states, attention_mask, padding_side, feature_extractor))
        return "ok"

    setattr(base_module, "_apply_feature_extractor", original)

    class GateProjection:
        def __init__(self):
            self.weight = SimpleNamespace(dtype="bfloat16")

        def forward(self, hidden_states):
            calls.append(("gate", hidden_states))
            return hidden_states

        def __call__(self, hidden_states):
            return self.forward(hidden_states)

    class Attention:
        def __init__(self):
            self.to_gate_logits = GateProjection()

    class Embeddings1DConnector:
        def __init__(self):
            self.attention = Attention()

        def parameters(self):
            return iter([SimpleNamespace(dtype="bfloat16")])

        def modules(self):
            return iter([self, self.attention])

        def forward(self, hidden_states, attention_mask=None):
            calls.append((hidden_states, attention_mask))
            # Maestro's cached retake path rebuilds this gate input as float32
            # after the connector boundary has already been aligned.
            return self.attention.to_gate_logits(Tensor("float32"))

    video_connector = Embeddings1DConnector()
    audio_connector = Embeddings1DConnector()
    ltx_model = SimpleNamespace(
        video_embeddings_connector=video_connector,
        audio_embeddings_connector=audio_connector,
    )
    setattr(connector_module, "Embeddings1DConnector", Embeddings1DConnector)

    def import_module(name):
        return connector_module if name.endswith("embeddings_connector") else base_module

    monkeypatch.setattr(importlib, "import_module", import_module)
    tree = ast.parse(ENGINE.read_text())
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                and n.name == "_install_ltx_retake_dtype_compat")
    namespace = {"importlib": importlib}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(ENGINE), "exec"), namespace)
    install = namespace["_install_ltx_retake_dtype_compat"]
    marker = "hidden-state-and-live-connector-gates-dtype-aligned"
    assert install(ltx_model) == marker
    result = base_module._apply_feature_extractor(
        (Tensor("float32"), Tensor("float32")), "mask", "right", FeatureExtractor())
    assert result == "ok"
    assert [tensor.dtype for tensor in calls[0][0]] == ["bfloat16", "bfloat16"]
    video_result = video_connector.forward(Tensor("float32"), "video-mask")
    audio_result = audio_connector.forward(Tensor("float32"), "audio-mask")
    assert video_result.dtype == "bfloat16"
    assert audio_result.dtype == "bfloat16"
    connector_calls = [call for call in calls if call[0] != "gate" and len(call) == 2]
    gate_calls = [call for call in calls if call[0] == "gate"]
    assert [call[0].dtype for call in connector_calls] == ["bfloat16", "bfloat16"]
    assert [call[1].dtype for call in gate_calls] == ["bfloat16", "bfloat16"]
    assert install(ltx_model) == marker


def test_studio_ui_exposes_explicit_two_stage_choice():
    source = (ROOT / "static" / "index.html").read_text()
    assert 'data-v="h3-ltx25"' in source
    assert "H3 → LTX" in source
