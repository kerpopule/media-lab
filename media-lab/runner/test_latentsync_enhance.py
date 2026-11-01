#!/usr/bin/env python3
"""Static regression gates for Media Lab's queued LatentSync 1.6 post-pass.

app.py is parsed rather than imported because importing it starts production
worker threads. The shell runner is inspected for lock, restore, offline, and
audio-delivery invariants.
"""
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class LatentSyncEnhanceContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = (ROOT / "app.py").read_text()
        start = cls.src.index("# ---------- post-render enhancement")
        end = cls.src.index("SHEET_SHOTS =", start)
        cls.runtime = cls.src[start:end]
        route = cls.src.index("class EnhanceReq(")
        route_end = cls.src.index('@app.get("/api/styles")', route)
        cls.route = cls.src[route:route_end]
        cls.runner = (ROOT / "runner/latentsync_video.sh").read_text()
        cls.locker = (ROOT / "runner/lock_instrumental_mouth.py").read_text()
        cls.avatar_runner = (ROOT / "runner/hunyuan_avatar_video.sh").read_text()
        cls.avatar_once = (ROOT / "runner/hunyuan_avatar_once.py").read_text()
        cls.avatar_dockerfile = (ROOT / "runner/Dockerfile.hunyuan-avatar").read_text()
        cls.avatar_wheel_lock = (ROOT / "runner/requirements-hunyuan-avatar.lock").read_text()
        cls.avatar_manifest = json.loads(
            (ROOT / "research/hunyuan-avatar/model-manifest.json").read_text()
        )

    def test_visible_queue_route_has_explicit_lipsync_contract(self):
        self.assertIn('submit_job("enhance", payload)', self.route)
        self.assertIn('requested == ["lipsync"]', self.route)
        self.assertIn('drive_audio_source: str = ""', self.route)
        self.assertIn('seed: int = 1247', self.route)
        self.assertIn('guidance_scale: float = 1.5', self.route)
        self.assertIn('mouth_lock_until: float = 0.0', self.route)

    def test_route_refuses_unknown_or_combined_operations(self):
        self.assertIn('any(o not in allowed for o in requested)', self.route)
        self.assertIn('requested not in (["lipsync"], ["avatar"])', self.route)
        self.assertIn('unsupported finishing operation', self.route)
        self.assertIn('single variable', self.route)

    def test_route_requires_readable_vocals_stem(self):
        self.assertIn('media_path(r.drive_audio_source)', self.route)
        self.assertIn('readable vocals-only drive stem', self.route)
        self.assertIn('payload["drive_audio_source"] = drive.name', self.route)

    def test_worker_fails_closed_and_never_substitutes_full_mix_as_drive(self):
        self.assertIn('ops == ["lipsync"]', self.runtime)
        self.assertIn('refusing to use the full mix', self.runtime)
        self.assertIn('_latentsync_file(src, drive, out', self.runtime)

    def test_pinned_local_runtime_and_model_files(self):
        self.assertIn('stage2_512.yaml', self.runtime)
        self.assertIn('latentsync_unet.pt', self.runtime)
        self.assertIn('whisper/tiny.pt', self.runtime)
        self.assertIn('LATENTSYNC16_START', self.runner)
        self.assertIn('--inference_steps 20 --guidance_scale "$GUIDANCE"', self.runner)
        self.assertIn('--seed "$SEED"', self.runner)

    def test_pinned_hunyuan_avatar_route_and_runtime(self):
        self.assertIn('requested in (["lipsync"], ["avatar"])', self.route)
        self.assertIn('hunyuan_avatar_ready()', self.route)
        self.assertIn('avatar_steps: int = 30', self.route)
        self.assertIn('avatar_frames: int = 129', self.route)
        self.assertIn('r.avatar_frames not in (17, 129)', self.route)
        self.assertIn('_hunyuan_avatar_file(src, drive, out', self.runtime)
        self.assertIn('media-lab-hunyuan-avatar:v1.9.0-r3', self.avatar_runner)
        self.assertIn('media-lab-hunyuan-avatar:v1.9.0-r3', self.avatar_once)
        self.assertIn('args.steps != 30', self.avatar_once)
        self.assertIn('hunyuan_video_avatar_720_quanto_bf16_int8.safetensors', self.avatar_once)
        self.assertIn('llava-llama-3-8b-v1_1_vlm_quanto_int8.safetensors', self.avatar_once)
        self.assertIn('sampling_steps=args.steps', self.avatar_once)
        self.assertIn('audio_guide=args.audio', self.avatar_once)
        self.assertIn('cfg_star_switch=False', self.avatar_once)
        self.assertIn('model.pipeline._interrupt = False', self.avatar_once)
        self.assertIn('model.pipeline.transformer.cache = None', self.avatar_once)
        self.assertIn('FRAMES=${8:-129}', self.avatar_runner)
        self.assertIn('"$FRAMES" == 17 || "$FRAMES" == 129', self.avatar_runner)
        self.assertIn('str(jd), str(frames)', self.runtime)

    def test_hunyuan_avatar_runner_owns_lock_restoration_and_audio_integrity(self):
        self.assertIn('LOCK=/run/user/1000/spark-gpu.lock', self.avatar_runner)
        self.assertIn('flock -n -x 9', self.avatar_runner)
        self.assertIn('UNIT_RESIDENTS=(media-lab-segment.service media-lab-image.service media-lab-comfy-image.service media-lab-comfy-music.service)', self.avatar_runner)
        self.assertIn('CONTAINER_RESIDENTS=("$LTX" "$H3")', self.avatar_runner)
        self.assertIn('docker stop --time 30 "$container"', self.avatar_runner)
        self.assertIn('systemctl --user stop "$unit"', self.avatar_runner)
        self.assertIn('"$POOL_LOCK" acquire', self.avatar_runner)
        self.assertLess(self.avatar_runner.index('docker stop --time 30 "$container"'),
                        self.avatar_runner.index('systemctl --user stop "$POOL"'))
        self.assertIn('unapproved GPU resident', self.avatar_runner)
        self.assertIn('docker top qwen38-vllm -eo pid', self.avatar_runner)
        self.assertIn('QWEN_PIDS["$qpid"]', self.avatar_runner)
        self.assertIn('HF_HUB_OFFLINE=1', self.avatar_runner)
        self.assertIn('TRANSFORMERS_OFFLINE=1', self.avatar_runner)
        self.assertIn('available_kb', self.avatar_runner)
        self.assertIn('--network none', self.avatar_runner)
        self.assertIn('--entrypoint python3 "$IMAGE_ID"', self.avatar_runner)
        self.assertIn('ffmpeg -nostdin -v error -i "$DST" -f null -', self.avatar_runner)
        self.assertIn('cmp "$SRC_AAC" "$DST_AAC"', self.avatar_runner)
        self.assertIn('OUT_WIDTH OUT_HEIGHT OUT_FPS OUT_FRAMES', self.avatar_runner)
        self.assertNotIn('-shortest', self.avatar_runner)

    def test_hunyuan_avatar_manifest_covers_complete_offline_runtime(self):
        runtime = self.avatar_manifest["runtime"]
        self.assertEqual(runtime["image"], "media-lab-hunyuan-avatar:v1.9.0-r3")
        self.assertEqual(runtime["image_id"],
                         "sha256:78961d13f4b4634f74eb28b9e078e85967e3f9456d9199a0b813d616b48e33c9")
        files = {item["path"] for item in self.avatar_manifest["weights"]["files"]}
        for required in (
            "hunyuan_video_custom_VAE_config.json",
            "llava-llama-3-8b/config.json",
            "llava-llama-3-8b/tokenizer.json",
            "clip_vit_large_patch14/text_config.json",
            "clip_vit_large_patch14/preprocessor_config.json",
            "whisper-tiny/config.json",
            "whisper-tiny/preprocessor_config.json",
            "hunyuan_video_720_quanto_int8_map.json",
        ):
            self.assertIn(required, files)
        self.assertGreaterEqual(len(files), 27)
        self.assertIn('hashlib.sha256()', self.avatar_runner)
        self.assertIn('FROM sha256:05bd1dc42dd2e0316a0291352f49966232f2d016c62ff9852a918eddaa690e16',
                      self.avatar_dockerfile)
        self.assertIn('--no-index --no-deps --require-hashes', self.avatar_dockerfile)
        self.assertEqual(self.avatar_wheel_lock.count('--hash=sha256:'), 12)

    def test_hunyuan_avatar_cancellation_and_restoration_fail_closed(self):
        self.assertIn('trap cleanup EXIT', self.avatar_runner)
        self.assertIn('trap on_int INT', self.avatar_runner)
        self.assertIn('trap on_term TERM', self.avatar_runner)
        self.assertIn('on_int() { exit 130; }', self.avatar_runner)
        self.assertIn('on_term() { exit 143; }', self.avatar_runner)
        self.assertIn('trap - EXIT INT TERM', self.avatar_runner)
        self.assertIn('GPU residents remain stopped', self.avatar_runner)
        self.assertNotIn('"$POOL_LOCK" acquire >/dev/null 2>&1 || true', self.avatar_runner)
        self.assertIn('rm -f "$DST"', self.avatar_runner)
        self.assertIn('restore_attempted=1', self.avatar_runner)
        self.assertIn('RUNNING_PROCS[jid] = None', self.runtime)
        self.assertIn('cancelled before Hunyuan Avatar launch', self.runtime)
        self.assertIn('out.unlink(missing_ok=True)', self.runtime)
        self.assertIn('--frames "$FRAMES" --width "$WIDTH" --height "$HEIGHT" &', self.avatar_runner)
        self.assertIn('wait "$docker_pid"', self.avatar_runner)

    def test_runner_uses_canonical_lock_and_restores_exact_pool(self):
        self.assertIn('LOCK=/run/user/1000/spark-gpu.lock', self.runner)
        self.assertIn('systemctl --user stop "$POOL"', self.runner)
        self.assertIn('"$POOL_LOCK" acquire', self.runner)
        self.assertIn('flock -n -x 9', self.runner)
        self.assertIn('could not acquire the canonical GPU lock after pool handoff', self.runner)
        self.assertIn('docker stop --time 30 "$LTX"', self.runner)
        self.assertIn('docker start "$LTX"', self.runner)
        self.assertIn('systemctl --user stop "$SEG"', self.runner)
        self.assertIn('systemctl --user start "$SEG"', self.runner)
        self.assertIn('docker stop --time 30 "$H3"', self.runner)
        self.assertIn('docker start "$H3"', self.runner)
        # The exact pool lease is released last so a slow container stop cannot
        # let a residency reconciler reacquire the lock and deadlock this job.
        self.assertLess(self.runner.index('docker stop --time 30 "$LTX"'),
                        self.runner.index('systemctl --user stop "$POOL"'))

    def test_runner_is_offline_and_preserves_the_full_source_timeline(self):
        self.assertIn('HF_HUB_OFFLINE=1 TRANSFORMERS_OFFLINE=1 HF_DATASETS_OFFLINE=1', self.runner)
        self.assertIn('PAD_DRIVE="$JOBDIR/latentsync-drive-padded.wav"', self.runner)
        self.assertIn('ffmpeg -nostdin -v error -y -i "$DRIVE" -af apad', self.runner)
        self.assertIn('--audio_path "$PAD_DRIVE"', self.runner)
        self.assertIn('-map 0:v:0 -map 1:a:0', self.runner)
        self.assertIn('-c:a copy', self.runner)
        self.assertIn('fps=$SRC_FPS,trim=duration=$SRC_VIDEO_DUR', self.runner)
        self.assertIn('[[ "$DST_FRAMES" == "$SRC_FRAMES" ]]', self.runner)
        self.assertIn('cmp "$SRC_AAC" "$DST_AAC"', self.runner)
        self.assertNotIn('-shortest', self.runner)

    def test_instrumental_lock_is_scoped_and_feathered(self):
        self.assertIn('mouth lock requires 1120x640', self.locker)
        self.assertIn('cv2.findTransformECC', self.locker)
        self.assertIn('cv2.GaussianBlur', self.locker)
        self.assertIn('MOUTH_LOCK_DONE', self.locker)
        self.assertIn('mouth_lock_until', self.route)
        self.assertIn('LOCK_FADE', self.runner)
        self.assertIn('instrumental mouth locking is disabled because it can corrupt microphone/chin occlusions', self.route)
        self.assertIn('Use a non-composited onset workflow instead', self.runtime)

    def test_runner_decodes_packaged_output_before_success(self):
        self.assertIn('ffmpeg -nostdin -v error -i "$DST" -f null -', self.runner)
        self.assertIn('LATENTSYNC16_DONE', self.runner)


if __name__ == "__main__":
    unittest.main()
