"""Real / Long (H3 Singularity dual-sampling): geometry, graph and runtime.

The runtime tests start a fake ComfyUI (a tiny HTTP server written into a temp
"ComfyUI" dir) exactly the way the engine starts the real one, so start,
warm-up, render, refusal and stop run end to end without a GPU.
"""
import base64
import json
import os
import signal
import socket
import sys
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "runner"))
import h3_singularity as sing  # noqa: E402

PNG = base64.b64encode(
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00"
    b"\x1f\x15\xc4\x89\x00\x00\x00\rIDATx\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05\x18\xd8N\x00\x00"
    b"\x00\x00IEND\xaeB`\x82").decode()


# ------------------------------------------------------------------ geometry
@pytest.mark.parametrize("seconds,frames", [
    (3, 124), (5, 124), (124 / 24, 124), (5.17, 141), (5.2, 141), (8, 192), (10, 243),
    (12, 294), (15, 362), (15.1, 362), (20, 362), ("bad", 124), (float("nan"), 124)])
def test_frames_land_on_the_17k_plus_5_grid_and_never_clip_a_line(seconds, frames):
    got = sing.frames_for_seconds(seconds)
    assert (got - 5) % 17 == 0 and got == frames
    if isinstance(seconds, (int, float)) and seconds == seconds and 5 <= seconds <= 15.08:
        assert got / 24 >= seconds - 1e-9


def test_longer_than_trained_takes_need_an_explicit_host_cap():
    assert sing.frames_for_seconds(20, max_frames=481) == 481
    assert sing.frames_for_seconds(30, max_frames=10_000) == sing.HARD_MAX_FRAMES
    assert sing.aligned_frames(124) == 124 and sing.aligned_frames(125) == 141


def test_canvas_matches_the_measured_eval():
    assert sing.pass1_size("landscape") == (960, 544)
    assert sing.pass1_size("portrait") == (544, 960)
    assert sing.output_size("landscape") == (1216, 672)      # measured 1216x672
    assert sing.output_size("portrait") == (672, 1216)       # measured 672x1216
    assert sing.output_size("landscape", 1.5) == (1440, 832) # measured 1440x832
    w, h = sing.pass1_size("square")
    assert w == h and w % 32 == 0
    assert sing.orientation_of(1216, 672) == "landscape"
    assert sing.orientation_of(672, 1216) == "portrait"
    assert sing.orientation_of(1024, 1000) == "square"


# --------------------------------------------------------------------- graph
def _graph(**kw):
    base = dict(prompt="p", frames=124, orientation="landscape", seed=7, prefix="rid")
    base.update(kw)
    return sing.build_graph(**base)


def test_audio_and_video_leave_pass_one_at_the_same_noise_level():
    g = _graph()
    # video: pass-1 denoised prediction -> upscale -> zero-step re-noise to the split sigma
    assert g["104"]["inputs"]["av_latent"] == ["108", 1]
    assert g["124"]["inputs"]["latent"] == ["104", 0]
    assert g["128"]["inputs"]["sigmas"] == ["100", 0]
    # audio: pass-1 state AT the split sigma, rejoined with that video
    assert g["102"]["inputs"]["av_latent"] == ["108", 0]
    assert g["105"]["inputs"] == {"video_latent": ["128", 0], "audio_latent": ["102", 1]}
    # pass 2 continues from the split without fresh noise; one decode for both streams
    assert g["106"]["inputs"]["noise"] == ["114", 0] and g["106"]["inputs"]["sigmas"] == ["99", 1]
    assert g["109"]["inputs"]["samples"] == ["106", 1] == g["110"]["inputs"]["samples"]


def test_mux_uses_the_models_own_rate_and_never_trims_to_audio():
    g = _graph(frames=362)
    combine = g["141"]["inputs"]
    assert combine["frame_rate"] == sing.FPS == 24
    assert combine["trim_to_audio"] is False and combine["save_metadata"] is False
    assert g["56"]["inputs"]["length"] == 362
    assert (g["56"]["inputs"]["width"], g["56"]["inputs"]["height"]) == (960, 544)


def test_loras_turbo_then_lms_then_realism():
    g = _graph()
    assert g["118"]["inputs"]["lora_02"] == sing.DEFAULT_MODELS["turbo_lora"]
    assert g["127"]["inputs"] == {"model": ["118", 0], "lora_name": sing.DEFAULT_MODELS["lms_lora"],
                                  "strength_model": 0.5}
    assert g["199"]["inputs"]["model"] == ["127", 0] and g["199"]["inputs"]["strength_model"] == 1.0
    assert g["126"]["inputs"]["model"] == ["199", 0]
    assert g["57"]["inputs"]["model"] == ["118", 0]  # pass 1 is Turbo only


def test_references_are_wired_per_type_and_capped():
    g = _graph(image_refs=[f"r{i}.png" for i in range(9)],
               video_refs=[{"path": "/x/v.mp4", "skip": 12, "cap": 124}] * 3,
               audio_refs=[{"path": "/x/a.wav"}] * 3)
    inputs = g["56"]["inputs"]
    assert inputs["ref_images.ref_image_8"] == ["178", 0]
    assert g["180"]["inputs"]["force_rate"] == 24 and g["180"]["inputs"]["skip_first_frames"] == 12
    assert g["180"]["inputs"]["format"] == "None"   # no loader-side resize or rate preset
    assert inputs["ref_videos.ref_video_2"] == ["182", 0]
    assert inputs["ref_audios.ref_audio_2"] == ["192", 0]
    assert not any(k.startswith("ref_video_audios") for k in inputs)  # soundtracks never condition
    for kw in ({"image_refs": ["a"] * 10}, {"video_refs": [{"path": "v"}] * 4},
               {"audio_refs": [{"path": "a"}] * 4}, {"ref_image_size": "huge"}, {"frames": 125}):
        with pytest.raises(sing.SingularityRequestError):
            _graph(**kw)


def test_prompt_gets_trigger_and_subject_map_but_keeps_a_directors_map():
    p = sing.compose_prompt("She waves.", ["woman", ""], ["dance moves"], ["her voice"])
    assert p.startswith(sing.TRIGGER)
    assert "<Subject 1> is the woman in <Picture 1>." in p
    assert "<Subject 2> is the person in <Picture 2>." in p
    assert "<Video 1> provides the dance moves." in p and "<Audio 1> provides the her voice." in p
    assert p.rstrip().endswith("detailed_description: She waves.")
    mapped = "subject_definitions: <Subject 1> is the man in <Picture 1>. detailed: runs"
    assert sing.compose_prompt(mapped, ["x"]) == f"{sing.TRIGGER}\n\n{mapped}"
    start = sing.compose_prompt("A street.", ["woman", "opening frame"], start_frame_picture=2)
    assert "<Picture 2> is the opening frame" in start and "<Subject 2>" not in start
    assert sing.compose_prompt(f"{sing.TRIGGER} text") == f"{sing.TRIGGER} text"


def test_warm_graph_touches_every_model_cheaply():
    g = sing.warm_graph("warm")
    classes = {n["class_type"] for n in g.values()}
    assert {"UNETLoader", "CLIPLoader", "VAELoader", "MinimaxH3LatentUpscaler3D",
            "LoraLoaderModelOnly", "VHS_VideoCombine"} <= classes
    assert g["56"]["inputs"]["length"] == 5
    assert (g["56"]["inputs"]["width"], g["56"]["inputs"]["height"]) == (256, 256)


def test_model_names_come_from_the_environment_as_bare_names():
    models = sing.models_from_env({"H3_SINGULARITY_DIT": "/w/other_dit.safetensors"})
    assert models["dit"] == "other_dit.safetensors"
    assert models["text_encoder"] == sing.DEFAULT_MODELS["text_encoder"]


def test_extra_model_paths_come_from_local_config():
    yaml = sing.extra_model_paths_yaml({"H3_SINGULARITY_MODELS_ROOT": "/m", "H3_SINGULARITY_EXTRA_LORAS": "/l",
                                        "H3_SINGULARITY_UPSCALERS": "/u"})
    assert "base_path: /m" in yaml and "    /m/loras" in yaml and "    /l" in yaml
    assert "latent_upscale_models: /u" in yaml
    with pytest.raises(sing.SingularityConfigError):
        sing.extra_model_paths_yaml({})


# ------------------------------------------------------------ fake renderer
FAKE_COMFY = textwrap.dedent('''
    import json, sys, threading, uuid, os
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    args = sys.argv[1:]
    port = int(args[args.index("--port") + 1]); out = args[args.index("--output-directory") + 1]
    log = os.path.join(out, "graphs.jsonl")
    HIST = {}
    class H(BaseHTTPRequestHandler):
        def log_message(self, *a): pass
        def _send(self, code, obj):
            b = json.dumps(obj).encode(); self.send_response(code)
            self.send_header("Content-Type", "application/json"); self.send_header("Content-Length", str(len(b)))
            self.end_headers(); self.wfile.write(b)
        def do_GET(self):
            if self.path == "/system_stats": return self._send(200, {"system": {}})
            if self.path.startswith("/history/"):
                pid = self.path.rsplit("/", 1)[1]
                return self._send(200, {pid: HIST[pid]} if pid in HIST else {})
            self._send(404, {})
        def do_POST(self):
            n = int(self.headers.get("Content-Length") or 0); body = json.loads(self.rfile.read(n) or b"{}")
            if self.path == "/interrupt": return self._send(200, {})
            graph = body["prompt"]
            open(log, "a").write(json.dumps(graph) + "\\n")
            if any(node["class_type"] == "UnknownNode" for node in graph.values()):
                return self._send(400, {"error": "invalid prompt", "node_errors": {}})
            pid = uuid.uuid4().hex
            prefix = graph["141"]["inputs"]["filename_prefix"]
            if "FAILME" in graph["56"]["inputs"]["prompt"]:
                HIST[pid] = {"status": {"status_str": "error", "completed": False, "messages": [
                    ["execution_error", {"node_type": "SamplerCustomAdvanced", "exception_message": "CUDA out of memory"}]]},
                    "outputs": {}}
            else:
                path = os.path.join(out, prefix + "_00001.mp4"); open(path, "wb").write(b"fake mp4")
                HIST[pid] = {"status": {"status_str": "success", "completed": True, "messages": []},
                             "outputs": {"141": {"gifs": [{"filename": prefix + "_00001.mp4", "subfolder": "",
                                                           "fullpath": path}]}}}
            self._send(200, {"prompt_id": pid})
    ThreadingHTTPServer(("127.0.0.1", port), H).serve_forever()
''')


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture
def fake_env(tmp_path):
    comfy = tmp_path / "ComfyUI"
    comfy.mkdir()
    (comfy / "main.py").write_text(FAKE_COMFY)
    media = tmp_path / "h3-out"
    media.mkdir()
    return {"H3_SINGULARITY_COMFY_DIR": str(comfy), "H3_SINGULARITY_PYTHON": sys.executable,
            "H3_SINGULARITY_MODELS_ROOT": str(tmp_path / "models"),
            "H3_SINGULARITY_PORT": str(_free_port())}, tmp_path / "runtime", media


def test_pipeline_starts_warms_renders_and_stops_its_process_group(fake_env):
    env, runtime, media = fake_env
    (media / "motion.mp4").write_bytes(b"v")
    pipe = sing.SingularityPipeline(env, runtime, log=lambda *a: None, media_dir=media)
    pipe.start()
    try:
        pgid = pipe.process.pid
        assert pipe.warm_s is not None
        row = pipe.generate({"prompt": "She says hi.", "frames": 300, "orientation": "portrait", "seed": 5,
                             "references": [{"b64": PNG, "role": "woman"}],
                             "video_references": [{"file": "motion.mp4", "role": "motion"}]}, "job1")
        assert Path(row["output"]).read_bytes() == b"fake mp4"
        assert row["frames"] == 311 and row["orientation"] == "portrait"
        graphs = [json.loads(line) for line in
                  (runtime / "singularity" / "out" / "graphs.jsonl").read_text().splitlines()]
        assert len(graphs) == 2 and graphs[0]["56"]["inputs"]["length"] == 5   # warm-up first
        take = graphs[1]
        assert take["56"]["inputs"]["length"] == 311
        assert (take["56"]["inputs"]["width"], take["56"]["inputs"]["height"]) == (544, 960)
        assert take["170"]["inputs"]["image"] == "job1-ref0.png"
        assert take["180"]["inputs"]["video"] == str(media / "motion.mp4")
        assert take["180"]["inputs"]["skip_first_frames"] == 0   # staged already trimmed
        assert take["56"]["inputs"]["prompt"].startswith(sing.TRIGGER)
        assert (runtime / "singularity" / "in" / "job1-ref0.png").is_file()
        sing.sweep_inputs(runtime, "job1")
        assert not (runtime / "singularity" / "in" / "job1-ref0.png").exists()
        assert not Path(row["output"]).exists()                    # renderer copy swept
        assert (runtime / "singularity" / "out" / "graphs.jsonl").exists()
    finally:
        pipe.close()
    with pytest.raises(ProcessLookupError):
        os.killpg(pgid, 0)


def test_request_errors_are_refused_before_any_render(fake_env):
    env, runtime, media = fake_env
    pipe = sing.SingularityPipeline(env, runtime, log=lambda *a: None, media_dir=media)
    with pytest.raises(sing.SingularityRequestError):
        pipe.request_spec({"references": [{"b64": PNG}] * 10}, "r")
    with pytest.raises(sing.SingularityRequestError):
        pipe.request_spec({"video_references": [{"file": "../../etc/passwd"}]}, "r")
    with pytest.raises(sing.SingularityRequestError):
        pipe.request_spec({"video_references": [{"file": "missing.mp4"}]}, "r")
    with pytest.raises(sing.SingularityRequestError):
        pipe.request_spec({"audio_references": [{"file": "x.wav"}] * 4}, "r")


def test_a_failed_node_is_a_render_error_and_a_refused_graph_is_config(fake_env):
    env, runtime, media = fake_env
    pipe = sing.SingularityPipeline(env, runtime, log=lambda *a: None, media_dir=media)
    pipe.start()
    try:
        with pytest.raises(sing.SingularityError) as failed:
            pipe.generate({"prompt": "FAILME", "frames": 124}, "job2")
        assert not isinstance(failed.value, (sing.SingularityConfigError, sing.SingularityRequestError))
        assert "out of memory" in str(failed.value)
        with pytest.raises(sing.SingularityConfigError):
            pipe.comfy.run({"1": {"class_type": "UnknownNode", "inputs": {}}}, timeout_s=10)
    finally:
        pipe.close()


def test_missing_runtime_is_a_config_error_not_a_crash(tmp_path):
    env = {"H3_SINGULARITY_COMFY_DIR": str(tmp_path / "nope"), "H3_SINGULARITY_MODELS_ROOT": str(tmp_path)}
    pipe = sing.SingularityPipeline(env, tmp_path, log=lambda *a: None)
    with pytest.raises(sing.SingularityConfigError):
        pipe.start()
    assert pipe.process is None
