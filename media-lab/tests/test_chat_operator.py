import unittest
from pathlib import Path

from chat_operator import (
    StudioOperator,
    ToolError,
    action_authorized,
    parse_model_envelope,
    signed_session_authorized,
    tool_instructions,
)


class FakeStudio:
    def __init__(self):
        self.characters = [
            {"id": "ava-id", "name": "Ava", "sheet_url": "/media/char_ava.png"},
            {"id": "mia-id", "name": "Mia", "sheet_url": "/media/char_mia.png"},
        ]
        self.jobs = {
            "song-ok": {
                "id": "song-ok", "kind": "music", "status": "done",
                "request": {"vibe": "warm acoustic"}, "url": "/media/song-ok.mp3",
            },
            "video-old": {
                "id": "video-old", "kind": "video", "status": "done",
                "request": {
                    "prompt": "Ava looks at camera", "model": "ltx25",
                    "orientation": "landscape", "duration": "5", "style": "cinematic",
                    "cast": ["ava-id"], "source": "/media/anchor.png", "seed": 77,
                },
                "url": "/media/video-old.mp4",
            },
        }
        self.queue = []
        self.created = []
        self.media = {
            "/media/song-ok.mp3", "/media/anchor.png", "/media/char_ava.png",
            "/media/char_mia.png",
        }

    def create_job(self, kind, request):
        jid = f"new-{len(self.created) + 1}"
        job = {"id": jid, "kind": kind, "status": "queued", "stage": "queued",
               "request": request}
        self.created.append(job)
        self.jobs[jid] = job
        self.queue.append(jid)
        return job

    def media_path(self, ref):
        return Path("/studio/media") / Path(str(ref)).name if ref in self.media else None

    def operator(self):
        return StudioOperator(
            load_characters=lambda: self.characters,
            get_jobs=lambda: self.jobs,
            get_queue=lambda: self.queue,
            media_path=self.media_path,
            create_job=self.create_job,
            eta_estimate=lambda _job: 3,
            valid_styles={"none", "cinematic", "musicvideo"},
            valid_orientations={"landscape", "portrait", "square", "vertical"},
        )


class AuthorizationTests(unittest.TestCase):
    def test_signed_cookie_is_required_and_ip_or_host_is_never_authority(self):
        verifier = lambda raw: "user" if raw == "signed-cookie" else ""
        self.assertTrue(signed_session_authorized("signed-cookie", verifier))
        self.assertFalse(signed_session_authorized("", verifier))
        self.assertFalse(signed_session_authorized("127.0.0.1", verifier))

    def test_explicit_action_language_is_required_for_mutation(self):
        self.assertTrue(action_authorized("Queue a 12 second test with Ava."))
        self.assertTrue(action_authorized("Iterate the last take and change orientation."))
        self.assertFalse(action_authorized("Help me improve this prompt."))
        self.assertFalse(action_authorized("What can the studio do?"))


class EnvelopeTests(unittest.TestCase):
    def test_strict_model_envelope(self):
        env = parse_model_envelope('{"message":"Checking the cast.","tool_call":{"name":"list_characters","arguments":{}}}')
        self.assertEqual("list_characters", env["tool_call"]["name"])
        with self.assertRaises(ToolError):
            parse_model_envelope('```json\n{"message":"x","tool_call":null}\n```')
        with self.assertRaises(ToolError):
            parse_model_envelope('{"message":"x","tool_call":null,"surprise":true}')

    def test_real_capability_contract_includes_actions_not_old_refusal(self):
        text = tool_instructions()
        self.assertIn("queue_musicvideo", text)
        self.assertIn("inspect_job", text)
        self.assertNotIn("cannot queue", text.lower())


class OperatorToolTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeStudio()
        self.op = self.fake.operator()

    def test_lists_canonical_characters_songs_jobs_and_queue(self):
        chars = self.op.execute("list_characters", {}, action_ok=False)
        self.assertTrue(chars["accepted"])
        self.assertEqual(["Ava", "Mia"], [c["name"] for c in chars["result"]["characters"]])
        songs = self.op.execute("list_songs", {}, action_ok=False)
        self.assertEqual("song-ok", songs["result"]["songs"][0]["id"])
        recent = self.op.execute("list_recent_jobs", {"limit": 5}, action_ok=False)
        self.assertTrue(any(j["id"] == "video-old" for j in recent["result"]["jobs"]))
        queue = self.op.execute("queue_state", {}, action_ok=False)
        self.assertEqual([], queue["result"]["active"])

    def test_inspect_job_returns_real_request_and_result_state(self):
        rec = self.op.execute("inspect_job", {"job_id": "video-old"}, action_ok=False)
        self.assertEqual("done", rec["result"]["status"])
        self.assertEqual(77, rec["result"]["request"]["seed"])
        with self.assertRaisesRegex(ToolError, "unknown job"):
            self.op.execute("inspect_job", {"job_id": "missing"}, action_ok=False)

    def test_prompt_injection_and_unknown_tools_or_arguments_fail_closed(self):
        with self.assertRaisesRegex(ToolError, "not allowlisted"):
            self.op.execute("shell", {"command": "rm -rf /"}, action_ok=True)
        with self.assertRaisesRegex(ToolError, "unknown argument"):
            self.op.execute("queue_video", {
                "prompt": "safe", "model": "ltx25", "orientation": "landscape",
                "duration": "5", "cast": ["Ava"], "path": "../../etc/passwd",
            }, action_ok=True)

    def test_mutation_requires_explicit_user_action(self):
        with self.assertRaisesRegex(ToolError, "explicit"):
            self.op.execute("queue_video", {
                "prompt": "Ava waves", "model": "ltx25", "orientation": "landscape",
                "duration": "5", "cast": ["Ava"],
            }, action_ok=False)
        self.assertEqual([], self.fake.created)

    def test_missing_character_song_and_path_traversal_fail_loud(self):
        with self.assertRaisesRegex(ToolError, "unknown character"):
            self.op.execute("queue_video", {
                "prompt": "test", "model": "ltx25", "orientation": "landscape",
                "duration": "5", "cast": ["Not Ava"],
            }, action_ok=True)
        with self.assertRaisesRegex(ToolError, "unknown song"):
            self.op.execute("queue_musicvideo", {
                "song_id": "missing", "concept": "close-up performance", "engine": "ltx25",
                "orientation": "landscape", "length": "12", "cast": ["Ava"],
            }, action_ok=True)
        with self.assertRaisesRegex(ToolError, "studio media"):
            self.op.execute("queue_image", {
                "prompt": "anchor", "source": "../../etc/passwd", "orientation": "landscape",
                "engine": "auto", "cast": ["Ava"],
            }, action_ok=True)

    def test_identity_sheet_can_make_anchor_image_but_cannot_directly_drive_video(self):
        image = self.op.execute("queue_image", {
            "prompt": "Place Ava in a large-face neutral-expression performance close-up",
            "source": "/media/char_ava.png", "orientation": "landscape",
            "engine": "auto", "cast": ["Ava"], "seed": 88,
        }, action_ok=True)
        self.assertTrue(image["accepted"])
        with self.assertRaisesRegex(ToolError, "identity sheet"):
            self.op.execute("queue_video", {
                "prompt": "Ava sings", "source": "/media/char_ava.png",
                "model": "ltx25", "orientation": "landscape", "duration": "5",
                "cast": ["Ava"], "seed": 88,
            }, action_ok=True)

    def test_explicit_queue_action_returns_truthful_receipt(self):
        rec = self.op.execute("queue_video", {
            "prompt": "Ava holds a restrained expression in a large-face close-up",
            "source": "/media/anchor.png", "model": "ltx25",
            "orientation": "portrait", "duration": "5", "style": "cinematic",
            "cast": ["Ava"], "seed": 77,
        }, action_ok=True)
        self.assertEqual("new-1", rec["job_id"])
        self.assertEqual("queued", rec["status"])
        self.assertNotEqual("done", rec["status"])
        self.assertEqual("ltx25", rec["model"])
        self.assertEqual(["Ava"], rec["cast_names"])
        self.assertEqual("/api/jobs/new-1", rec["queue_url"])

    def test_musicvideo_is_bounded_to_qualification_and_has_explicit_engine_song_cast_seed(self):
        rec = self.op.execute("queue_musicvideo", {
            "song_id": "song-ok", "concept": "Large faces, restrained expression, warm stage light",
            "engine": "h3", "orientation": "portrait", "length": "12",
            "cast": ["Ava", "Mia"], "style": "musicvideo", "seed": 991,
        }, action_ok=True)
        req = self.fake.created[-1]["request"]
        self.assertEqual("song-ok", req["song_id"])
        self.assertEqual("h3", req["engine"])
        self.assertEqual("portrait", req["orientation"])
        self.assertEqual(["ava-id", "mia-id"], req["cast"])
        self.assertEqual(991, req["seed"])
        with self.assertRaisesRegex(ToolError, "12-second qualification"):
            self.op.execute("queue_musicvideo", {
                "song_id": "song-ok", "concept": "full", "engine": "ltx25",
                "orientation": "landscape", "length": "full", "cast": ["Ava"],
            }, action_ok=True)

    def test_no_silent_engine_or_orientation_fallback(self):
        with self.assertRaisesRegex(ToolError, "model"):
            self.op.execute("queue_video", {
                "prompt": "test", "model": "auto", "orientation": "landscape",
                "duration": "5", "cast": ["Ava"],
            }, action_ok=True)
        with self.assertRaisesRegex(ToolError, "orientation"):
            self.op.execute("queue_musicvideo", {
                "song_id": "song-ok", "concept": "test", "engine": "h3",
                "orientation": "auto", "length": "12", "cast": ["Ava"],
            }, action_ok=True)

    def test_iteration_changes_exactly_one_declared_variable(self):
        rec = self.op.execute("iterate_job", {
            "job_id": "video-old", "change": {"orientation": "portrait"},
        }, action_ok=True)
        req = self.fake.created[-1]["request"]
        old = self.fake.jobs["video-old"]["request"]
        changed = {k for k in set(old) | set(req) if old.get(k) != req.get(k)}
        self.assertEqual({"orientation"}, changed)
        self.assertEqual("orientation", rec["changed_field"])
        with self.assertRaisesRegex(ToolError, "exactly one"):
            self.op.execute("iterate_job", {
                "job_id": "video-old",
                "change": {"orientation": "portrait", "model": "h3"},
            }, action_ok=True)
        with self.assertRaisesRegex(ToolError, "must actually change"):
            self.op.execute("iterate_job", {
                "job_id": "video-old", "change": {"orientation": "landscape"},
            }, action_ok=True)


if __name__ == "__main__":
    unittest.main()
