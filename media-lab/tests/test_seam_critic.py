"""The critic pass: measured seam checks, the vision look (faked), verdicts."""
import json
import shutil
import subprocess

import pytest

from media_lab_core import seam_critic, stitch

pytestmark = pytest.mark.skipif(not (shutil.which("ffmpeg") and shutil.which("ffprobe")),
                                reason="needs ffmpeg")


def _clip(path, src, seconds=2.0, freq=440, volume=1.0):
    subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-f", "lavfi", "-i", f"{src}{':' if '=' in src else '='}s=320x180:r=24:d={seconds}",
                    "-f", "lavfi", "-i", f"sine=frequency={freq}:sample_rate=48000:duration={seconds}",
                    "-filter_complex", f"[1:a]volume={volume}[a]", "-map", "0:v", "-map", "[a]",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path)], check=True,
                   capture_output=True)
    return path


@pytest.fixture
def cut(tmp_path):
    a = _clip(tmp_path / "a.mp4", "testsrc2")
    b = _clip(tmp_path / "b.mp4", "testsrc2", freq=660)          # same framing: a jump cut
    c = _clip(tmp_path / "c.mp4", "color=c=0x2255ff", freq=550)  # a colour jump inside the scene
    plan = {"shots": [{"path": str(a), "scene": "s"}, {"path": str(b), "scene": "s"},
                      {"path": str(c), "scene": "s"}],
            "width": 320, "height": 180, "color_match": False}
    receipt = stitch.render(plan, tmp_path / "cut.mp4")
    return tmp_path / "cut.mp4", receipt


def test_measured_half_flags_jump_cut_and_colour_jump(cut, tmp_path):
    path, receipt = cut
    report = seam_critic.review(path, receipt, frames_dir=tmp_path / "seams")
    s1, s2 = report["seams"]
    assert any("jump cut" in r for r in s1["reasons"])
    assert any(r.startswith("colour jump") for r in s2["reasons"])
    assert s1["verdict"] == "fix_edit" and s2["verdict"] == "fix_colour"
    assert (tmp_path / "seams" / "seam01-pair.jpg").is_file()
    assert report["vision_ran"] is False and report["pass"] is False
    assert "measured checks only" in report["summary"]
    json.dumps(report)


def test_vision_verdicts_become_a_rerender_list(cut, tmp_path):
    path, receipt = cut
    calls = []

    def chat(system, text, jpeg):
        calls.append(text)
        assert jpeg[:2] == b"\xff\xd8"
        if "seam 2" in text:
            return json.dumps({"people_consistent": False, "right_problems": ["looks into the camera"],
                               "verdict": "rerender_right", "reason": "the right frame stares at the lens"})
        return "```json\n" + json.dumps({"verdict": "ok", "reason": "fine"}) + "\n```"

    report = seam_critic.review(path, receipt, frames_dir=tmp_path / "seams", bible={"characters": []}, chat=chat)
    assert len(calls) == 2 and report["vision_ran"]
    assert report["rerender"] == [{"shot": 3, "reasons": ["looks into the camera"]}]
    assert "Nobody looks into the lens" in seam_critic.rerender_patch(["looks into the camera"])


def test_vision_failure_is_reported_not_passed(cut, tmp_path):
    path, receipt = cut

    def broken(system, text, jpeg):
        raise TimeoutError

    report = seam_critic.review(path, receipt, frames_dir=tmp_path / "seams", chat=broken)
    assert report["vision_ran"] is False and not report["pass"]


def test_vision_probe_rejects_a_text_only_engine():
    assert seam_critic.vision_probe(lambda s, t, j: "Red.")
    assert not seam_critic.vision_probe(lambda s, t, j: "I cannot describe the image as none was provided.")


def test_delta_e_is_zero_for_same_colour_and_large_for_opposites():
    assert seam_critic.delta_e([120, 80, 60], [120, 80, 60]) == 0
    assert seam_critic.delta_e([255, 0, 0], [0, 0, 255]) > 100


def test_produce_films_a_board_with_one_seed_and_edits_of_the_master(tmp_path):
    from media_lab_core import director_cli
    take = _clip(tmp_path / "take.mp4", "testsrc2", seconds=3.0)

    class FakeStudio:
        def __init__(self):
            self.calls, self.n = [], 0

        def submit(self, path, body):
            self.n += 1
            self.calls.append((path, body))
            return f"job{self.n}"

        def wait(self, job_id, log=None, **kw):
            path, _ = self.calls[int(job_id[3:]) - 1]
            ext = ".png" if path == "/api/image" else ".mp4"
            return {"status": "done", "url": f"/media/{job_id}{ext}"}

        def fetch(self, url, dest):
            dest.write_bytes(take.read_bytes() if url.endswith(".mp4") else b"png")
            return dest

        def run(self, path, body, **kw):
            j = self.wait(self.submit(path, body))
            j["id"] = f"job{self.n}"
            return j

    board = {"seed": 99, "bible": {"location": "a laundromat", "characters": [
                {"name": "Maya", "look": "woman, curly hair", "wardrobe": "teal scrubs", "reference": "/media/maya.png"},
                {"name": "Theo", "look": "man, red hair", "wardrobe": "green parka", "reference": "/media/theo.png"}]},
             "beats": [{"shot_size": "WS", "characters": ["Maya", "Theo"], "video_prompt": "Both wait.",
                        "screen_side": {"Maya": "left", "Theo": "right"}},
                       {"shot_size": "MCU", "characters": ["Theo"], "speaker": "Theo",
                        "screen_side": {"Theo": "right"}, "video_prompt": 'Theo: "Where do they go?"'}]}
    studio = FakeStudio()
    journal = director_cli.produce(board, tmp_path / "prod", studio, rounds=0, log=lambda m: None,
                                   studio_wraps_h3=True)
    images = [b for p, b in studio.calls if p == "/api/image"]
    takes = [b for p, b in studio.calls if p == "/api/generate"]
    assert "source" not in images[0]                                   # the master is painted fresh
    assert [b.get("reference_source") for b in images[1:3]] == ["/media/maya.png", "/media/theo.png"]
    assert images[3]["source"] == "/media/job3.png" and images[3]["reference_source"] == "/media/theo.png"
    assert all(t["seed"] == 99 and t["model"] == "h3" and t["source"] for t in takes)
    assert all("overall_soundscape" not in t["prompt"] for t in takes)   # the old studio wraps it itself
    assert (tmp_path / "prod" / journal["final_cut"]).is_file()
    assert (tmp_path / "prod" / "critic-r0.md").is_file()

    # one take per engine load: every take is preceded by an image job (which
    # makes the studio reload H3), so no engine process films two prompts
    studio = FakeStudio()
    director_cli.produce(board, tmp_path / "prod1", studio, rounds=0, log=lambda m: None, takes_per_load=1)
    paths = [p for p, _ in studio.calls]
    assert paths == ["/api/image"] * 3 + ["/api/generate", "/api/image", "/api/generate"]


def test_lipsync_off_becomes_a_rerender_and_unmeasured_is_said():
    plan = {"shots": [{"dialogue": True, "source_path": "/x/a.mp4"}, {"dialogue": False},
                      {"dialogue": True, "source_path": "/x/c.mp4"}]}
    fake = {"/x/a.mp4": {"face_track": True, "av_offset_frames": 0, "confidence": 7.1},
            "/x/c.mp4": {"face_track": True, "av_offset_frames": -5, "confidence": 1.9}}
    rows = seam_critic.lipsync(plan, lambda v: fake[v])
    assert [(r["shot"], r["verdict"]) for r in rows] == [(1, "in sync"), (3, "off")]
    report = seam_critic.verdicts({"seams": [], "film": {}}, None, plan, rows)
    assert report["rerender"][0]["shot"] == 3 and "lip-sync off by -5" in report["rerender"][0]["reasons"][0]
    assert [r["verdict"] for r in seam_critic.lipsync(plan, None)] == ["not measured", "not measured"]
    assert "Lip-sync" in seam_critic.markdown(report)


def test_syncnet_runner_needs_a_configured_checkout(monkeypatch):
    monkeypatch.delenv("MEDIA_LAB_SYNCNET_PYTHON", raising=False)
    assert seam_critic.syncnet_runner() is None


def test_studio_run_rides_out_a_transient_capacity_refusal(monkeypatch):
    from media_lab_core import director_cli
    answers = iter([{"status": "error", "message": "Something went wrong — the studio stopped this job safely.",
                     "detail": "h3/t2va requires 24.0 GiB including reserve; only 22.7 GiB available"},
                    {"status": "done", "url": "/media/ok.mp4"}])
    settled = []
    monkeypatch.setattr(director_cli, "settle_memory", lambda **kw: settled.append(1))
    studio = director_cli.Studio.__new__(director_cli.Studio)
    studio.submit = lambda path, body: "j"
    studio.wait = lambda jid, log=None: dict(next(answers))
    j = studio.run("/api/generate", {"prompt": "x"})
    assert j["status"] == "done" and settled == [1]
    studio.wait = lambda jid, log=None: {"status": "error", "message": "The prompt is empty"}
    assert studio.run("/api/generate", {"prompt": "x"})["status"] == "error" and settled == [1]
