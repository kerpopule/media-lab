"""Director produce on Real / Long, and the Ref2VA prompt schema end to end:
identity shots film on Real / Long from their start frame plus the characters'
reference pictures, every still is made before the first take (one engine
load), and a Ref2VA-schema prompt is never wrapped in H3's three-field form."""
import base64
import json
from pathlib import Path

import app
from media_lab_core import director_cli
from runner import h3_singularity as sing

PNG = base64.b64decode("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGD4DwABBAEAwS2OUAAAAABJRU5ErkJggg==")

BIBLE = {
    "style": "35mm film", "location": "a 24-hour laundromat", "time_of_day": "2 a.m.",
    "characters": [
        {"name": "Maya", "look": "woman, dark curly hair", "wardrobe": "teal scrubs", "reference": "/media/maya.png"},
        {"name": "Theo", "look": "lanky man, round glasses", "wardrobe": "green parka", "reference": "/media/theo.png"},
    ],
}
BEATS = [
    {"shot_size": "WS", "characters": ["Maya", "Theo"], "screen_side": {"Maya": "left", "Theo": "right"},
     "video_prompt": "Theo searches a dryer while Maya folds towels.", "duration": 5},
    {"shot_size": "MCU", "characters": ["Maya"], "screen_side": {"Maya": "left"},
     "video_prompt": "Maya folds a towel.", "dialogue": [{"speaker": "Maya", "line": "It's always the left one."}],
     "duration": 5, "transition": "cut"},
]


class FakeStudio:
    def __init__(self):
        self.calls = []
        self.n = 0

    def run(self, path, body, *, label="", retries=2, log=None):
        self.n += 1
        self.calls.append((path, json.loads(json.dumps(body))))
        ext = ".png" if path == "/api/image" else ".mp4"
        return {"id": f"j{self.n}", "status": "done", "url": f"/media/j{self.n}{ext}"}

    def fetch(self, url, dest):
        Path(dest).write_bytes(PNG)
        return Path(dest)


def _produce(tmp_path, monkeypatch, **kw):
    monkeypatch.setattr(director_cli.stitch, "render",
                        lambda plan, out: {"sha256": "x", "probe": {"duration": 10.0}, "plan": {"notes": []}})
    monkeypatch.setattr(director_cli.seam_critic, "review",
                        lambda *a, **k: {"summary": "ok", "rerender": [], "seams": []})
    monkeypatch.setattr(director_cli.seam_critic, "markdown", lambda *a, **k: "")
    monkeypatch.setattr(director_cli.seam_critic, "syncnet_runner", lambda: None)
    studio = FakeStudio()
    journal = director_cli.produce({"bible": BIBLE, "beats": BEATS, "seed": 7}, tmp_path, studio,
                                   rounds=0, log=lambda m: None, **kw)
    return studio, journal


def test_real_long_films_identity_shots_with_references_after_every_still(tmp_path, monkeypatch):
    studio, journal = _produce(tmp_path, monkeypatch, real_long=True, takes_per_load=1)
    paths = [p for p, _ in studio.calls]
    first_take = paths.index("/api/generate")
    assert "/api/image" not in paths[first_take:], "a still after a take would evict the loaded engine"
    takes = [b for p, b in studio.calls if p == "/api/generate"]
    assert len(takes) == 2
    for take in takes:
        assert take["h3_engine"] == "singularity" and take["model"] == "h3"
        assert take["source"].startswith("/media/")          # the start frame -> opening frame
        assert take["reference_detail"] == "match"
        assert all(r["b64"] == base64.b64encode(PNG).decode() for r in take["references"])
        assert "The first frame is the given image" not in take["prompt"]
    assert [r["role"] for r in takes[0]["references"]] == ["person called Maya", "person called Theo"]
    assert [r["role"] for r in takes[1]["references"]] == ["person called Maya"]
    assert journal["routes"]["2"]["variant"] == "real-long"
    assert all(s[0]["engine"] == "real-long" for s in journal["shots"].values())
    assert not list(tmp_path.glob(".ref-*")), "reference temp files are cleaned up"


def test_without_real_long_sol_batches_and_no_references(tmp_path, monkeypatch):
    studio, journal = _produce(tmp_path, monkeypatch, takes_per_load=1)
    takes = [b for p, b in studio.calls if p == "/api/generate"]
    assert all("h3_engine" not in t and "references" not in t for t in takes)
    assert all(t["prompt"].startswith("integrated_multimodal_description: [Shot 1] The first frame") for t in takes)
    # takes_per_load=1: still, take, still, take
    kinds = [p for p, _ in studio.calls]
    assert kinds.index("/api/generate") < len(kinds) - 1 - kinds[::-1].index("/api/image")


def test_ref2va_schema_prompt_passes_through_the_studio_wrapper():
    vlog = ("r34l1sm\n\nsubject_definitions:\n<Subject 1> is the young woman from <Picture 1>.\n\n"
            "detailed_description:\n[Shot 1] She talks to the lens.\n\noverall_soundscape:\nroom tone")
    assert app.h3_prompt(vlog) == vlog
    assert app.h3_prompt("subject_definitions: <Subject 1> is the man in <Picture 1>.").startswith("subject_definitions")
    plain = app.h3_prompt("A summary: she waves.")
    assert plain.startswith("integrated_multimodal_description: [Shot 1] ")


def test_studio_three_field_prompt_is_not_nested_under_detailed_description():
    wrapped = app.h3_prompt('Maya says "hi" in a laundromat.')
    out = sing.compose_prompt(wrapped, ["person called Maya", "opening frame"], start_frame_picture=2)
    assert "detailed_description: [Shot 1] " in out
    assert "integrated_multimodal_description" not in out
    assert "<Subject 1> is the person called Maya in <Picture 1>." in out
    assert out.startswith(sing.TRIGGER)
