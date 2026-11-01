#!/usr/bin/env python3
"""Focused regression gates for the H3 Ref2VA fail-closed reference contract and
the Qwen full-quality-vs-preview graph recipe.

These tests import runner.h3_reference (a dependency-light module) and parse the
Qwen graph from app.py source WITHOUT importing app.py (importing app.py starts
production worker threads). They mutation-test each invariant so a vacuous
assertion cannot pass.
"""
import base64
import unittest
from pathlib import Path

from runner import h3_reference as hr

ROOT = Path(__file__).resolve().parents[1]

# A 1x1 transparent PNG, base64 — valid, decodable, matches the PNG magic prefix.
_TINY_PNG_B64 = (
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk"
    "+A8AAQUBAScY42YAAAAASUVORK5CYII="
)


def ref(b64=_TINY_PNG_B64, role="Subject 1"):
    return {"b64": b64, "role": role}


class NormalizeReferencesTest(unittest.TestCase):
    def test_keeps_valid_role_tagged_reference_verbatim(self):
        r = ref(role="Ava")
        out = hr.normalize_references([r])
        self.assertEqual(out, [r])          # role preserved byte-for-byte
        self.assertTrue(hr.has_usable_reference([r]))

    def test_drops_blank_b64(self):
        self.assertEqual(hr.normalize_references([ref(b64="", role="Ava")]), [])

    def test_drops_non_decodable_base64(self):
        garbage = ref(b64="!!not-base64!!", role="Ava")
        self.assertEqual(hr.normalize_references([garbage]), [])

    def test_drops_decodable_but_not_image_bytes(self):
        # "hello" base64-decodes fine but is NOT an image — must be refused so
        # it can never reach a silent route.
        not_image = ref(b64=base64.b64encode(b"this is text not pixels").decode())
        self.assertEqual(hr.normalize_references([not_image]), [])

    def test_drops_wav_riff_payload_passing_as_webp(self):
        # A real WAV is RIFF....WAVE: it starts with the same RIFF container a
        # WebP does. The old magic check (any RIFF) would let it through; the
        # fourcc check must reject it.
        wav = b"RIFF\x24\x00\x00\x00WAVEfmt \x10\x00\x00\x00\x01\x00\x01\x00\x44\xac\x00\x00\x88\x58\x01\x00\x02\x00\x10\x00data\x00\x00\x00\x00"
        self.assertEqual(len(wav) % 4, 0)
        self.assertEqual(hr.normalize_references([ref(b64=base64.b64encode(wav).decode())]), [])

    def test_accepts_real_webp_fourcc(self):
        webp = b"RIFF\x10\x00\x00\x00WEBPVP8 \x00\x00\x00\x00\x00\x00\x00\x00"
        self.assertEqual(len(webp) % 4, 0)
        self.assertEqual(hr.normalize_references([ref(b64=base64.b64encode(webp).decode())]),
                         [ref(b64=base64.b64encode(webp).decode())])

    def test_drops_non_canonical_base64(self):
        # strict b64 (validate=True) rejects a valid-enough-but-noncanonical
        # stream; the generic decoder would have accepted it.
        noncanon = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mNk+A8AAQUBAScY42YAAAAASUVORK5CYI"  # missing pad 'I'
        self.assertEqual(hr.normalize_references([ref(b64=noncanon)]), [])

    def test_drops_non_dict_items(self):
        self.assertEqual(hr.normalize_references([ref(), "junk", 42]), [ref()])

    def test_mixed_list_keeps_only_usable(self):
        out = hr.normalize_references([ref(role="Ava"), ref(b64="bad", role="product")])
        self.assertEqual([r["role"] for r in out], ["Ava"])


class VariantSelectionTest(unittest.TestCase):
    """validate_h3_reference_request() must fail closed, never silently
    degrade. Each of these is a case the task explicitly demands hard-fail."""

    def test_ref2va_with_reference_passes(self):
        self.assertIsNone(hr.validate_h3_reference_request(
            "h3", "ref2va", [ref(role="Ava")], "max"))

    def test_ref2va_resident_with_missing_refs_hard_fails(self):
        # THE orchestrator-flagged defect: actor-cloning resident but no actors.
        for missing in (None, [], [ref(b64="", role="Ava")], [ref(b64="bad")]):
            with self.subTest(missing=missing):
                err = hr.validate_h3_reference_request("h3", "ref2va", missing)
                self.assertIsNotNone(err)
                self.assertIn("no usable reference", err)

    def test_references_but_fl2va_resident_hard_fails(self):
        err = hr.validate_h3_reference_request("h3", "fl2va", [ref(role="Ava")])
        self.assertIsNotNone(err)
        self.assertIn("fl2va", err)
        self.assertIn("ref2va", err)

    def test_references_on_non_h3_engine_hard_fails(self):
        for engine in ("ltx25", "ltx", "wav"):
            with self.subTest(engine=engine):
                err = hr.validate_h3_reference_request(engine, "ref2va", [ref()])
                self.assertIsNotNone(err)
                self.assertIn("h3 ENGINE", err)

    def test_invalid_reference_detail_fails_first(self):
        err = hr.validate_h3_reference_request("h3", "ref2va", [], "squish")
        self.assertIsNotNone(err)
        self.assertIn("reference_detail", err)

    def test_plain_fl2va_no_reference_is_allowed(self):
        # The promoted creative default: fl2va with a start frame and no refs.
        self.assertIsNone(hr.validate_h3_reference_request("h3", "fl2va", None))

    def test_required_variant_preserves_fl2va_default_and_selects_ref2va(self):
        self.assertEqual(hr.required_variant(None), hr.H3_FL2VA_VARIANT)
        self.assertEqual(hr.required_variant([]), hr.H3_FL2VA_VARIANT)
        self.assertEqual(hr.required_variant([ref(role="Mia")]), hr.H3_REF2VA_VARIANT)


class AppIntegrationTest(unittest.TestCase):
    """The app must actually carry references through to the engine and fail
    closed on the resident checkpoint. Parsed from app.py source (no import —
    importing starts production worker threads). Each assertion points at the
    real wiring added for this card."""

    @classmethod
    def setUpClass(cls):
        cls.src = (ROOT / "app.py").read_text()
        # snapshot the exact blocks that must contain the wiring
        mj_start = cls.src.find("def make_video_job(")
        mj_end = cls.src.find("RESUBMIT", mj_start)
        cls.make_video_job_src = cls.src[mj_start:mj_end]
        rv_start = cls.src.find("def run_video(")
        rv_end = cls.src.find("def run_music(", rv_start)
        cls.run_video_src = cls.src[rv_start:rv_end]
        gs_start = cls.src.find("class GenReq(")
        gs_end = cls.src.find("class MusicReq(", gs_start)
        cls.genre_src = cls.src[gs_start:gs_end]
        say_start = cls.src.find("def run_say(")
        say_end = cls.src.find("RUNNERS =", say_start)
        cls.run_say_src = cls.src[say_start:say_end]
        sr_start = cls.src.find("class SayReq(")
        sr_end = cls.src.find("@app.get(\"/api/voices\")", sr_start)
        cls.say_req_src = cls.src[sr_start:sr_end]
        route_start = cls.src.find("def character_say(")
        route_end = cls.src.find("# ---------- song", route_start)
        cls.say_route_src = cls.src[route_start:route_end]
        variant_start = cls.src.find("def ensure_h3_variant(")
        variant_end = cls.src.find("VIDEO_ENGINE_NAMES =", variant_start)
        cls.variant_src = cls.src[variant_start:variant_end]
        cls.engine_src = (ROOT / "runner/engine_server.py").read_text()

    def test_genreq_carries_references_and_detail(self):
        # references must ride the typed request so the engine can see them.
        self.assertIn("references: list = []", self.genre_src)
        self.assertIn('reference_detail: str = "match"', self.genre_src)

    def test_make_video_job_validates_references(self):
        # the fl2va downgrade bug must be impossible from the job-creation gate.
        self.assertIn('_h3ref.normalize_references(references)', self.make_video_job_src)
        self.assertIn('Refusing a silent downgrade', self.make_video_job_src)
        self.assertIn('_h3ref.assert_ref_count_ok', self.make_video_job_src)

    def test_make_video_job_refuses_non_h3_with_references(self):
        # references requested on a non-H3 engine must hard-fail at creation,
        # never silently run without likeness.
        self.assertIn('engine != "h3"', self.make_video_job_src)
        self.assertIn("requires model 'h3'", self.make_video_job_src)

    def test_run_video_forwards_public_reference_fields(self):
        self.assertIn('body["references"] = [{k: v for k, v in ref.items()', self.run_video_src)
        self.assertIn('if not str(k).startswith("_")', self.run_video_src)
        self.assertIn('body["reference_detail"]', self.run_video_src)

    def test_run_video_conserves_public_roles_in_body(self):
        # Preserve every normalized public field while excluding private,
        # preparation-only payloads such as the full-body edit reference.
        self.assertIn('for ref in references', self.run_video_src)
        self.assertIn('if not str(k).startswith("_")', self.run_video_src)

    def test_run_video_fails_closed_on_fl2va_resident(self):
        # reference pictures must never reach a fl2va-resident engine.
        self.assertIn('h3_resident_variant()', self.run_video_src)
        self.assertIn('_h3ref.H3_REF2VA_VARIANT', self.run_video_src)
        self.assertIn('_h3ref.H3_FUSED_R1024_VARIANT', self.run_video_src)
        self.assertIn("not actor-capable", self.run_video_src)

    def test_cold_path_refuses_references(self):
        # the cold legacy path has no reference input — references must not
        # silently drop through to it (they would render without likeness).
        self.assertIn('if start_b64 or references or staged_video_refs:', self.run_video_src)

    def test_genreq_references_are_list_typed(self):
        self.assertIn("references: list = []", self.genre_src)

    def test_ref2va_undecodable_reference_hard_fails(self):
        err = hr.validate_h3_reference_request("h3", "ref2va", [ref(b64="zzzz")])
        self.assertIsNotNone(err)

    def test_say_route_and_runtime_transport_references(self):
        self.assertIn("references: list = []", self.say_req_src)
        self.assertIn('reference_detail: str = "match"', self.say_req_src)
        self.assertIn('"references": references', self.say_route_src)
        self.assertIn('body["references"] = references', self.run_say_src)
        self.assertIn('body["reference_detail"] = detail', self.run_say_src)

    def test_say_reference_only_never_injects_a_composition_frame(self):
        self.assertIn('reference_only: bool = False', self.say_req_src)
        self.assertIn('"reference_only": r.reference_only', self.say_route_src)
        self.assertIn('if r.reference_only:', self.say_route_src)
        self.assertIn('reference_only forbids a source/composition frame',
                      self.say_route_src)
        self.assertIn('reference_only = bool(r.get("reference_only"))',
                      self.run_say_src)
        self.assertIn('if reference_only:', self.run_say_src)
        self.assertIn('native H3 Ref2VA reference-only mode', self.run_say_src)
        self.assertIn('elif supplied and', self.run_say_src)
        self.assertIn('elif eng in VIDEO_ENGINE_NAMES:', self.run_say_src)
        self.assertLess(self.run_say_src.index('if reference_only:'),
                        self.run_say_src.index('elif supplied and'))
        self.assertLess(self.run_say_src.index('if reference_only:'),
                        self.run_say_src.index('elif eng in VIDEO_ENGINE_NAMES:'))

    def test_say_fused_r1024_is_explicit_and_requires_combined_inputs(self):
        self.assertIn('h3_fused_r1024: bool = False', self.say_req_src)
        self.assertIn('if r.h3_fused_r1024:', self.say_route_src)
        self.assertIn('h3_fused_r1024 requires an approved source', self.say_route_src)
        self.assertIn('h3_fused_r1024 requires at least one identity reference',
                      self.say_route_src)
        self.assertIn('"h3_fused_r1024": r.h3_fused_r1024', self.say_route_src)

    def test_say_exact_prompt_bypasses_generic_talking_head_wrapper(self):
        self.assertIn('exact_prompt: str = ""', self.say_req_src)
        self.assertIn('"exact_prompt": r.exact_prompt.strip()[:4000]',
                      self.say_route_src)
        self.assertIn('exact_prompt = str(r.get("exact_prompt") or "").strip()',
                      self.run_say_src)
        self.assertIn('if exact_prompt:', self.run_say_src)
        self.assertIn('prompt = exact_prompt', self.run_say_src)
        self.assertLess(self.run_say_src.index('if exact_prompt:'),
                        self.run_say_src.index('The person looks at the camera'))

    def test_say_can_pin_original_native_frame_contract(self):
        self.assertIn('frame_count: int = 0', self.say_req_src)
        self.assertIn('"frame_count": r.frame_count', self.say_route_src)
        self.assertIn('requested_frames = int(r.get("frame_count") or 0)',
                      self.run_say_src)
        self.assertIn('(requested_frames - 5) % 17 == 0', self.run_say_src)
        self.assertIn('(requested_frames - 1) % 8 == 0', self.run_say_src)
        self.assertIn('frames = requested_frames', self.run_say_src)

    def test_say_reference_transport_fails_closed(self):
        self.assertIn('eng != "h3"', self.say_route_src)
        self.assertIn('len(usable) != len(references)', self.say_route_src)
        self.assertIn('len(references) != len(requested_refs)', self.run_say_src)

    def test_ref2va_promotes_source_to_composition_reference(self):
        # Maestro's omni/reference branch ignores image_start. The engine must
        # promote the settled source into the reference manifest instead.
        self.assertIn("is_ref2va = (ENGINE == 'h3' and", self.engine_src)
        self.assertIn("H3_VARIANT in ('ref2va', 'fused_r1024')", self.engine_src)
        self.assertIn("'image_intent': 'composition'", self.engine_src)
        self.assertIn("approved source scene, wardrobe, framing, and cast layout",
                      self.engine_src)
        self.assertIn("SOURCE_COMPOSITION_REFERENCE", self.engine_src)

    def test_ref2va_identity_pictures_are_identity_only(self):
        self.assertIn("'image_intent': ref.get('image_intent') or 'identity'",
                      self.engine_src)

    def test_ref2va_audio_uses_native_drive_reference(self):
        # Ref2VA disables Maestro's FL2VA input_waveform source_audio_mode. The
        # wav must ride the native reference manifest with drive intent instead.
        self.assertIn("'type': 'audio', 'path': str(tmp_wav)", self.engine_src)
        self.assertIn("'audio_intent': 'drive'", self.engine_src)
        self.assertIn("AUDIO_DRIVE_REFERENCE", self.engine_src)
        self.assertIn("if ENGINE == 'h3' and not is_ref2va:", self.engine_src)

    def test_ref2va_prompts_remain_untagged_for_complete_relationship_map(self):
        # A pre-tagged Picture 1 makes Maestro skip relationships for all other
        # identity/audio references. Both video and talking routes suppress it.
        self.assertIn("start_image=bool(start_b64) and not ref2va_request",
                      self.run_video_src)
        self.assertIn("start_image=has_start and not requested_refs",
                      self.run_say_src)

    def test_pinned_audio_never_uses_bookkeeping_line_as_transcript(self):
        self.assertIn('audio_transcript: str = ""', self.src)
        self.assertIn('"audio_transcript": r.audio_transcript.strip()[:500]',
                      self.say_route_src)
        self.assertIn('if (r.get("audio_source") or r.get("drive_audio_source"))',
                      self.run_say_src)
        self.assertIn('line=prompt_line', self.run_say_src)

    def test_h3_can_drive_from_vocals_but_deliver_untouched_mix(self):
        self.assertIn('drive_audio_source: str = ""', self.say_req_src)
        self.assertIn('"drive_audio_source": r.drive_audio_source.strip()',
                      self.say_route_src)
        self.assertIn('drive_requested and r.get("engine") != "h3"', self.run_say_src)
        self.assertIn('refusing to fall back to the full mix', self.run_say_src)
        self.assertIn('drive_padded = jd / "drive48.wav"', self.run_say_src)
        self.assertIn('base64.b64encode(drive_padded.read_bytes())', self.run_say_src)
        self.assertIn('["-i", str(padded)]', self.run_say_src)
        self.assertIn('abs(delivery_dur - drive_dur) > 0.050', self.run_say_src)

    def test_supplied_audio_does_not_require_or_fallback_to_a_cloned_voice(self):
        self.assertIn('audio_requested = bool(str(r.get("audio_source")',
                      self.run_say_src)
        self.assertIn('if audio_requested and not (audio_master and _nonempty(audio_master))',
                      self.run_say_src)
        self.assertIn('if not audio_requested:', self.run_say_src)
        self.assertIn('refusing to synthesize a replacement voice', self.run_say_src)
        self.assertLess(self.run_say_src.index('audio_requested = bool'),
                        self.run_say_src.index('if not audio_requested:'))

    def test_runtime_swap_is_locked_verified_and_rolls_back_exact_config(self):
        self.assertIn("media-lab-inference.lock", self.variant_src)
        self.assertIn("engine_busy(\"h3\")", self.variant_src)
        self.assertIn('variant=target["variant"]', self.variant_src)
        self.assertIn('turbo_preset=target["turbo_preset"]', self.variant_src)
        self.assertIn("h3_resident_config() == current", self.variant_src)
        self.assertIn('status="rolled-back" if restored else "failed"', self.variant_src)


class ReferenceDetailAndCountTest(unittest.TestCase):
    def test_resolve_accepts_match_and_max(self):
        self.assertEqual(hr.resolve_reference_detail("match"), "match")
        self.assertEqual(hr.resolve_reference_detail("max"), "max")
        self.assertEqual(hr.resolve_reference_detail(None), "match")

    def test_resolve_rejects_unknown(self):
        with self.assertRaises(ValueError):
            hr.resolve_reference_detail("supermax")

    def test_ref_count_cap_refuses_contact_sheet(self):
        with self.assertRaises(ValueError):
            hr.assert_ref_count_ok(hr.H3_REF_MAX + 1)
        hr.assert_ref_count_ok(hr.H3_REF_MAX)   # boundary is allowed


class QwenQualityVsPreviewTest(unittest.TestCase):
    """Parse the img_graph() builder out of app.py and mutate it. The task
    demands a full-quality 40-step non-Lightning mode AND an explicitly labeled
    4-step Lightning preview mode, with separate image1/image2 inputs."""

    @classmethod
    def setUpClass(cls):
        src = (ROOT / "app.py").read_text()
        start = src.find("def img_graph(")
        end = src.find("def kontext_ready", start)
        cls.img_graph_src = src[start:end]

    def test_module_is_imported_by_app(self):
        # app.py must share the reference contract through the _h3ref alias so a
        # preview/quality number can never drift from the tested constants.
        src = (ROOT / "app.py").read_text()
        self.assertIn("from runner import h3_reference as _h3ref", src)

    def test_quality_mode_bypasses_lightning_lora(self):
        # The quality branch wires the shared 40/4.0/1.0 constants into the
        # sampler, and must NOT load the Lightning LoRA (model feeds off the
        # AuraFlow-shifted UNet "ms" node, not a "lo" LoRA loader).
        self.assertIn("_h3ref.QWEN_QUALITY_STEPS", self.img_graph_src)
        self.assertIn("_h3ref.QWEN_QUALITY_CFG", self.img_graph_src)
        self.assertIn('"model": ["ms", 0]', self.img_graph_src)
        # Consts must hold the sample unit-testable full-quality numbers.
        self.assertEqual(hr.QWEN_QUALITY_STEPS, 40)
        self.assertEqual(hr.QWEN_QUALITY_CFG, 4.0)
        self.assertEqual(hr.QWEN_QUALITY_GUIDE, 1.0)

    def test_preview_mode_is_explicitly_lightning_4_step(self):
        # Preview is the ONLY graph path to reference the Lightning LoRA and the
        # 4/1.0 preview constants.
        self.assertIn("_h3ref.QWEN_PREVIEW_STEPS", self.img_graph_src)
        self.assertIn("_h3ref.QWEN_PREVIEW_CFG", self.img_graph_src)
        self.assertIn("_h3ref.QWEN_PREVIEW_LORA", self.img_graph_src)
        self.assertEqual(hr.QWEN_PREVIEW_STEPS, 4)
        self.assertEqual(hr.QWEN_PREVIEW_CFG, 1.0)

    def test_separate_image1_and_image2_inputs(self):
        self.assertIn('"image1"', self.img_graph_src)
        self.assertIn('"image2"', self.img_graph_src)

    def test_preview_and_quality_share_no_lora_conflict(self):
        # The full-quality graph must NOT wire the Lightning LoRA; only the
        # preview branch may reference the LoRA constant (count == 1).
        self.assertEqual(self.img_graph_src.count("_h3ref.QWEN_PREVIEW_LORA"), 1)

    # --- mutation: each regression must actually be caught ---
    def _mutate(self, old, new):
        self.assertIn(old, self.img_graph_src)
        return self.img_graph_src.replace(old, new)

    def test_detector_catches_quality_regression_to_preview_consts(self):
        mutated = self._mutate(
            "_h3ref.QWEN_QUALITY_STEPS", "_h3ref.QWEN_PREVIEW_STEPS")
        self.assertNotIn("_h3ref.QWEN_QUALITY_STEPS", mutated)


class H3TurboManagedPresetTest(unittest.TestCase):
    def test_turbo_is_opt_in(self):
        self.assertIsNone(hr.required_turbo_preset({}))
        self.assertIsNone(hr.required_turbo_preset({"h3_turbo": False}))

    def test_true_and_exact_name_resolve_to_pinned_preset(self):
        self.assertEqual(hr.required_turbo_preset({"h3_turbo": True}),
                         hr.H3_TURBO_PRESET)
        self.assertEqual(hr.required_turbo_preset(
            {"h3_turbo": hr.H3_TURBO_PRESET}), hr.H3_TURBO_PRESET)
        self.assertEqual(hr.required_turbo_preset(
            {"h3_turbo": "v4-8step"}), "v4-8step")

    def test_arbitrary_turbo_controls_fail_loudly(self):
        for value in ("custom.safetensors", "v4-4step", 0.5, {"strength": 2}):
            with self.subTest(value=value), self.assertRaises(ValueError):
                hr.required_turbo_preset({"h3_turbo": value})

    def test_managed_preset_is_exactly_six_steps_at_strength_one(self):
        self.assertEqual(hr.H3_TURBO_STEPS, 6)
        self.assertEqual(hr.H3_TURBO_STRENGTH, 1.0)
        self.assertEqual(hr.H3_TURBO_SIZE_BYTES, 779849816)
        self.assertEqual(len(hr.H3_TURBO_SHA256), 64)
        self.assertEqual(hr.turbo_steps("v4-6step"), 6)
        self.assertEqual(hr.turbo_steps("v4-8step"), 8)
        with self.assertRaises(ValueError):
            hr.turbo_steps("v4-7step")

    def test_runtime_config_combines_ref2va_and_turbo(self):
        cfg = hr.required_runtime_config({
            "references": [ref()], "h3_turbo": True,
        })
        self.assertEqual(cfg, {"variant": hr.H3_REF2VA_VARIANT,
                               "turbo_preset": hr.H3_TURBO_PRESET})

    def test_fused_r1024_runtime_is_explicit_and_requires_both_inputs(self):
        cfg = hr.required_runtime_config({
            "h3_fused_r1024": True,
            "source": "/media/approved-scene.png",
            "references": [ref()],
            "h3_turbo": "v4-8step",
        })
        self.assertEqual(cfg, {"variant": hr.H3_FUSED_R1024_VARIANT,
                               "turbo_preset": "v4-8step"})
        for request in (
                {"h3_fused_r1024": True, "references": [ref()]},
                {"h3_fused_r1024": True, "source": "/media/approved-scene.png"}):
            with self.subTest(request=request), self.assertRaises(ValueError):
                hr.required_runtime_config(request)
        self.assertIsNone(hr.validate_h3_reference_request(
            "h3", hr.H3_FUSED_R1024_VARIANT, [ref()]))
        self.assertRegex(hr.validate_h3_reference_request(
            "h3", hr.H3_FUSED_R1024_VARIANT, []) or "",
            "no usable reference picture")

    def test_engine_and_launcher_keep_convrot_safe_fail_closed_contract(self):
        engine = (ROOT / "runner" / "engine_server.py").read_text()
        launcher = (ROOT / "runner" / "start_h3_engine.sh").read_text()
        app = (ROOT / "app.py").read_text()
        for required in (
            "model.validate_loras", "offload.load_loras_into_model",
            "model.finalize_loras", "ConvRot-safe", "H3_TURBO_STEPS",
            "loras=['transformer'] if _turbo_loras else []",
        ):
            self.assertIn(required, engine)
        for required in (
            "H3_TURBO_SHA256", "H3_TURBO_SIZE_BYTES", "sha256sum -c",
            "H3_AFFINE_MAP_SHA256", "H3_AFFINE_ARCH=fl2va",
            "H3_AFFINE_ARCH=ref2va", "${H3_AFFINE_ARCH}_rank8.sft",
            "a42778e02ab2708dc70e23837ec4d3061b44f938c940decbc7a5b91f2c27c59e",
            "7179899e59fce9c36038cd6c0c57edaced0032c769c436cef234b07bf809381f",
        ):
            self.assertIn(required, launcher)
        self.assertIn('env["H3_TURBO_PRESET"]', app)
        self.assertIn('turbo_preset', app)

    def test_fused_combined_keyframe_gate_is_lab_only_and_fail_closed(self):
        engine = (ROOT / "runner" / "engine_server.py").read_text()
        launcher = (ROOT / "runner" / "start_h3_engine.sh").read_text()
        self.assertIn("H3_FUSED_COMBINED = H3_VARIANT == 'fused_r1024'", engine)
        self.assertIn("H3_VARIANT not in ('fl2va', 'ref2va', 'fused_r1024')", engine)
        self.assertIn("elif H3_FUSED_COMBINED:", engine)
        self.assertIn("FUSED_COMBINED_BOUNDARY_KEYFRAME", engine)
        self.assertIn("extra['input_video']", engine)
        self.assertIn("extra['prefix_frames_count'] = 1", engine)
        self.assertIn("refusing identity-only fallback", engine)
        self.assertNotIn("FUSED_COMBINED_START_KEYFRAME", engine)
        self.assertIn("if tmp_img and not H3_FUSED_COMBINED:", engine)
        self.assertIn("FUSED_SHA256", launcher)
        self.assertIn("FUSED_SIZE_BYTES", launcher)
        self.assertIn("fused_r1024", launcher)
        # The launcher may select the explicit variant, but no unscoped boolean
        # can accidentally turn combined conditioning on for official Ref2VA.
        self.assertNotIn("H3_FUSED_COMBINED", launcher)


class SerializationTest(unittest.TestCase):
    """Role fields survive through the app->engine manifest shape the engine
    shim already builds (minimax_h3_references entries {type,path,role})."""

    def test_manifest_roles_match_input_order(self):
        refs = hr.normalize_references([
            ref(role="Ava"), ref(role="Mia"),
            ref(role="product"), ref(role="style")])
        self.assertEqual([r["role"] for r in refs],
                         ["Ava", "Mia", "product", "style"])

    def test_engine_manifest_keeps_roles(self):
        # Mirror the loop engine_server.py runs: b64 -> temp file + role tag.
        import tempfile
        from pathlib import Path as P
        refs = hr.normalize_references([ref(b64=_TINY_PNG_B64, role="product")])
        manifest, files = [], []
        for i, r in enumerate(refs, 1):
            rp = P(tempfile.mkstemp(suffix=f"-ref{i}.png")[1])
            rp.write_bytes(base64.b64decode(r["b64"]))
            files.append(rp)
            manifest.append({"type": "image", "path": str(rp),
                             "role": r.get("role") or f"Subject {i}"})
        try:
            self.assertEqual([m["role"] for m in manifest], ["product"])
            self.assertTrue(all(len(m["path"]) for m in manifest))
        finally:
            for f in files:
                f.unlink(missing_ok=True)


if __name__ == "__main__":
    unittest.main(verbosity=2)