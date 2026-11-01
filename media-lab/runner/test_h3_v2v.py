import ast
import unittest
from pathlib import Path

from runner import h3_reference as hr

ROOT = Path(__file__).resolve().parent.parent
APP = (ROOT / "app.py").read_text()
ENGINE = (ROOT / "runner/engine_server.py").read_text()
IMAGE_SERVICE = (ROOT / "image-svc/image_service.py").read_text()
UI = (ROOT / "static/index.html").read_text()


class VideoReferenceValidationTest(unittest.TestCase):
    def test_normalizes_bounded_media_reference(self):
        got = hr.normalize_video_references([
            {"source": "/media/take.mp4", "start_sec": 1.25,
             "include_audio": False, "role": "motion only"}
        ])
        self.assertEqual(got, [{"source": "/media/take.mp4", "start_sec": 1.25,
                                "include_audio": False, "role": "motion only"}])

    def test_rejects_remote_negative_nonfinite_or_loose_boolean_reference(self):
        self.assertEqual(hr.normalize_video_references([
            {"source": "https://example.com/take.mp4"},
            {"source": "/media/take.mp4", "start_sec": -1},
            {"source": "/media/take.mp4", "start_sec": "nan"},
            {"source": "/media/take.mp4", "include_audio": "false"},
            {"source": "/media/take.mp4", "include_audio": True},
            {"source": "/media/take.exe"},
        ]), [])

    def test_video_reference_selects_ref2va_without_turbo(self):
        cfg = hr.required_runtime_config({
            "video_references": [{"source": "/media/take.mp4"}],
            "h3_turbo": False,
        })
        self.assertEqual(cfg, {"variant": "ref2va", "turbo_preset": None})

    def test_video_reference_count_is_capped(self):
        with self.assertRaises(ValueError):
            hr.normalize_video_references([
                {"source": f"/media/take{i}.mp4"} for i in range(hr.H3_VIDEO_REF_MAX + 1)
            ])


class NativeV2VContractTest(unittest.TestCase):
    def test_app_prepares_before_h3_boot_and_never_cold_falls_back(self):
        start = APP.index("def run_video(j):")
        end = APP.index("def run_music(j):", start)
        src = APP[start:end]
        self.assertLess(src.index("_h3_v2v_stage(j)"), src.index("ensure_engine(eng, j)"))
        self.assertIn("_h3_v2v_prepare_first_frame", src)
        self.assertIn("if start_b64 or references or staged_video_refs", src)
        self.assertIn('body["video_references"] = staged_video_refs', src)

    def test_first_frame_pipeline_fails_closed(self):
        self.assertIn("SAM 3 could not confidently isolate", APP)
        self.assertIn("refusing an unsafe identity/clothing swap", APP)
        self.assertIn('"quality": True', APP)
        self.assertIn("never film an unverified fallback frame", APP)
        self.assertIn("effectively a no-op", APP)
        self.assertIn("diff_inside_selection", APP)
        self.assertIn("SFACE_COSINE_GATE = 0.363", APP)
        self.assertIn("not math.isfinite(identity_score)", APP)
        self.assertIn("did not verify as the selected identity", APP)

    def test_character_edit_uses_body_reference_but_h3_gets_no_private_payload(self):
        self.assertIn('role="fullbody"', APP)
        self.assertIn('identity_ref.get("_edit_b64") or identity_ref["b64"]', APP)
        self.assertIn('if not str(k).startswith("_")', APP)

    def test_engine_manifest_uses_only_staged_out_basename(self):
        self.assertIn("name = Path(str(ref.get('file') or '')).name", ENGINE)
        self.assertIn("vp = OUT / name", ENGINE)
        self.assertIn("'type': 'video', 'path': str(vp)", ENGINE)
        self.assertIn("'include_audio': False", ENGINE)

    def test_source_video_audio_is_prohibited_in_all_three_layers(self):
        self.assertIn("or include_audio", (ROOT / "runner/h3_reference.py").read_text())
        self.assertIn('"-an"', APP)
        self.assertNotIn('if ref.get("include_audio")', APP)
        self.assertIn("source-video audio is prohibited", ENGINE)
        self.assertIn("'include_audio': False", ENGINE)

    def test_image_editor_has_second_reference_and_full_quality_mode(self):
        self.assertIn("reference_image_path: Optional[str]", IMAGE_SERVICE)
        self.assertIn('g["pos"]["inputs"]["image2"]', IMAGE_SERVICE)
        self.assertIn('"model": ["ms", 0] if quality else ["lo", 0]', IMAGE_SERVICE)
        self.assertIn("def masked_diff_stats", IMAGE_SERVICE)
        self.assertIn('"diff_inside_selection": selected_stats', IMAGE_SERVICE)

    def test_quality_graph_really_bypasses_lightning_and_connects_image2(self):
        tree = ast.parse(IMAGE_SERVICE)
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef)
                  and n.name == "qwen_graph")
        ns = {}
        class Asset:
            name = "asset.bin"
        ns.update(QWEN_UNET=Asset(), QWEN_LORA=Asset(), QWEN_CLIP=Asset(),
                  QWEN_VAE=Asset())
        exec(compile(ast.Module(body=[fn], type_ignores=[]), "qwen_graph", "exec"), ns)
        graph = ns["qwen_graph"]("swap", "qa", 7, 40, 1.0,
                                  edit_name="source.png", mask_name="mask.png",
                                  reference_name="identity.png", quality=True)
        self.assertNotIn("lo", graph)
        self.assertEqual(graph["ks"]["inputs"]["model"], ["ms", 0])
        self.assertEqual(graph["ks"]["inputs"]["cfg"], 4.0)
        self.assertEqual(graph["pos"]["inputs"]["image2"], ["rs", 0])
        self.assertEqual(graph["ks"]["inputs"]["latent_image"], ["nm", 0])
        self.assertIn("40 if req.quality", IMAGE_SERVICE)

    def test_ui_is_h3_only_and_source_audio_is_off(self):
        self.assertIn("id=\"v2vBox\"", UI)
        self.assertIn("c.dataset.v==='h3'?'block':'none'", UI)
        self.assertIn("include_audio:false", UI)
        self.assertIn("Source audio is ignored by default", UI)
        self.assertIn("v2v_swap_first_frame:v2vSwap", UI)


if __name__ == "__main__":
    unittest.main()
