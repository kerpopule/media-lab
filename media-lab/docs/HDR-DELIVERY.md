# Media Lab HDR delivery default

Owner request: every newly produced Media Lab video should use the reference's H.264 High 8-bit MP4, HLG transfer, BT.2020 primaries/nonconstant matrix, codec and container signaling, no Dolby Vision. Sharpness and real creative quality remain separate acceptance checks.

The shared `_finish_video` path now uses `media_lab_core.hdr_delivery.prepare`. Main native H3/Real-Long, LTX and hybrid takes use this path. It preserves `jobs/<id>/engine-original.mp4`, moves optional face enhancement before grading, applies an explicit managed SDR709-to-HLG2020 transform and uses CRF17 rather than the former CRF23 re-encode. Correct already-HLG source is not graded a second time. PQ, incompatible gamut/range and failed encoding are refused, not silently published as SDR. Cancellation is rechecked before publication.

This does NOT claim sensor-native HDR or recover lost detail. The SDR source assumption is recorded. Library MP4s get verified VUI and colr 9/18/9 signaling and a full decode. Physical HDR display behavior and platform retention need actual display/platform testing.

The candidate also gates produced MP4s at `gallery_add`, covering legacy music-video stitching, boardfilm, talking and enhancement paths without regrading raw imports or existing Library assets. Correctly signaled HLG AVC remains byte-identical; lost HDR input signaling is refused rather than guessed. JPEG posters get an explicit HLG-to-SDR viewing transform. This broader gate passes extracted-function and real-codec fixtures on Mac, the staged Spark package and the exact installed Spark source; it is deployed as a host-preserving two-function overlay. Deployment and installed-test receipts are retained under `docs/evidence/hdr-default/`. Each real renderer/export integration still needs its own exercise before claiming full end-to-end coverage. Standalone editor downloads outside this Library publication API are not established by this gate.

Focused disposable tests use real ffmpeg codec output, retained source hashes, audio stream presence, already-HLG handling, PQ/gamut refusal, cancellation, encoder failure/no fallback, and the exact extracted app finalizer through actual HLG encoding. Run the test file directly to avoid shared app bootstrap. Example:

`python3 -c 'import sys,runpy; sys.path.insert(0,"/absolute/repo/media-lab"); runpy.run_path("/absolute/repo/media-lab/tests/test_hdr_delivery_default.py",run_name="__main__")'`

Canonical monorepo is public. This task has no public-code publication approval, so commit locally but do not push. The private deployed overlay must preserve all other installed app functions and verify the preimage hash before writing. No restart is permitted during an active GPU lease or recovery hold.
