"""Real-codec pair and application-hook regressions; no live services or GPU."""

import ast
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from typing import Any
from unittest.mock import patch

import numpy as np

from media_lab_core import dual_delivery, hdr_delivery, hdr_finish, sdr_delivery


class DualDeliveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="delivery-fixture-")
        self.root = Path(self.temp.name)
        self.source = self.root / "original ' ; safe.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-f",
                "lavfi",
                "-i",
                "testsrc2=s=128x72:r=24:d=0.5",
                "-f",
                "lavfi",
                "-i",
                "sine=frequency=440:duration=0.5",
                "-c:v",
                "libx264",
                "-pix_fmt",
                "yuv420p",
                "-c:a",
                "aac",
                "-color_primaries",
                "bt709",
                "-color_trc",
                "bt709",
                "-colorspace",
                "bt709",
                "-color_range",
                "tv",
                str(self.source),
            ],
            check=True,
            capture_output=True,
        )

    def tearDown(self):
        self.temp.cleanup()

    def pair(self, **kwargs):
        return hdr_delivery.prepare(
            self.source,
            self.root / "hdr.mp4",
            self.root / "job",
            sdr_target=self.root / "sdr.mp4",
            **kwargs,
        )

    def audio(self, path):
        return subprocess.run(
            ["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a:0", "-f", "framemd5", "-"],
            check=True,
            capture_output=True,
        ).stdout

    def hooks(self, *, gallery_failure=False):
        text = (Path(__file__).parents[1] / "app.py").read_text()
        functions: list[ast.stmt] = [
            n
            for n in ast.parse(text).body
            if isinstance(n, ast.FunctionDef) and n.name in ("gallery_add", "_finish_video")
        ]
        saved = []
        job = {"id": "fixture", "prompt": "Synthetic codec fixture"}

        def failed(j, reason, detail=""):
            j.update(status="failed", failure=reason, detail=detail)

        def save(*args):
            if gallery_failure:
                raise OSError("injected gallery storage failure")
            saved.append(args[-1])

        scope: dict[str, Any] = {
            "Path": Path,
            "os": os,
            "MEDIA": self.root,
            "JOBS_DIR": self.root / "jobs",
            "ROOT": self.root,
            "jobs": {"fixture": job},
            "time": time,
            "media_path": lambda url: self.root / Path(url).name if url else None,
            "_load": lambda *a: [],
            "_save": save,
            "save_state": lambda: None,
            "_maybe_face_fix": lambda *a: None,
            "fail": failed,
        }
        exec(  # noqa: S102 - exercise the repository's exact hooks
            compile(ast.Module(body=functions, type_ignores=[]), "<exact-app-hooks>", "exec"), scope
        )
        return scope, job, saved

    def test_pair_same_source_audio_and_geometry(self):
        before = sdr_delivery.digest(self.source)
        r = self.pair()
        self.assertEqual(sdr_delivery.digest(self.source), before)
        self.assertEqual(sdr_delivery.digest(self.root / "job/engine-original.mp4"), before)
        self.assertEqual(r["source_sha256"], r["sdr_delivery"]["source_sha256"])
        self.assertEqual(r["codec_vui"]["transfer_characteristics"], [18])
        self.assertEqual(r["sdr_delivery"]["codec_vui"]["transfer_characteristics"], [1])
        self.assertEqual(self.audio(self.source), self.audio(self.root / "sdr.mp4"))
        self.assertEqual(r["sdr_delivery"]["probe"]["streams"][0]["nb_frames"], "12")

    def test_grade_neutrals_endpoints_primaries_gamut(self):
        ramp = np.repeat(np.linspace(0, 1, 1025)[:, None], 3, axis=1)
        result = sdr_delivery.grade(ramp)
        self.assertTrue(np.isfinite(result).all())
        self.assertGreaterEqual(np.diff(result, axis=0).min(), 0)
        self.assertLess(np.ptp(result, axis=1).max(), 1e-8)
        np.testing.assert_allclose(sdr_delivery.grade(np.eye(3)), np.eye(3), atol=1e-8)
        random = sdr_delivery.grade(np.random.default_rng(42).random((10000, 3)))
        self.assertGreaterEqual(random.min(), 0)
        self.assertLessEqual(random.max(), 1)

    def test_hdr_encoder_failure_removes_both_outputs_keeps_original(self):
        with (
            patch.object(hdr_delivery, "encode", side_effect=RuntimeError("HDR encoder failed")),
            self.assertRaisesRegex(RuntimeError, "HDR encoder failed"),
        ):
            self.pair()
        self.assertFalse((self.root / "hdr.mp4").exists())
        self.assertFalse((self.root / "sdr.mp4").exists())
        self.assertTrue((self.root / "job/engine-original.mp4").exists())

    def test_sdr_failure_never_runs_hdr(self):
        with (
            patch.object(sdr_delivery, "encode", side_effect=RuntimeError("SDR failed")),
            patch.object(hdr_delivery, "encode") as hdr,
        ):
            with self.assertRaisesRegex(RuntimeError, "SDR failed"):
                self.pair()
            hdr.assert_not_called()

    def test_existing_files_never_deleted_even_on_hdr_failure(self):
        (self.root / "hdr.mp4").write_bytes(b"foreign")
        with self.assertRaisesRegex(ValueError, "overwrite"):
            self.pair()
        self.assertEqual((self.root / "hdr.mp4").read_bytes(), b"foreign")
        self.assertFalse((self.root / "sdr.mp4").exists())

    def test_cancelled_pair_keeps_original_no_deliveries(self):
        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            self.pair(cancelled=lambda: True)
        self.assertFalse((self.root / "hdr.mp4").exists())
        self.assertFalse((self.root / "sdr.mp4").exists())

    def test_owned_process_is_cancelled_and_reaped(self):
        start = time.monotonic()
        with self.assertRaisesRegex(RuntimeError, "cancelled"):
            sdr_delivery.run(
                [sys.executable, "-c", "import time;time.sleep(30)"],
                cancelled=lambda: time.monotonic() - start > 0.15,
            )
        self.assertLess(time.monotonic() - start, 3)

    def test_owned_process_timeout(self):
        with self.assertRaises(TimeoutError):
            sdr_delivery.run([sys.executable, "-c", "import time;time.sleep(30)"], timeout=0.1)

    def test_source_metadata_refused(self):
        for field, value in [
            ("color_transfer", "smpte2084"),
            ("color_primaries", "smpte432"),
            ("color_range", "pc"),
        ]:
            metadata = hdr_finish.probe(self.source)
            metadata["streams"][0][field] = value
            with (
                patch.object(hdr_finish, "probe", return_value=metadata),
                self.assertRaises(ValueError),
            ):
                sdr_delivery.encode(self.source, self.root / "sdr.mp4", assume_sdr709=True)
            self.assertFalse((self.root / "sdr.mp4").exists())

    def test_silent_video_supported(self):
        silent = self.root / "silent.mp4"
        subprocess.run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-i",
                str(self.source),
                "-an",
                "-c:v",
                "copy",
                "-color_primaries",
                "bt709",
                "-color_trc",
                "bt709",
                "-colorspace",
                "bt709",
                "-color_range",
                "tv",
                str(silent),
            ],
            check=True,
            capture_output=True,
        )
        r = sdr_delivery.encode(silent, self.root / "sdr.mp4")
        self.assertEqual(len(r["probe"]["streams"]), 1)

    def test_native_hlg_tone_map_without_hdr_reencode(self):
        hdr = self.root / "native.mp4"
        hdr_delivery.encode(self.source, hdr, assume_sdr709=True)
        before = sdr_delivery.digest(hdr)
        r = dual_delivery.enforce(hdr, self.root / "sdr.mp4", self.root / "job")
        self.assertEqual(sdr_delivery.digest(hdr), before)
        self.assertEqual(sdr_delivery.digest(self.root / "job/retained/engine-original.mp4"), before)
        self.assertIn("tone map", r["sdr_delivery"]["source_transform"])

    def test_stale_sibling_rejected_without_modifying_either(self):
        sibling = self.root / "sdr.mp4"
        sibling.write_bytes(b"foreign")
        before = self.source.read_bytes()
        with self.assertRaisesRegex(ValueError, "transaction receipt"):
            dual_delivery.enforce(self.source, sibling, self.root / "job")
        self.assertEqual(self.source.read_bytes(), before)
        self.assertEqual(sibling.read_bytes(), b"foreign")

    def test_actual_finalizer_and_gallery_publish_both_after_checks(self):
        scope, job, saved = self.hooks()
        scope["_finish_video"](job, self.source)
        self.assertEqual(job["status"], "done", job)
        self.assertEqual(len(saved), 1)
        self.assertEqual(
            saved[0][0]["variants"], {"hdr": "/media/fixture.mp4", "sdr": "/media/fixture.sdr.mp4"}
        )
        self.assertTrue((self.root / "fixture.jpg").is_file())
        sdr_delivery.verify(self.root / "fixture.sdr.mp4", source=self.source)
        hdr_finish.verify(self.root / "fixture.mp4", "x264")

    def test_library_failure_does_not_mark_job_done(self):
        scope, job, saved = self.hooks(gallery_failure=True)
        scope["_finish_video"](job, self.source)
        self.assertEqual(job["status"], "failed")
        self.assertNotIn("url", job)
        self.assertFalse((self.root / "fixture.mp4").exists())
        self.assertFalse((self.root / "fixture.sdr.mp4").exists())
        self.assertEqual(saved, [])

    def test_api_projections_include_variants_without_private_receipts(self):
        text = (Path(__file__).parents[1] / "app.py").read_text()
        functions: list[ast.stmt] = []
        for node in ast.parse(text).body:
            if isinstance(node, ast.FunctionDef) and node.name in ("brief", "job"):
                node.decorator_list = []
                functions.append(node)
        record = {
            "id": "fixture",
            "kind": "video",
            "status": "done",
            "variants": {"hdr": "/media/fixture.mp4", "sdr": "/media/fixture.sdr.mp4"},
            "hdr_delivery": {"private_internal_path": "not for the API"},
        }
        scope: dict[str, Any] = {
            "jobs": {"fixture": record},
            "queue": [],
            "online_queue": [],
            "brief_request": lambda r: r,
            "job_queue_lane": lambda j: "local",
        }
        exec(  # noqa: S102 - exact API projection regression
            compile(ast.Module(body=functions, type_ignores=[]), "<exact-api-projections>", "exec"),
            scope,
        )
        for result in (scope["brief"](record), scope["job"]("fixture")):
            self.assertEqual(result["variants"], record["variants"])
            self.assertNotIn("hdr_delivery", result)

    def test_legacy_gallery_path_gets_both_versions(self):
        scope, _job, saved = self.hooks()
        scope["gallery_add"](
            "legacy",
            "Synthetic old stitch",
            "boardfilm",
            "/media/" + self.source.name,
            "/media/legacy.jpg",
        )
        self.assertEqual(len(saved), 1)
        self.assertTrue(self.source.with_name(self.source.stem + ".sdr.mp4").exists())
        self.assertEqual(saved[0][0]["delivery_policy"], "hdr-and-vibrant-sdr-v1")


if __name__ == "__main__":
    unittest.main(verbosity=2)
