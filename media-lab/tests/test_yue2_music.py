"""YuE2 as the primary music engine + the music edit tools.

Everything here runs on a laptop: the YuE2 shim is a tiny stand-in HTTP server
that writes a real (ffmpeg-made) FLAC, the stem separator is a shell script, and
the studio app is loaded under a disposable HOME exactly like test_cut_api.
Anything that needs the real engine on the Spark is marked `spark`.
"""
from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

REPO = Path(__file__).resolve().parents[1]
FFMPEG = shutil.which("ffmpeg") and shutil.which("ffprobe")
NOTICE = "Non-commercial use only (YuE2 weights are CC BY-NC 4.0)"
CAPTION = ("Global Metadata: Slow rainy-night jazz, brushed drums, late-night mood.\n\n"
           "Vocal Details: A warm, tired tenor.\n\n"
           "Arrangement: Piano opens, bass joins, a muted trumpet closes it.")
LYRICS = "[Verse]\nRain on the glass\nyour name on my lips\n[Chorus]\nCome home tonight\n"
ABC_WITH_CHORDS = 'X:1\nT:Ref\nM:4/4\nK:C\n"C"C D E F | "G7"G A B c |\n"Am"A2 G2 | "F"F4 |\n'


def _sine(path: Path, seconds=3, codec="flac"):
    args = ["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi",
            "-i", f"sine=frequency=440:sample_rate=48000:d={seconds}", "-ac", "2"]
    if codec == "mp3":
        args += ["-codec:a", "libmp3lame", "-q:a", "4"]
    subprocess.run(args + [str(path)], check=True, capture_output=True, timeout=120)


class FakeYue2:
    """Stand-in for runner/yue2_engine_server.py: same routes, same shapes."""

    def __init__(self, out_dir: Path):
        self.out_dir = out_dir
        self.calls: list[tuple[str, dict]] = []
        self.plan_abc = "X:1\nT:Plan\nK:C\nC D E F|\n"
        self.transcribe_abc = ABC_WITH_CHORDS
        srv = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, code, obj):
                b = json.dumps(obj).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                self._send(200, {"ok": True, "engine": "music", "impl": "yue2", "model": "YuE2-3B",
                                 "loaded": True, "busy": False, "phase": None,
                                 "cache": {"renders": len(srv.calls), "errors": 0, "last_error": None}})

            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                req = json.loads(self.rfile.read(n) or b"{}")
                path = self.path.split("?")[0]
                srv.calls.append((path, req))
                if path == "/generate":
                    rid = req["request_id"]
                    d = srv.out_dir / f"job-{rid}"
                    d.mkdir(parents=True, exist_ok=True)
                    _sine(d / "audio.flac", seconds=3)
                    self._send(200, {"ok": True, "file": str(d / "audio.flac"),
                                     "abc": req.get("abc") or "X:1\nT:Made\nK:C\nE F G A|\n",
                                     "seconds": 3.0, "elapsed": 0.1, "seed": req.get("seed"),
                                     "sample_rate": 48000, "truncated": False, "dir": str(d),
                                     "timing": {"e2e_seconds": 0.1}, "cached": False})
                elif path == "/plan":
                    self._send(200, {"ok": True, "abc": srv.plan_abc, "elapsed": 0.1,
                                     "seed": req.get("seed"), "truncated": False})
                elif path == "/transcribe":
                    self._send(200, {"ok": True, "abc": srv.transcribe_abc,
                                     "dir": str(srv.out_dir / "t"), "elapsed": 0.1})
                elif path == "/interrupt":
                    self._send(200, {"ok": True, "interrupted": False})
                else:
                    self._send(404, {"ok": False, "error": "not found"})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture(scope="module")
def studio(tmp_path_factory):
    if not FFMPEG:
        pytest.skip("ffmpeg/ffprobe are required")
    home = tmp_path_factory.mktemp("yue2-home")
    root = home / "media-lab-simple"
    root.mkdir()
    for name in ("static", "config"):
        (root / name).symlink_to(REPO / name)
    old = {k: os.environ.get(k) for k in ("HOME", "MEDIA_LAB_DISABLE_BACKGROUND_WORKERS")}
    os.environ["HOME"] = str(home)
    os.environ["MEDIA_LAB_DISABLE_BACKGROUND_WORKERS"] = "1"
    spec = importlib.util.spec_from_file_location("yue2_test_app", REPO / "app.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["yue2_test_app"] = module
    try:
        spec.loader.exec_module(module)
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    module.INFERENCE_LOCK = str(root / "inference.lock")
    module.pick_next_job = lambda: None
    yield module


@pytest.fixture
def shim(studio, tmp_path, monkeypatch):
    fake = FakeYue2(tmp_path / "music-out")
    monkeypatch.setitem(studio.ENGINES["yue2"], "port", fake.port)
    monkeypatch.setattr(studio, "ensure_engine", lambda name, j=None: "up")
    monkeypatch.setattr(studio, "_ensure_engine_under_lease", lambda name, j=None: "up")
    monkeypatch.setattr(studio, "gpu_render_ready", lambda engine, task: object())
    monkeypatch.setattr(studio, "delegation_headers", lambda lease: {"X-Test-Fence": "1"})
    monkeypatch.setattr(studio, "_gpu_finish_job_operation", lambda *args: None)
    @contextmanager
    def fake_gpu_operation(*args, **kwargs):
        yield object()
    monkeypatch.setattr(studio, "gpu_operation", fake_gpu_operation)
    monkeypatch.setattr(studio, "touch_engine", lambda name: None)
    monkeypatch.setattr(studio, "qwen_json",
                        lambda system, user, max_tokens=2400: {"caption": CAPTION, "lyrics": LYRICS})
    yield fake
    fake.close()


def _client(studio, signed=True):
    client = TestClient(studio.app, base_url="http://127.0.0.1")
    if signed:
        assert client.post("/api/gate", json={"code": studio.ACCESS_CODE}).status_code == 200
    return client


# ---------------------------------------------------------------- request model

def test_music_request_defaults_to_yue2(studio):
    # The request leaves the engine to the host: YuE2 where the owner opted into
    # its non-commercial licence (the suite does, see conftest), else Music 3.
    r = studio.MusicReq(vibe="x")
    assert (r.engine, r.style, r.cot, r.abc, r.reference_song_id, r.seed, r.instrumental) == \
        ("", "", "full", "", "", None, False)
    assert studio.default_music_engine() == "yue2"
    assert r.length == "auto" and r.duration_seconds is None


def test_api_music_validates_engine_fields(studio, shim):
    client = _client(studio)
    assert client.post("/api/music", json={"vibe": "x", "engine": "acestep"}).status_code == 400
    assert client.post("/api/music", json={"vibe": "x", "cot": "sideways"}).status_code == 400
    assert client.post("/api/music", json={"vibe": "x", "reference_song_id": "nope"}).status_code == 400
    assert client.post("/api/music", json={"vibe": "x", "engine": "music3", "abc": "X:1"}).status_code == 400
    ok = client.post("/api/music", json={"vibe": "rainy jazz", "duration_seconds": 30})
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body["engine"] == "yue2" and body["license"] == "CC-BY-NC-4.0"
    j = studio.jobs[body["id"]]
    assert j["engine"] == "yue2" and j["license"] == "CC-BY-NC-4.0" and j["request"]["engine"] == "yue2"
    assert studio.eta_key(j) == "music/yue2/30.0/warm" or studio.eta_key(j) == "music/yue2/30.0/cold"
    m3 = client.post("/api/music", json={"vibe": "rainy jazz", "engine": "music3", "duration_seconds": 30})
    assert m3.status_code == 200 and m3.json()["engine"] == "music3" and m3.json()["license"] == ""
    assert "license" not in studio.jobs[m3.json()["id"]]
    assert "/yue2/" not in studio.eta_key(studio.jobs[m3.json()["id"]])


def test_music_routes_sit_behind_the_session_gate(studio):
    # Loopback callers are trusted as plain users (by design), so the gate is
    # proven by exemption: none of the music routes is a public path.
    for path in ("/api/music", "/api/music/plan", "/api/music/engines", "/api/music/abc123/abc",
                 "/api/music/abc123/rearrange", "/api/music/abc123/cover", "/api/music/abc123/stems"):
        assert not studio.gate_exempt(studio.gate_path(path)), path


# ------------------------------------------------------------ engine registry

def test_engine_registry_and_residency_membership(studio):
    e = studio.ENGINES["yue2"]
    assert e["kind"] == "unit" and e["unit"] == "media-lab-yue2.service"
    assert e["health"] == "/health" and e["gb"] == 18 and e["boot_wait"] == 240
    assert e["port"] == 8197 and "start_yue2_engine.sh" in e["cmd"]
    assert "yue2" in studio.COMPANION_ENGINE_NAMES and "yue2" in studio.COMPANION_NAMES
    assert "stems" in studio.COMPANION_JOB_KINDS and "stems" in studio.RUNNERS
    policy = json.loads((REPO / "config/companion-residency-policy.json").read_text())
    assert "yue2" in policy["companion_slot"]["members"]
    assert policy["measured_or_bounded_gib"]["yue2"]["license"] == "CC-BY-NC-4.0"
    assert (REPO / "config/media-lab-yue2.service").read_text().count("start_yue2_engine.sh") == 1
    assert studio.MUSIC_ENGINE_UNITS == {"yue2": "yue2", "music3": "music"}
    assert studio.YUE2_LICENSE_NOTICE == NOTICE


def test_reaper_and_stand_down_cover_yue2(studio, monkeypatch):
    stopped = []
    monkeypatch.setattr(studio, "engine_idle_s", lambda name: 999999)
    monkeypatch.setattr(studio, "engine_up", lambda name: name in ("yue2", "music"))
    monkeypatch.setattr(studio, "engine_busy", lambda name: name == "music")
    monkeypatch.setattr(studio, "stop_engine", lambda name: stopped.append(name))
    studio.reap_idle_engines()
    assert stopped == ["yue2"]            # idle YuE2 reaped; busy Music 3 left alone
    stopped.clear()
    monkeypatch.setattr(studio, "pplx_primary_healthy", lambda: True)
    monkeypatch.setattr(studio, "release_voice_weights", lambda: True)
    monkeypatch.setattr(studio, "engine_busy", lambda name: False)
    assert studio.stand_down_other_companions("ltx") == "up"
    assert set(stopped) == {"yue2", "music"}


def test_local_config_keys(studio):
    from media_lab_core import local_config
    values = local_config.load({})
    assert values["YUE2_PORT"] == "8197"
    assert values["YUE2_KIT"].endswith("runtime/yue2-iso")
    assert values["YUE2_MODELS_ROOT"].endswith("media-lab-p3-models/yue2")
    assert values["MELBAND_ROFORMER_ROOT"].endswith("melband-roformer-0.1.5")
    assert local_config.yue2_port() == 8197
    example = (REPO / "config/local.env.example").read_text()
    for key in ("YUE2_KIT", "YUE2_MODELS_ROOT", "YUE2_PORT", "MELBAND_ROFORMER_ROOT"):
        assert f"\n{key}=" in example


# ---------------------------------------------------------------- helpers

def test_abc_strip_chords_and_style_line(studio):
    stripped = studio._abc_strip_chords(ABC_WITH_CHORDS)
    assert '"' not in stripped and "K:C" in stripped and "C D E F" in stripped
    assert studio._style_line_from_caption(CAPTION) == \
        "Slow rainy-night jazz, brushed drums, late-night mood"
    assert studio._style_line_from_caption("just a line") == "just a line"


# -------------------------------------------------------- run_music branches

def _submit(studio, **req):
    base = {"vibe": "rainy jazz", "lyrics": "", "length": "auto", "duration_seconds": 30,
            "engine": "yue2", "style": "", "cot": "full", "abc": "", "reference_song_id": "",
            "seed": None, "instrumental": False}
    base.update(req)
    assert studio._validate_music_request(base) is None
    return studio._submit_music(base)


def test_run_music_yue2_records_through_the_shim(studio, shim):
    j = _submit(studio, seed=7)
    studio.run_music(j)
    assert j["status"] == "done", j.get("message")
    path, body = shim.calls[-1]
    assert path == "/generate"
    assert body["style"] == "Slow rainy-night jazz, brushed drums, late-night mood"
    assert body["lyrics"] == LYRICS.strip() and body["cot"] == "full" and body["seed"] == 7
    # The requested length is a target; the engine gets a safety ceiling
    # (max(secs + 90, 1.75 * secs), at most 360 s) so songs end naturally.
    assert body["max_seconds"] == 120 and body["request_id"] == f"{j['id']}-a1" and "abc" not in body
    assert j["engine"] == "yue2" and j["license"] == "CC-BY-NC-4.0" and j["abc"].startswith("X:1")
    assert j["style_line"] == body["style"] and j["yue2"]["sample_rate"] == 48000
    jd = studio.JOBS_DIR / j["id"]
    assert (jd / "score.abc").read_text() == j["abc"]
    assert json.loads((jd / "result.json").read_text())["ok"] is True
    assert (studio.MEDIA / f"{j['id']}.mp3").stat().st_size > 0
    assert (studio.MEDIA / f"{j['id']}-wave.png").exists()
    row = next(x for x in json.loads((studio.ROOT / "gallery.json").read_text()) if x["id"] == j["id"])
    assert row["engine"] == "yue2" and row["license"] == "CC-BY-NC-4.0" and row["kind"] == "music"
    # the shim's job directory is its cache: never deleted by the app
    assert Path(json.loads((jd / "result.json").read_text())["file"]).exists()


def test_run_music_yue2_style_and_instrumental_and_score(studio, shim):
    j = _submit(studio, style="dreamy indie pop, warm synths", instrumental=True,
                abc="X:1\nT:Mine\nK:G\nG A B c|\n", cot="melody")
    studio.run_music(j)
    assert j["status"] == "done", j.get("message")
    _, body = shim.calls[-1]
    assert body["style"] == "dreamy indie pop, warm synths"
    assert body["lyrics"] == "[Instrumental]" and body["cot"] == "melody"
    assert body["abc"].startswith("X:1\nT:Mine") and j["abc"] == body["abc"]


def test_run_music_cover_transcribes_and_strips_chords(studio, shim):
    ref = studio.MEDIA / "refsong01.mp3"
    _sine(ref, seconds=4, codec="mp3")
    j = _submit(studio, reference_song_id="refsong01", style="lo-fi bossa nova")
    studio.run_music(j)
    assert j["status"] == "done", j.get("message")
    paths = [p for p, _ in shim.calls[-2:]]
    assert paths == ["/transcribe", "/generate"]
    t_body = shim.calls[-2][1]
    assert t_body["audio_path"] == str(ref) and t_body["task"] == "melody-full"
    g_body = shim.calls[-1][1]
    assert g_body["cot"] == "melody" and '"' not in g_body["abc"] and "C D E F" in g_body["abc"]
    assert j["reference_song_id"] == "refsong01" and j["cot"] == "melody"
    assert (studio.JOBS_DIR / j["id"] / "reference.abc").read_text() == ABC_WITH_CHORDS


def test_run_music_shim_busy_is_reported_as_busy(studio, shim, monkeypatch):
    def refuse(url, payload=None, timeout=10, **kwargs):
        raise RuntimeError(f"HTTP 409 from {url}: busy")
    monkeypatch.setattr(studio, "http_json", refuse)
    j = _submit(studio)
    studio.run_music(j)
    assert j["status"] == "error" and j["message"] == studio.BUSY_MSG


def test_run_music_music3_branch_is_unchanged(studio, shim, monkeypatch):
    out = studio.COMFY_MUSIC_DIR / "output/music3-lab"
    out.mkdir(parents=True, exist_ok=True)
    seen = {}

    def fake_comfy(port, graph, timeout_s, poll_s=4, job=None):
        seen["port"] = port
        seen["caption"] = graph["13"]["inputs"]["caption"]
        _sine(out / f"LAB_{job['id']}_a1_00001_.flac", seconds=3)
    monkeypatch.setattr(studio, "comfy_run", fake_comfy)
    j = _submit(studio, engine="music3")
    studio.run_music(j)
    assert j["status"] == "done", j.get("message")
    assert seen["port"] == 8196 and seen["caption"] == CAPTION
    assert j["engine"] == "music3" and "license" not in j and "abc" not in j
    assert all(p != "/generate" for p, _ in shim.calls)
    row = next(x for x in json.loads((studio.ROOT / "gallery.json").read_text()) if x["id"] == j["id"])
    assert row["engine"] == "music3" and "license" not in row


# ------------------------------------------------------------- edit routes

def test_plan_route_returns_a_score(studio, shim):
    client = _client(studio)
    assert client.post("/api/music/plan", json={"style": ""}).status_code == 400
    assert client.post("/api/music/plan", json={"style": "x", "cot": "off"}).status_code == 400
    r = client.post("/api/music/plan", json={"style": "bright ska", "lyrics": "[Verse]\nla la", "seed": 3})
    assert r.status_code == 200, r.text
    assert r.json()["abc"] == shim.plan_abc and r.json()["license"] == "CC-BY-NC-4.0"
    path, body = shim.calls[-1]
    assert path == "/plan" and body["style"] == "bright ska" and body["seed"] == 3 and body["cot"] == "full"


def test_abc_rearrange_and_cover_routes(studio, shim):
    client = _client(studio)
    assert client.get("/api/music/nosuchsong/abc").status_code == 404
    src = _submit(studio, style="smoky jazz")
    studio.run_music(src)
    assert src["status"] == "done"
    got = client.get(f"/api/music/{src['id']}/abc")
    assert got.status_code == 200 and got.json()["abc"] == src["abc"]
    assert got.json()["license"] == "CC-BY-NC-4.0" and got.json()["style"] == "smoky jazz"

    # re-arrange: same score, new style — the songwriter is skipped
    re_ = client.post(f"/api/music/{src['id']}/rearrange", json={"style": "punk", "lyrics": "[Verse]\nnew words"})
    assert re_.status_code == 200, re_.text
    nj = studio.jobs[re_.json()["id"]]
    req = nj["request"]
    assert req["abc"] == src["abc"] and req["style"] == "punk" and req["skip_songwriter"] is True
    assert req["engine"] == "yue2" and req["rearranged_from"] == src["id"] and req["duration_seconds"] == 30
    calls_before = len(shim.calls)
    studio.run_music(nj)
    assert nj["status"] == "done", nj.get("message")
    _, body = shim.calls[-1]
    assert body["abc"] == src["abc"].strip() and body["style"] == "punk"
    assert body["lyrics"] == "[Verse]\nnew words"
    assert len(shim.calls) == calls_before + 1

    # an edited score wins over the stored one
    edited = "X:1\nT:Edited\nK:D\nD E F G|\n"
    re2 = client.post(f"/api/music/{src['id']}/rearrange", json={"abc": edited})
    assert re2.status_code == 200 and studio.jobs[re2.json()["id"]]["request"]["abc"] == edited.strip()
    # a song with no score cannot be re-arranged, but it can be covered
    up = studio.MEDIA / "uploaded99.mp3"
    _sine(up, seconds=4, codec="mp3")
    assert client.post("/api/music/uploaded99/rearrange", json={}).status_code == 400
    assert client.post("/api/music/nosuchsong/cover", json={}).status_code == 404
    cv = client.post("/api/music/uploaded99/cover", json={"style": "bossa nova"})
    assert cv.status_code == 200, cv.text
    creq = studio.jobs[cv.json()["id"]]["request"]
    assert creq["reference_song_id"] == "uploaded99" and creq["cot"] == "melody"
    assert creq["engine"] == "yue2" and creq["style"] == "bossa nova" and 20 <= creq["duration_seconds"] <= 180


def test_stems_route_and_runner(studio, shim, tmp_path, monkeypatch):
    client = _client(studio)
    src = _submit(studio, style="smoky jazz")
    studio.run_music(src)
    assert src["status"] == "done"
    monkeypatch.setattr(studio, "_melband_cli", lambda: None)
    assert client.post(f"/api/music/{src['id']}/stems").status_code == 503
    assert client.post("/api/music/nosuchsong/stems").status_code == 404

    # a stand-in separator: copies the input to <stem>_vocals / _instrumental
    fake = tmp_path / "melband-roformer-infer"
    fake.write_text("#!/bin/sh\n"
                    "while [ $# -gt 0 ]; do case \"$1\" in --input_folder) IN=$2; shift;; "
                    "--store_dir) OUT=$2; shift;; esac; shift; done\n"
                    "mkdir -p \"$OUT\"; for f in \"$IN\"/*.wav; do b=$(basename \"$f\" .wav); "
                    "cp \"$f\" \"$OUT/${b}_vocals.wav\"; cp \"$f\" \"$OUT/${b}_instrumental.wav\"; done\n")
    fake.chmod(0o755)
    monkeypatch.setattr(studio, "_melband_cli", lambda: (fake, tmp_path / "models", "melband-roformer-kim-vocals"))
    r = client.post(f"/api/music/{src['id']}/stems")
    assert r.status_code == 200, r.text
    sj = studio.jobs[r.json()["id"]]
    assert sj["kind"] == "stems" and sj["request"]["song_id"] == src["id"]
    studio.run_stems(sj)
    assert sj["status"] == "done", sj.get("message")
    assert set(sj["stems"]) == {"vocals", "instrumental"}
    gallery = json.loads((studio.ROOT / "gallery.json").read_text())
    for stem, sid in sj["stems"].items():
        row = next(x for x in gallery if x["id"] == sid)
        assert row["kind"] == "music" and row["stem"] == stem and row["stem_of"] == src["id"]
        assert row["engine"] == "yue2" and row["license"] == "CC-BY-NC-4.0"
        assert (studio.MEDIA / f"{sid}.mp3").stat().st_size > 0
        # Cut sees a stem as an ordinary song: the studio resolver hands it over
        # as a music item (Cut's own asset record is covered by test_cut_*).
        item = studio._cut_gallery_item(sid)
        assert item and item["kind"] == "music" and item["duration_seconds"] > 0


# ----------------------------------------------------------------- UI contract

def test_music_ui_carries_engine_chip_licence_badge_and_tools():
    ui = (REPO / "static/index.html").read_text(encoding="utf-8")
    assert 'id="m_engine"' in ui and 'data-v="yue2"' in ui and 'data-v="music3"' in ui
    assert '<div class="chip sel" data-v="yue2">' in ui          # YuE2 is the default chip
    assert ui.count(NOTICE) >= 3                                # title, aria-label, JS constant
    assert "function ncPop" in ui and "const NC_NOTICE=" in ui
    assert 'id="m_style"' in ui and 'id="m_inst"' in ui
    for tool in ("musicEditScore", "musicRearrange", "musicCover", "musicStems"):
        assert f"function {tool}" in ui or f"async function {tool}" in ui
    for label in ("✏️ Edit score", "↺ Re-arrange", "🎤 Cover", "🎚 Stems"):
        assert label in ui
    assert "engine:sel('#m_engine')||'yue2'" in ui
    assert "s.engine==='yue2'||s.license?ncBadge():''" in ui
    assert "Warming up Music 3…" in ui                          # screenshot songs stay on Music 3


def test_music_docs_exist():
    doc = (REPO / "docs/MUSIC.md").read_text(encoding="utf-8")
    assert NOTICE in doc and "YUE2_KIT" in doc and "/api/music/{id}/stems" in doc
    assert "MUSIC.md" in (REPO / "docs/INSTALL.md").read_text(encoding="utf-8")


# --------------------------------------------------------------- Spark only

@pytest.mark.spark  # needs a live YuE2 engine on the studio host (YUE2_PORT)
def test_live_yue2_shim_health():
    """Needs the real engine on the studio host (YUE2_PORT)."""
    import urllib.request
    from media_lab_core import local_config
    with urllib.request.urlopen(f"http://127.0.0.1:{local_config.yue2_port()}/health", timeout=5) as r:
        health = json.loads(r.read())
    assert health["impl"] == "yue2" and health["model"] == "YuE2-3B"
