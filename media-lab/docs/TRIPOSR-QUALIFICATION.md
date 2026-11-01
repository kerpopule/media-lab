# TripoSR qualification candidate

Status: isolated Spark CPU inference and reusable worker entry point tested; not installable or advertised. No production changes made. The sections below record successive evidence and remaining qualification work.

The official [source](https://github.com/VAST-AI-Research/TripoSR) describes image-to-3D reconstruction and MIT coverage for code and pretrained models. The [model repository](https://huggingface.co/stabilityai/TripoSR) also declares MIT. These declarations do not complete dependency or hardware qualification.

## Immutable inputs inspected

- Source revision: `107cefdc244c39106fa830359024f6a2f1c78871`.
- Model revision: `5b521936b01fbe1890f6f9baed0254ab6351c04a`.
- `model.ckpt`: 1,677,246,742 bytes; upstream LFS SHA-256 `429e2c6b22a0923967459de24d67f05962b235f79cde6b032aa7ed2ffcd970ee`. This is metadata, not a locally verified download.
- Downloaded `config.yaml`: SHA-256 `74ca708ce086bf68e97709ea6b3d91f14717921c04691e84043f0eb8fcc68e62`.

## Required work before an install recipe

1. Review a bounded checkpoint conversion path. Upstream `tsr/system.py` calls `torch.load` without an explicit restricted loader; repository policy permits SafeTensors by default. Do not use this loader as-is or treat the extension as a security review. Any conversion must produce tensor-only evidence, a deterministic SafeTensors hash, and retain provenance.
2. Pin the complete dependency closure. Upstream requirements include an unpinned Git URL for torchmcubes and unpinned rembg, Hugging Face Hub, imageio and Gradio. A headless adapter should exclude the demo UI and use the independently qualified background-removal path rather than acquiring rembg models implicitly.
3. Resolve `facebook/dino-vitb16` in the image tokenizer: inspect whether only configuration or separate weights are needed, freeze every required file and license, and prove offline loading. Never leave a floating model ID in an installed runtime.
4. Qualify CPU and Spark separately. Upstream's approximate 6 GB VRAM guidance is not a measured memory floor for our adapter or evidence for GB10/SM121 compatibility. Compile and verify native dependencies in an isolated environment under the canonical resource owner.
5. Run deterministic image-to-mesh tracers with bounded runtime and memory, finite vertices, valid indices, nonempty geometry and self-contained GLB export. Verify output decoding and actual project preview/import. Only then register the exact tested variant.

## Intended user flow

Choose a Library image, select Make 3D asset, review the compatible local or paired-server destination, and submit an owned durable job. The resulting GLB belongs in the same Library and must copy into projects and portable shares. Keep source images and show measured limitations. No cloud fallback or silent model substitution.

## Resolved tokenizer dependency

Inspected the pinned `tsr/models/tokenizers/image.py`: it constructs ViTModel from a downloaded config, without a separate pretrained-weight fetch. The main state dictionary load supplies parameters (actual completeness still requires a load tracer). DINO config revision is `f205d5d8e640a89a2b8ef0369670dfc37cc07fc2`, 454 bytes, SHA-256 `b87c0270b97db085fd82cf114a761fd0f62ae7914fbd407c752a2260646b689c`. The adapter must replace this network configuration lookup with a verified local config.

`triposr-source-review.json` records 14 downloaded, hashed upstream source/license/requirements files and candidate revisions. It is deliberately outside the install catalog. It does not qualify the checkpoint, native dependency, runtime or platforms.

## Checkpoint static inspection

The actual 1,677,246,742-byte checkpoint was downloaded on Spark and matched the pinned hash. Static ZIP/pickle disassembly found 553 archive entries, 75,332 metadata bytes, 17,003 pickle operations and these GLOBAL references: collections.OrderedDict, torch.FloatStorage and torch._utils._rebuild_tensor_v2. No STACK_GLOBAL or extension opcodes were observed. No tensor loading or upstream code execution occurred. This is not a safe-loading approval.

The [PyTorch serialization guidance](https://docs.pytorch.org/docs/2.14/notes/serialization.html) says restricted loading reduces execution exposure but does not eliminate denial-of-service or memory-corruption risks. Next conversion must use an explicit restricted loader in a bounded isolated process, no added globals, validate plain tensor shapes/types, preserve provenance, and verify the resulting SafeTensors artifact. Do not infer the loader behavior solely from upstream's omitted weights_only argument; defaults depend on the installed PyTorch version.

## Restricted conversion result

Converted the pinned checkpoint on Spark with Torch 2.11.0+cpu using explicit weights_only=True, mmap, CPU mapping, and an empty added-globals list. The process ran with seccomp network denial (probe passed), MemoryMax=12G, MemorySwapMax=0, CPUQuota=400% and RuntimeMaxSec=300; terminal exit status was zero. No model architecture code was imported.

SafeTensors output: 1,677,170,936 bytes, SHA-256 `f72bb520b8b1a5639600ac818496f22d6ccb3b42d3942412bd1e2375ef780a2b`. Every plain dense float32 tensor was checked for finite values and compared exactly after decoding the converted artifact. This proves conversion preservation, not model quality, architecture compatibility, reproducibility across runtimes or full inference qualification. Two converter tests passed (roundtrip/existing-output preservation and nonfinite rejection).

## Offline source preparation

`tools/prepare-triposr-source.py` verifies every reviewed source hash and the pinned DINO config before creating a fresh output tree. It preserves upstream licensing and source files, records modified-file hashes, changes the tokenizer to use the local config, disables `from_pretrained` entirely, and replaces implicit rembg execution with an explicit error requesting a qualified cutout. No upstream module is imported during preparation.

Real source preparation was run twice with identical resulting file hashes. Existing-output and changed-config attempts were refused without publishing output. The prepared tree is copied to the isolated Spark review directory. It remains unqualified: torchmcubes, remaining runtime dependency locks, actual SafeTensors architecture loading, and bounded mesh tracers are still required. Texture baking imports remain separate from the initial vertex-color path; neither path is advertised yet.

## Spark CPU marching-cubes build

Built torchmcubes revision `3381600ddc3d2e4d74222f8495866be5fafbace4` from a verified 206,534-byte source archive (SHA-256 `a884dc17c4efafa575c4c167a00c61308429c56cc4d0572f04f03e520ce30ef5`). Its license is MPL-2.0; retain that license and corresponding-source obligations separately from VibeX code.

The isolated cp312/aarch64 environment uses the existing hash-pinned Torch 2.11.0+cpu base plus scikit-build-core 1.0.3, pybind11 3.1.0, CMake 4.4.3, Ninja 1.13.2 and their locked dependencies. Build explicitly sets CMAKE_CUDA_COMPILER=NOTFOUND, points CMake to the installed Torch and pybind11 packages, and uses two workers. A transient unit enforced MemoryMax=12G, no swap, CPUQuota=200%, RuntimeMaxSec=600 and seccomp network denial. The first attempt failed to locate pybind11; an explicit pybind11_DIR resolved it. Final exit status zero.

Wheel: `torchmcubes-0.1.0-cp312-cp312-linux_aarch64.whl`, SHA-256 `ae13b470b2c05450de71cca612967f305f5218da7b41667b6e4e2c617e696723`. Installed offline with required hash into the new review environment only. A 24-cubed synthetic scalar field produced 1,280 finite vertices and 2,492 triangles with valid indices on CPU. This is a native-dependency smoke test, not full model qualification or evidence for CUDA.

## First real offline image-to-mesh tracer

Resolved a 41-package inference lock. OmegaConf 2.3.1 requires ANTLR 4.9.3, whose PyPI release has no wheel; verified its source SHA-256 `f224469b4168294902bb1efa80a8bf7855f24c99aef99cbefc1bcd3cce77881b`, inspected its setuptools script, built a local wheel with networking denied, and included that wheel in the hash lock. Added trimesh 5.1.0 and imageio 2.37.4. Existing environments were not modified.

Strict SafeTensors architecture load succeeded with all 419,275,628 parameters and no extra network fetches. A real tracer used the previously qualified synthetic cup cutout, foreground ratio 0.85, gray compositing, seed 7, four CPU threads, deterministic algorithms, renderer chunks of 8192, and marching-cubes resolution 128. Seccomp network denial and cgroup 12GiB/no-swap/400%-CPU/600s limits were enforced. Transient unit completed with exit zero.

Inference/extraction/export took 21.15 seconds. The GLB decoded successfully with 10,198 finite vertices, 20,348 triangles and valid indices. Output 408,332 bytes, SHA-256 `8574558441116941ef105b51ae7e76587ca44c198c769555c136a29f83d0e391`. This is one synthetic-image test; repeated determinism, geometry fidelity/appearance, license inventory, production adapter integration, user-facing installation, and all other platforms remain unqualified.

## Repeatability and visual limitation

A fresh network-denied bounded process repeated the tracer in 21.31 seconds. Vertex/triangle counts and output size matched, but raw GLB hash differed (`dc6d81e2176bf6bc7006093d557b1534ed767950d777c85424f1a32851aeda48`). Comparison after lexicographically ordering vertex position/color records and remapping triangles with winding-preserving cyclic rotations showed exact equality of vertex positions, colors and oriented faces. The extraction/export ordering is therefore not yet byte-deterministic; add and test canonical mesh ordering before binding a qualification receipt to output hashes.

Four-view geometry inspection shows the synthetic flat cup illustration became a thin relief with a rear protrusion, not a volumetric cup. It is not evidence of acceptable general game-asset quality. Qualification needs a perspective object photograph and additional varied inputs, inspection of depth/silhouette/texture, and honest input guidance. Do not promote this model from the single technical tracer.

## Canonical export verification

Added `media_lab_core/mesh_order.py` to order vertex/color records, remap face indices and sort cyclically rotated triangles without reversing winding. It rejects invalid bounds, nonfinite coordinates and ambiguous duplicate vertex/color records; it does not weld vertices or repair geometry. Two tests cover permutation invariance, preserved orientation and invalid input rejection.

Applying the canonical ordering to both actual tracer meshes and exporting with the same pinned trimesh produced byte-identical GLBs, SHA-256 `6b096ebd5587b8daae9202128ddc6f1634994634f27d74e3a8eeb405fd291798`. This closes the observed ordering issue for this pair. It does not resolve the thin-relief quality limitation or qualify other inputs/runtimes. The final generation adapter still needs to invoke this ordering before publication.

## Perspective chair fixture

Used upstream examples/chair.png at the pinned TripoSR source revision: 114,705 bytes, SHA-256 `2503c12a74419d91a4c6c9f1affc48fee6e2b8b9091956ca6211e91ada57b5bf`. This is a demonstration fixture for evaluation, not bundled product media. The bounded network-denied Spark CPU tracer now invokes canonical ordering before GLB export. It completed with exit zero in 20.94 seconds, yielding 10,259 vertices and 20,448 triangles; GLB 410,488 bytes, SHA-256 `a7380f83524d536e2d4d8b7c154bcb3b8b73e3f43ec00970a7823623491984a3`. Decode, finite coordinates and index checks passed.

Four-view visual inspection shows a recognizable volumetric chair with seat and back depth. Rear legs are distorted and surface/color detail is uneven. This improves on the flat-illustration failure but supports only a draft reconstruction workflow with preview and user review. It does not establish finished game-ready geometry, broad input quality, physical accuracy, rigging or collision readiness.

## Browser Library import

The actual generated chair was served by an isolated Library fixture, paired through the current exported VibeX web interface, and imported into a disposable project. The portable verification marker crossed the browser boundary successfully. Open builder reached the chosen project; its Chat and Files views showed the saved GLB. Reopening the project after a full navigation reload retained the attachment. This proves web import/storage for this asset, not interactive model rendering, native-platform import or a live generation-to-Library job workflow. Full app regression: 392 tests across 46 files passed.


## Reusable CPU worker entry point

`media_lab_core.triposr_cpu` now accepts package, input and new output directory arguments. It pins the prepared source-receipt hash before trusting component hashes, verifies model/config bytes, refuses existing output directories, and accepts bounded single RGBA cutout PNGs with visible foreground and transparency. The exact CPU float32/seed-7/resolution-128 variant performs strict SafeTensors loading, canonical mesh ordering, self-contained GLB checks and independent geometry decode before writing a completion receipt. It does not install, register, claim a resource lease or enforce OS isolation itself; the calling controller must supply those boundaries. Interrupted or rejected work may leave a private incomplete directory without a receipt.

Ran this entry point on isolated Spark through a transient unit with network-denying seccomp, 12GiB memory limit, no swap, 400% CPU quota and 600-second timeout. Unit exited zero and is inactive/dead. Model load through export took 23.92 seconds. Output matched the prior chair hash exactly: `a7380f83524d536e2d4d8b7c154bcb3b8b73e3f43ec00970a7823623491984a3`, 410,488 bytes, 10,259 vertices and 20,448 triangles. Four local tests passed for component integrity/symlinks, untrusted source receipt refusal, existing-result preservation, and invalid cutouts without publication. This is still a candidate worker, not normal owned-job integration or complete runtime/license qualification.


## Stage 11w — controller verification for 3D results

Added media_lab_core.triposr_result.verify_triposr_result. It uses directory-relative bounded regular-file reads, refuses symlink/FIFO entries, checks the exact input hash, model/source hashes, variant, parameters, byte count and output hash, validates receipt bounds and self-contained GLB structure, then returns the exact checked bytes for publication. Geometry decode remains the isolated worker responsibility; receipt counts are not independently decoded here. Caller must await successful child exit and retain private directory ownership. No queue integration yet.

Four unittest methods passed, including nine changed-receipt subcases and two nonregular-file subcases. Verified the actual retained chair GLB against the Spark worker-v1 receipt: 410488 bytes and SHA-256 a7380f83524d536e2d4d8b7c154bcb3b8b73e3f43ec00970a7823623491984a3. Synthetic test container is deliberately structural, not a valid geometry tracer. No production changes.


## Stage 11x — shared owned CPU queue for 3D

Extracted existing background-removal claim/heartbeat/cancellation/input-integrity lifecycle into cpu_jobs.run_next_cpu. Background removal delegates with unchanged interface. JobStore.claim_cpu_job now accepts explicit image/model kind (default remains image) in parameterized SQL scope; legacy/unowned and other-kind/engine work is untouched. Added candidate triposr_jobs entry point requiring exact model hash, immutable variant and owned input identity, with explicitly supplied executor. No host registration or default unqualified execution.

Focused background/3D/result tests passed: 15 tests plus 11 subtests. Fake-executor queue tests prove model claim scope, canonical lock held during execution, cancellation wins, and changed input/variant refuses execution. Real inference through this queue is not yet tested; next step is a bounded 3D process executor with verified immutable artifact publication. Production unchanged.


## Stage 11y — real owned 3D queue execution on Spark

Added triposr_worker.run_triposr_job: canonical CPU lock held through bounded child execution, private staging, independent result verification and directory publication; cancellation before publication; existing results verified and reused without inference. Sanitized child environment selects separate model Python. CPU process errors now accept a task label; background-removal cancellation behavior retained after focused regression caught and corrected a label-scope error.

Local combined suite before label-only change: 38 tests and 13 subtests passed. Final CPU/3D executor regression after label correction: 11 tests and 2 subtests passed. Tests cover held lock, cancellation, failed verification without publication, and no execution on verified recovery.

Real Spark queue canary used new queue-v1 code/results/SQLite inside the isolated TripoSR review root. First transient unit failed before import/claim because model Python lacks psutil. Second used existing independent controller-cache Python (psutil 7.2.2), with separate inference Python; no environments modified. Unit vibex-triposr-queue-v2-20260904 exited zero, inactive/dead, under seccomp network denial, 12GiB/no-swap/400%-CPU/600s limits. Systemd reported 2.3G memory peak, zero swap. Owned job 2b9f3fa8fb0549e2b8a39e3f8b3b12d4 succeeded; model-load-through-export 24.83 seconds, artifact410488 bytes SHA a7380f83524d536e2d4d8b7c154bcb3b8b73e3f43ec00970a7823623491984a3. Second executor call returned recovered=true with same bytes/hash. Evidence triposr-review/queue-v1-proof.json and queue-v1-canary.py. Real run precedes the error-label-only change.

No public API/Library registration, production changes, or installer promotion. Canonical test lock is scoped to the isolated root, not production; CPU only. Next: owner-authenticated model job/result API and Library integration, followed by host/runtime/license qualification and user setup.


## Stage 11z — owned model downloads and history import action

Job content endpoint now checks model GLB bounds, self-contained structure and exact saved size/hash, returning the same checked buffer with model/gltf-binary, glb-v1 portability marker and no-store. Existing ownership checks remain. Nonimage preview refuses models. Client readModelResult verifies marker, MIME, size and SHA-256 before saving. GenerationHistory offers Use 3D model in this project for succeeded model jobs; library handler attaches model-<id>.glb through existing binary project storage. No 3D engine registration or creation UI yet.

Validation: full app 407 tests/47 files; TypeScript passed. Focused API/3D queue/result suite 9 tests and 11 subtests passed. API test verifies unauthenticated401, other owner404, exact content, model preview404, changed output409. Client fixture tests transport integrity only, not geometry decoding. Full browser history action against real queued Spark result remains next. No deployment.


## Stage 12a — browser history-to-project model import

Exported current web app (27 routes) to /tmp/vibex-model-history-web-20260904. Ran local model-history-browser-canary.py on62916 with CORS for web62917. It serves the actual retained Spark-generated chair as an explicitly seeded owned history fixture; this is not a browser-initiated live inference test and does not reuse the real Spark queue DB. Paired through visible UI with fixture code, selected disposable Background removal test project, clicked Use 3D model in this project in server history. Row became Added to project and offered Open builder. Builder Chat and Files showed assets/mtnqvvvv-lzb1kx-model-68cf4a68.glb. Full navigation to root and reopening project retained both.

Direct reload of dynamic project URL404ed on Python static HTTP server (no dynamic route fallback); deep-link serving remains unverified. Root reload persistence passed. No interactive mesh preview/native runtime claim. Browser tab20 closed; exact owned API PID38480 and static hostPID38841 stopped. No production changes.


## Stage 12b — owned generation result as immutable input

Added authenticated POST /api/studio/jobs/{id}/input for completed image results, recognized by generation-auth middleware path routing. It reuses owner/status/artifact integrity checks, accepts only supported image MIME, bounds the snapshot read to20MiB, verifies that read against the saved output hash, decodes/dimension-checks, then stores a deduplicated owner-scoped immutable input. Does not require Library permission for the owner's own job. Shared snapshot concurrency guard applies. Client snapshotStudioImage uses generation credentials and shared strict snapshot-response validation.

Two focused API tests passed (new snapshot ownership/status/dedup/corruption plus model result regression), 16 client tests passed, TypeScript passed. No browser action calls this yet; upcoming Make3D workflow can reuse a completed cutout without downloading/reuploading it. No production deployment.


## Stage 12c — exact 3D client submission contract

Added listModelEngines/submitModelJob/readModelJob. Discovery accepts only server-advertised TripoSR CPU float32 seed7 res128 vertex-color v1 with exact converted-model SHA. Submission validates input/variant/request ID, transmits explicit kind/model revision and immutable input hash. Status/cancel refuse mismatched kind/ID. Generic503 message now names unavailable generation capability instead of incorrectly saying background removal for every operation. No server engine advertised or UI submission enabled. Caller contract requires persistence before send; durable model workflow remains next.

19 generation-client tests passed; TypeScript passed. Added tests for exact advertised discovery, no-engine absence, precise submission payload, pretransport substitution refusal, and poll/cancel identity/kind checks. No deployed changes.


## Stage 12d — durable model requests

Added model-workflow using the existing background workflow's persistence/serialization pattern with its own storage namespace. Owned image job snapshot precedes durable saving; exact input, model revision/variant and stable request ID are retained before submit. Lost response retries reuse exact arguments. Cancellation intent survives failures; duplicate preparation/polling serialize, terminal jobs stop polling, and requests are origin-bound. Pending preparation identity includes engine/revision/variant. No UI action yet and unsupported variants remain rejected by submitModelJob.

11 model/background workflow tests passed; TypeScript passed. Tests include module-reload recovery, cancellation after lost acceptance, storage failure preventing submit, duplicate serialization, origin separation, terminal polling, no stored credentials, variant separation and owned-job snapshot arguments. No production changes.


## Stage 12e — Library 3D generation controls

Added ModelGeneration panel keyed by server origin. Completed image rows in saved background jobs and server history can select a source for Make draft3D asset on this server. Panel explains single-object transparent cutout and draft geometry limitations. Discovery gates submission; no compatible advertised engine means disabled start. Saved requests poll while focused, retain cancellation/recovery controls, display result-import action, Added to project and Open builder. Polling uses durable workflow serialization. Separate capability error survives request polling; storage-read failure in action cleanup is caught. No engine registered/advertised by production.

TypeScript and full app suite417 tests across48 files passed before final wording-only status correction (cancelled/failed prioritized over stored cancellation flag). UI rendering, layout, input-selection visibility, reconnect and live3D submission still require browser/native validation; do not treat unit suite as proof of new controls. No deployment.


## Stage 12f — browser 3D controls and availability correction

Browser test against isolated no-engine glb-browser-canary identified misleading unavailability text when pairing expired. Model panel now distinguishes checking capability, discovery error and successfully empty capability list; resets stale engine during focused discovery. TypeScript and27-route web export passed. Re-exported /tmp/vibex-model-controls-fixed-20260904. Browser verified expired pairing shows connection error without claiming model absence; reconnect through visible UI then shows no compatible model for actual empty list. Selecting Make3D on completed cutout shows source title and disabled start. No request submitted, engine unadvertised. Enabled/live submission remains unverified.

Test tab22 closed, exact local API PID55216 and webPID58246 stopped. Initial pre-host-open tab21 got connection-refused error and could not be reselected because browser tool blocked its data-URL error document; that error tab may remain. No production changes.


## Stage 12g — authenticated API through real Spark 3D worker

New isolated api-v1 directory on Spark contains copied current controller/worker modules, private SQLite, results and canary. In-process FastAPI TestClient authenticated with actual signed studio_jobs tickets: seeded completed chair-image fixture → owned image snapshot → exact model submission → duplicate request returns same ID → default owner-checked store input resolution → real bounded TripoSR CPU execution → authenticated exact GLB download. Other device denied image snapshot, foreign input reuse and model content (404). GLB response had glb-v1 marker and matched expected410488 bytes SHA a7380f83524d536e2d4d8b7c154bcb3b8b73e3f43ec00970a7823623491984a3. Job bf99083b5ca24b4194df180895be1020 succeeded.

Transient unit vibex-triposr-api-v1-20260904 exited0 and inactive/dead.12GiB/no-swap/400%-CPU/600s limits; systemd reported2.4G peak,0B swap. Controller used existing controller-cache nudPWqFGqXq4OSo1 Python with FastAPI0.141.1; model used separate runtime via a fixed wrapper that invokes seccomp deny-network before model execution. ASGI controller itself was not network-denied (TestClient requires event-loop socketpair); it opened no public server. No dependencies installed/modified remotely.

Evidence triposr-review/api-v1-canary.py and api-v1-proof.json. This proves real API/queue/worker/download integration, not browser-to-live-worker or complete model installation/qualification. Source is an explicitly seeded chair image fixture, not live background removal in the same test. No production changes.


## Stage 12h — browser through live Spark inference

Used existing isolated api-v1 code/runtime with new browser-live SQLite/results. Fixed test-only stdlib HTTP transport forwarded requests to the existing FastAPI ASGI app; bound Spark127.0.0.1:62919 only, SSH tunnel local62916, staticweb62917. Existing controller Python lacks uvicorn, so no dependency installation was needed. Test harness explicitly advertised the exact candidate variant for this evaluation only; this is not installer qualification or public registration. Signed pairing through visibleUI seeded one owned chair-image fixture.

Browser selected server-history image, clicked Make draft3D asset, observed queued saved request, fully navigated to root while inference continued, returned to Library and recovered Draft3D asset ready. Chose disposable Background removal test project, imported GLB, saw Added to project, Open builder and Chat attachment assets/mtnrjx44-1vce8u-model-7116bf3f.glb. Actual Spark job7116bf3fb5de4ee0a9c0a0446b7e883d succeeded;410488bytes SHA a7380f83524d536e2d4d8b7c154bcb3b8b73e3f43ec00970a7823623491984a3. New UI issue: same model appears in local request panel and server history; exclude locally represented model job IDs next.

Network denied in model child via existing fixed wrapper. Testserver unit12GiB/no-swap/400%-CPU/1800s; worker600s limit. After terminal job evidence and no childprocesses, stopped exact testunit vibex-triposr-browser-v1-20260904 (inactive/dead). Closed browser23 and exact SSH tunnelPID67691/staticserverPID67695. Script evidence triposr-review/browser-live-canary.py. No production changes. This proves browser→server→realworker→project import in test transport, not physicalMacoff/publicdomain/nativeplatform/installer support.


## Stage 12i — exclude locally displayed 3D jobs from history

ModelGeneration now reports job IDs represented by its visible persisted request records. Library adds those IDs to GenerationHistory exclusions only when their reported origin matches the selected server, alongside existing background job exclusions. No jobs are deleted or globally hidden; another device without the local request sees the server-history row. TypeScript passed and25 model-workflow/generation-client tests passed. This validates typing and underlying contracts, not duplicate-row rendering; browser recheck remains open.


## Stage 12j — exact TripoSR runtime notice inventory

Compared all41 installed distributions against inference.lock plus native-wheel.lock, parsing version pins and direct-wheel filenames; no missing/extra/version-mismatched packages. Reused existing runtime_inventory collector without importing model packages. Initial inventory lacked wheel notices for tokenizers and antlr4-python3-runtime; existing pinned tokenizers supplement covered the former. ANTLR verified sdist SHA f224469b4168294902bb1efa80a8bf7855f24c99aef99cbefc1bcd3cce77881b also lacked a separate license file. Added full upstream LICENSE.txt from4.9.3 tag resolved commit e4c1a74c66bd5290364ea2b36c97cd724b247357,2699bytes SHA b1b379fcaf3219593a4c433feb1b35c780bed23fafaae440b1ae2771a9521e3a. Manifest binds packageversion/source/hash; preserved full text including unrelated JS notices.

Refreshed inventory41packages, missing_notice_files empty; inventory SHA d44c8ca9cb995f8e0ae28028968b8a4ab48acc4109c4325ed9d2825b24477a70. Evidence triposr-review/runtime-inventory.json and collect-runtime-notices.py; remote license-review directory only, no runtime changes. Two inventory tests passed. Notice availability is not complete vendored/native/source-obligation closure; install promotion remains blocked pending that audit and host qualification. No production changes.


## Stage 12k — ELF dependency inventory

Added native_inventory collector: checks installed package versions, confines metadata-listed files to selected runtime, recognizes ELF headers without importing/loading libraries, invokes readelf --dynamic with per-file timeout, hashes bytes and records NEEDED/SONAME/RPATH/RUNPATH. New-output-only CLI. Bounded1000 ELF files/2GiB each. Four native/runtime inventory tests passed for metadata parsing, version/path refusal and notices. Actual Spark collection exited0, no model execution or runtime mutation.

205 ELF files total642610489 bytes; inventory SHA e2639c41d2b655d00825988411f3786e7f3a42c0c305487eab287695dff369f7, evidence triposr-review/native-inventory.json. torchmcubes extension200688bytes SHA02375e3208c2aa4a79887382ee8ace6b6ff24c10459f5588c2b423082260a700; needs libtorch.so/libc10.so/libtorch_python.so/libtorch_cpu.so/libgomp.so.1/libstdc++.so.6/libgcc_s.so.1/libc.so.6/ld-linux-aarch64.so.1; no RPATH/RUNPATH or CUDA NEEDED entries. Names absent from inventory: libc.so.6,libdl.so.2,libgcc_s.so.1,libm.so.6,libpthread.so.0,librt.so.1,libstdc++.so.6. Name presence does not prove actual linker resolution or ABI/notice closure; inventory includes build tools and potentially test binaries. No install promotion or production changes.


## Stage 12l — smaller independently tested runtime

Traced direct imports of prepared offline source/worker and dependency metadata with environment/extras markers and installed version constraints. Selected11 roots and33-package closure. New minimal-v1.lock retains exact requirement blocks/hashes from inference.lock/native-wheel.lock; removed kornia,kornia-rs,timm,pathspec,pybind11,scikit-build-core,cmake,ninja. Installed via --require-hashes --no-deps into new runtime-minimal-v1 using existing uv; original runtime untouched. uv pip check passed all33 installed packages.

Bounded network-denied Spark tracer unit vibex-triposr-minimal-v1-20260904 exited0/inactive/dead.12GiB/no-swap/400%-CPU/600s limits; systemd2.5G peak/0B swap. Model load/export26.79sec,410488byte chair GLB same hash a7380f83524d536e2d4d8b7c154bcb3b8b73e3f43ec00970a7823623491984a3. docs/triposr-runtime-review.json records candidate exact versions; deliberately outside install catalog. Evidence minimal-runtime-plan.py,minimal-v1-plan.json,minimal-v1.lock,minimal-v1-receipt.json. This is source/dependency review plus real tracer, not proof every input/path/platform is supported. Portable install recipe and remaining redistribution/host qualification remain open. No production changes.


## Stage 12m — portable verified runtime lock

Committed candidate33-package lock template without test-host absolute paths and triposr_runtime.prepare_lock. Renderer verifies exact ANTLR/native wheel hashes, rejects symlinks/oversized/missing files, encodes wheel directory as fileURI, writes new output only and returns template/rendered hashes. Does not download/build/install/register itself. Two tests passed for spaces, existing-output preservation, corrupted/symlink wheel refusal. Template SHA850df3b9ccb6414d51d6a26e26c0ebe87d1ea50fe638eb0c7592965c7556b0fa.

Actual Spark copied only two reviewed wheels into portable-v1/wheel cache; renderer verified them and produced lock SHA f51b04090fe3c181040a60e7ed4b370e05c93959d15f6db569d71bcd066fe8db. Installed33 packages with --require-hashes --no-deps into fresh portable-v1/runtime with spaces; install exited0 and uv pip check passed. No model inference repeated in this relocated environment; previous minimal runtime tracer remains separate evidence. Original environments retained. Portable native source build/conversion recipes, complete redistribution and host/installer lifecycle still unfinished. No production changes.


## Stage 12n — explicit candidate runtime compatibility check

Added data/triposr-runtime.json for exact33-package Linux aarch64 CPython3.12.3 candidate, and triposr_compatibility.validate_runtime/verify_runtime. Worker checks host/Python/complete normalized installed version set before model imports; refuses extra/missing/duplicate/drifted distributions. Successful receipt records platform and canonical package-version SHA. This is version compatibility evidence, not installed-file integrity, full platform support or installer approval. Older41-package review runtime no longer satisfies current worker guard; retained historical copies/results unchanged.

12 compatibility/input tests passed. Real minimal runtime accepted with versions SHA e9f892fdab5d4bff1238a16f2e3ca254a2a241bdf66a1ddba6ea7c56e5f7ca03. Updated worker ran in relocated portable-v1/runtime with spaces via network-denied bounded unit vibex-triposr-compatible-v1-20260904, exited0/inactive/dead.26.38sec model-load/export; same410488byte chair hash a7380f83524d536e2d4d8b7c154bcb3b8b73e3f43ec00970a7823623491984a3. Evidence triposr-review/compatible-v1-receipt.json. No production changes or new engine registration.


## Stage 12o — reproducible CPU native build recipe

Added tools/build-triposr-mcubes.py. Verifies exact206534byte source archive/hash/revision, reviewed LinuxARM64Python3.12.3 and build dependency versions; extracts bounded regular source into new output; explicitly disables CUDA, locates Torch/pybind CMake configs via package metadata, limits parallel build to2, retains source and emits wheel receipt. Caller must enforce OS isolation. First two bounded offline builds succeeded but differed: compiled extension embedded absolute source paths. Fixed source/runtime paths with compiler -ffile-prefix-map plus SOURCE_DATE_EPOCH1735191863 from pinned archive.

Fresh v3 andv4 builds in different directories both produced91115byte wheel SHA72c5acf87e5d90ed8522034506afbb755404c15fa46f04376a5095df9ac918c5. Units vibex-triposr-build-portable-v1/v2/v3/v4-20260904 all exited0/inactive/dead; seccomp network denied,12GiB/no-swap/200%-CPU/600s. Final paired builds consumed~24CPU sec each; v3reported2.9Gpeak. Syntax compile passed. Evidence native-portable-v3-receipt.json/native-portable-v4-receipt.json. This proves byte reproducibility for two builds on the same reviewed toolchain with different source roots, not arbitrary compiler/distribution reproducibility. New wheel has NOT been installed/promoted; existing runtime lock still pins original qualified wheel. Next: native smoke and complete model tracer for rebuilt wheel, then intentional pin update. No production changes.


## Stage 12p — rebuilt wheel runtime qualification

Installed the reproducible native wheel (91115 bytes, SHA72c5acf87e5d90ed8522034506afbb755404c15fa46f04376a5095df9ac918c5) into fresh runtime-rebuilt-v1 using a hash-required, no-dependency install of the 33-package lock. uv pip check passed. Current compatibility-guarded worker ran under network-denying seccomp and bounded systemd unit vibex-triposr-rebuilt-v1-20260904 (12GiB, no swap, 400% CPU, 600 seconds); unit exited0/inactive/dead, 2.5G peak, zero swap. Full model load, native marching cubes, GLB export/decode completed in26.68 seconds. Chair output remains410488 bytes,10259 vertices,20448 triangles, SHAa7380f83524d536e2d4d8b7c154bcb3b8b73e3f43ec00970a7823623491984a3.

Updated both candidate native-wheel pins only after the successful tracer. New portable template SHA c1a8fac48418f02ba270540b17052cac457c8a04c353fa9960971e2f558cd79b. Renderer verified actual new native and existing ANTLR artifacts from fresh directory with spaces, producing lock SHA e8e90814e5d3f476c6408f2e7e1fe70d2baa30e973baae00e8f3925a27580283. Fourteen focused runtime, compatibility and CPU-input tests passed after the pin update. Evidence triposr-review/rebuilt-v1-receipt.json. Existing environments and outputs preserved. This candidate remains outside the install catalog: complete source/redistribution closure, host lifecycle and broader platform/quality qualification remain unfinished. No production changes.


## Stage 12q — reproducible ANTLR source build and complete rebuilt runtime

Previous goal turn made progress by qualifying and pinning the native wheel. Added tools/build-triposr-antlr.py: exact117034byte source archive SHA f224469b4168294902bb1efa80a8bf7855f24c99aef99cbefc1bcd3cce77881b; reviewed LinuxARM64 CPython3.12.3/setuptools81.0.0; bounded regular archive extraction, new output only, SOURCE_DATE_EPOCH1636221141, wheel receipt. It does not install/register or enforce its own network isolation; caller does.

Spark units vibex-triposr-antlr-v1/v2-20260904 both network-denied,2GiB/no-swap/200%-CPU/120s and terminal exited0/inactive/dead. Different output roots produced identical144590byte wheels SHA2a87ebcac22b720a265c06148b104f28eca7bf1203201eb971f37184bcef5569. Compared every wheel member with original qualified ANTLR wheel: same ordered member names and identical uncompressed bytes; ZIP timestamps differ. Evidence antlr-portable-v1-receipt.json and antlr-portable-v2-receipt.json. Same-host/toolchain reproducibility only.

Installed both reproducibly rebuilt wheels with full33-package hash lock into fresh runtime-all-rebuilt-v1; uv pip check passed. Current guarded worker under network-denied12GiB/no-swap/400%-CPU/600s unit vibex-triposr-all-rebuilt-v1-20260904 completed actual chair model/native mesh/GLB tracer in26.83sec; exited0/inactive/dead,2.5Gpeak/zero swap. Same410488byte GLB SHAa7380f83524d536e2d4d8b7c154bcb3b8b73e3f43ec00970a7823623491984a3. Saved all-rebuilt-v1-receipt.json. Updated both ANTLR candidate pins after success; template SHAdbb16944992a100d05b11fc9794693f3fdf64de6870f18f0331a56f50ee50d7e. Syntax compilation and14 focused runtime/compatibility/input tests passed.

No original runtime, jobs or outputs modified; no production change. Candidate still outside installation catalog. Next needs include assembled source/build/conversion manifest, complete third-party/native redistribution closure, host lifecycle and broader UI/platform qualification. Full unified-studio goal remains active.


## Stage 12r — close local encoder-config integrity gap

Previous turn was progress (reproducible ANTLR build, complete fresh runtime tracer and pin update). While assembling the package contract, inspection found prepare-triposr-source adds dino-config.json separately from the upstream file list. The pinned source receipt therefore did not cause verify_package to hash this runtime-read configuration. Added explicit454byte bound and pinned SHA b87c0270b97db085fd82cf114a761fd0f62ae7914fbd407c752a2260646b689c before model imports. No source receipt or model bytes changed.

New regression test proves valid package acceptance and altered/symlink local configuration rejection.13 focused CPU/compatibility tests passed. First edit command used a duplicated media-lab path and failed without changing files; corrected the working-directory-relative paths and reran tests after actual edits. Read-only verification of the real Spark package using copied current verifier passed full source, DINO config, model config and SafeTensors hashes; no inference repeated because the change adds pre-load integrity checks only. Existing retained model-generation proof remains separate. No production/runtime/job mutation. Assembled installer manifest still unfinished; next continue that integration and third-party closure.


## Stage 12s — candidate build manifest and drift refusal

Previous turn was progress (DINO configuration integrity fix). Added data/triposr-build.json as shared candidate source for both wheel hashes and seven exact recipe/config/lock files. triposr_runtime derives wheel pins and template hash from that manifest. prepare_lock now rejects any unreviewed template change before writing; verify_recipes checks file size/hash and rejects missing, symlink or escaped recipe files without executing them. Optional --recipes points to the Media Lab repository for the CLI check. Manifest remains candidate-not-installable and lists unresolved lifecycle/redistribution/platform work. It is a repository-controlled consistency contract, not a signed external trust root or full runtime integrity proof.

17 focused runtime/CPU/compatibility tests passed, including lock drift refusing output, actual repository recipe consistency, changed recipe rejection and symlink rejection. Copied current recipes/manifest into fresh Spark manifest-v1, verified all seven real files and both reproducibly built wheels, rendered lock in wheel directory with spaces. Result lock SHA c4100f46c170d80d4554c1bef23d889f160d39fcc1817d7ab6b32a9cfb0bb765; unchanged template SHAdbb16944992a100d05b11fc9794693f3fdf64de6870f18f0331a56f50ee50d7e. Receipt triposr-review/build-manifest-v1-receipt.json. No inference repeated: hashes match the full runtime tracer from12q. No install registration, production changes or user data changes. Next: assemble the actual isolated installation lifecycle and complete redistribution review before enabling a capability pack.


## Stage 12t — runtime provenance required for result recovery

Previous turn made progress (build manifest and real artifact check). Controller result verification previously ignored the runtime provenance field even though the current worker emits it. Added expected_runtime_receipt from the pinned runtime spec without inspecting/importing controller runtime packages; publication/recovery now refuses missing or mismatched platform, Python and package-version digest. Existing outputs are never altered or silently requalified. This remains declared version provenance, not installed-file integrity or a signed attestation.

16 focused result/worker/compatibility tests and16 subtests passed, covering missing/mismatched runtime receipts and preserved output/cancellation behaviors. Read-only Spark current verifier accepted all-rebuilt-v1-result (410488byte chair GLB SHAa7380f83524d536e2d4d8b7c154bcb3b8b73e3f43ec00970a7823623491984a3), refused historical minimal-v1-result without runtime provenance, and verified the historical receipt bytes unchanged. Evidence triposr-review/result-provenance-v1.json. No inference rerun or production changes. Full goal remains active; installer lifecycle, runtime file integrity, redistribution closure and broader unified UX/platform work remain incomplete.


## Stage 13m — exact rebuilt runtime notice and native inventory

Previous stage was progress (427 app tests, typecheck/lint and current unsigned simulator build passed). Signing question remains pending; independent work continued. Collected installed notice files and readelf metadata from the exact runtime-all-rebuilt-v1 on Spark, without loading model/native libraries or starting inference. Both collectors exited0. New isolated evidence directory rebuilt-inventory-13m; no production changes.

33 packages;200 ELF files totaling573035737 bytes. Every package has discovered or pinned supplemental notice text; ANTLR and tokenizers still require the bundled supplemental notices. Notices SHA2565d852c014ca948e9833f38acf02daaae59fe5816630af47d2d374688bef55b4b; native inventory SHA25655e8d010070a4ed61e1929016944736423e3d7d985fdf91a3f8fecd176e7b13d. External dependency-name candidates: libc.so.6, libdl.so.2, libgcc_s.so.1, libm.so.6, libpthread.so.0, librt.so.1, libstdc++.so.6. Name matching is discovery only, not linkage resolution.

Concrete review gap: torchvision0.26.0+cpu includes8 ELF files, including torchvision.libs/ld-linux-aarch64.5cbc5e90.so.1 plus bundled JPEG, PNG, WebP, sharpYUV and zlib libraries; its discovered notice is only torchvision's1517-character LICENSE. Obtain exact wheel build/source provenance and corresponding third-party notices/source obligations before making this pack installable. Also review statically bundled components in Rust extensions (tokenizers, safetensors, hf-xet) and torch/numpy/pillow vendor coverage. Top-level package notice presence does not prove redistribution closure. Candidate status stays unchanged. This is a sharper next action than relying on the older41-package development inventory.


## Stage 13n — torchvision binary provenance traced to pinned source

Previous stage made progress by inventorying the exact33-package runtime. Read installed torchvision/version.py without importing it: git_version336d36e8db990a905498c73933e35231876e28bc; wheel tagcp312-cp312-manylinux_2_28_aarch64, setuptools72.1.0. Downloaded five upstream text files at that immutable commit into external evidence torchvision-source-13n with byte/hash receipt; no scripts executed.

The Linux workflow delegates to pytorch/test-infra release/2.11 (mutable branch). Its selected pre-script is packaging/pre_build_script.sh, which installs libpng/libjpeg-turbo/libwebp without fixed versions, and auditwheel<6.3.0. The similarly named pre_build_script_arm64.sh is a Windows build path and is not evidence for this Linux wheel. packaging/wheel/relocate.py follows ELF dependency closure, copies libraries outside its allowlist, renames them using a hash of the source path (not file content), and rewrites dependencies. Its allowlist includes ld-linux-x86-64.so.2 but omits ld-linux-aarch64.so.1. This is consistent with the observed bundled ARM loader; an actual build log/container receipt would be needed to establish the full executed build.

Consequently, neither hashed library filenames nor torchvision's git_version identify the bundled codec/loader source versions. Do not attach guessed notices or mark redistribution complete. Next lifecycle work must obtain matching upstream build/container provenance or explicitly qualify a separately pinned source build with its complete dependencies; any changed runtime requires new hashes and tracer evidence, never silent substitution. Existing verified runtime and candidate status remain unchanged. No signing answer received; native test remains pending, other work available.

Sources: https://github.com/pytorch/vision/blob/336d36e8db990a905498c73933e35231876e28bc/packaging/wheel/relocate.py and https://github.com/pytorch/vision/blob/336d36e8db990a905498c73933e35231876e28bc/packaging/pre_build_script.sh .


## Stage 16ar — test whether torchvision is needed

The pinned prepared TripoSR source contains no direct torchvision import. A new
experimental virtual environment on Spark exposes the existing pinned runtime
packages through read-only-use symlinks, excluding torchvision, its metadata and
bundled libraries. Bytecode writes are disabled. The original runtime, lock and
qualification remain unchanged; this is not a distributable installation.

With torchvision absent, Transformers ViTModel/ViTConfig and TSR imports passed.
A separate bounded CPU process then verified every prepared-source hash, config
hash and the complete SafeTensors hash before strictly loading all 419,275,628
parameters. Model construction and loading took 2.684 seconds; the process exited
zero. It used an 8 GiB memory cap, no swap, four-CPU quota, 90-second limit and
AF_UNIX-only socket families, with Hugging Face/Transformers offline flags. The
reported systemd memory peak was implausibly small and is not a memory-floor
measurement. No inference or GPU work was performed.

This makes dependency removal a concrete alternative to reconstructing the
opaque torchvision wheel's bundled codec/loader provenance. It does not yet
prove full inference equivalence or dependency closure. Next: trace the same
pinned input under the reduced environment, compare decoded/canonical output,
and only then prepare a distinct immutable runtime specification and lock.
Remaining torch, NumPy, Pillow and Rust-extension notice/source review still
applies. Both test units are inactive; production services remained active.


## Stage 16as — reduced-runtime inference equivalence

The same pinned chair input, prepared source, SafeTensors, seed 7, CPU float32
and resolution 128 were traced with torchvision absent. The process verified
input/source/model hashes and explicitly confirmed IPv4 socket creation was
denied. It held the canonical inference lease, used a 12 GiB memory cap, no
swap, four-CPU quota and 120-second deadline, then exited successfully.

Inference and canonical export took 21.913 seconds. The decoded output contains
10,259 vertices and 20,448 triangles, is 410,488 bytes, and has SHA-256
`a7380f83524d536e2d4d8b7c154bcb3b8b73e3f43ec00970a7823623491984a3`.
A direct byte comparison against the previous all-rebuilt runtime output passed.
This establishes equivalence for this tracer, not general model quality.

A separate experimental lock removes only the exact torchvision wheel stanza
from the prior rendered lock. Source lock SHA-256 is
`03b1bc8f9b3747049267dc71c3373a4b09f8823565fd4cb960bfa3aca79bbc68`;
candidate lock SHA-256 is
`e3ad23da718e91042ddbf0e0fb106d2641d9bbb8e5b798cd7650cb19a65f71ce`.
It remains external development evidence with machine-local built-wheel paths.
Next validate a fresh installation from that lock, its actual dependency graph,
and the same tracer before introducing a distinct portable runtime manifest.
The checked-in runtime and install catalog are unchanged.


## Stage 16at — fresh reduced-runtime installation

A new empty virtual environment installed all 32 packages from the candidate
lock with hash enforcement and binary wheels only. Dependency resolution passed
without reintroducing torchvision; `pip check` reported no broken requirements.
The complete installed version map exactly equals the previous runtime minus
torchvision. Existing environments were not changed.

The same network-denied, lease-held CPU tracer then passed in the fresh runtime
and produced the same 410,488-byte GLB checksum as stages 16as and 12q. Inference
and export took 21.115 seconds. Both test units stopped successfully; production
services remained active. This validates installation from the development lock,
not a fresh-machine installer: two rebuilt wheels still use reviewed local
artifact paths. The separate `triposr-reduced-runtime-review.json` records this
candidate; the active runtime manifest and catalog remain unchanged.


## Stage 16au — explicit portable reduced profile

`media_lab_core.triposr_runtime` now accepts `--profile without-vision-v1`
with a separate reviewed build manifest, runtime specification and portable
lock template. Omitting the flag retains the original profile. Both use the
same verified rebuilt native wheels. This command only verifies and renders;
it does not install, activate or register a model.

Run the existing recipe command with `--profile without-vision-v1`, `--recipes`
pointing at this Media Lab checkout, `--wheels` pointing at the verified rebuilt
wheels and `--output` naming a new lock file. The reduced template SHA-256 is
`3d769e74b4fcd3c8bc461fc05d41390e607dc634dae7521133439cb81e9ebadd`.
An actual Spark run verified the recipes and wheels and rendered a lock with
SHA-256 `e557408aa53aec0c574d7986b64e57bec6d475ee7cb99209d359ad61cc4d85fb`.
Requirement and hash lines matched the fresh-install candidate after normalizing
local wheel paths and comments. Nineteen runtime/compatibility/result tests and
sixteen subtests passed. The worker's default runtime identity and historical
receipts remain unchanged; reduced-profile worker integration and remaining
notice review must precede installation qualification.


## Stage 16av — explicit worker runtime provenance

The CPU entry point and bounded worker now accept `runtime_profile`, defaulting
to `original`; the CPU CLI exposes `--runtime-profile without-vision-v1`. The
worker forwards the profile to its child and verifies the same profile on both
new results and recovery. Reduced receipts include the explicit profile and
the exact 32-package digest. Original receipts retain their historical shape.
Unknown profiles are rejected before worker writes or execution.

Thirty-one focused tests and sixteen subtests passed, including rejection of
original receipts under the reduced profile and vice versa. The actual fresh
Spark environment passed the updated verifier with package digest
`2c197bf6f3ff15de89a6759ba8f5de9fabad195ee47c851a2be4cdf2cce37008`.
This verifies the runtime check on real packages; a full invocation of the
updated bounded worker still needs a new tracer/result receipt. No engine was
registered and no historical receipt was rewritten.


## Stage 16aw — reduced-profile bounded worker and recovery

The actual bounded worker ran in the fresh Linux ARM64 environment with
`runtime_profile=without-vision-v1`, network sockets denied and the canonical
inference lease held. It published a verified 410,488-byte GLB with SHA-256
`a7380f83524d536e2d4d8b7c154bcb3b8b73e3f43ec00970a7823623491984a3`.
Its receipt records the reduced profile and exact 32-package digest. A second
call recovered the result even with a nonexistent Python executable, proving
recovery did not relaunch inference. Verification under the original profile
rejected that receipt. The unit completed successfully in 25.957 seconds and
was confirmed inactive with MainPID=0; all four production services remained
active. The initial harness launch had a quoting syntax error before execution;
the corrected script was transferred as a file and passed.

This closes the bounded-worker evidence gap from stage 16av. It does not qualify
the engine for installation: remaining dependency notices and source review,
installer lifecycle, broader platform acceptance and output quality still need
work. No production service or engine registration changed.


## Stage 16bc — pinned Rust extension source archives

Read-only inspection of the fresh reduced runtime confirmed that tokenizers
has no installed notice file, while safetensors and hf-xet carry top-level
licenses. Torch and Pillow carry substantial combined notices; NumPy explicitly
lists bundled libraries. These inventories do not establish full correspondence
between notices and binaries.

Downloaded PyPI source distributions for the exact installed versions of
safetensors 0.8.0, tokenizers 0.22.2 and hf-xet 1.6.0. Each archive matched its
PyPI SHA-256 and byte count. Inspected tar members in memory without extraction
or execution. Their Python binding Cargo.lock files list 69, 149 and 422
packages respectively (67, 147 and 416 registry packages); no Git dependencies
appear in these lockfiles. The remaining entries are local workspace packages.
`triposr-rust-source-review.json` records immutable archive and lockfile hashes.

These source locks include optional/development dependencies and are not proof
of the installed wheels' actual build graph. Next: resolve the target/features,
collect matching crate notices, and establish wheel build provenance or qualify
a pinned rebuild. No model runtime, engine registration or production service
changed. Evidence is in rust-runtime-sources-16bc under the external review
directory. The capability remains unqualified for installation.


## Stage 16bd — hash-matching Rust crate notice collection

Reverified the three source archive and Cargo.lock hashes, then collected all
555 unique registry name/version pairs across those locks. Each crate archive
matched its locked SHA-256 before tar inspection; no archive content was
executed or extracted wholesale. Transfers totaled 112,250,414 bytes. The
collector completed with no errors and saved 998 conventionally named notice
records. Every saved notice was subsequently rehashed successfully.

29 packages have no conventionally named LICENSE/NOTICE/COPYING/COPYRIGHT file
in this scan. This is a collection gap, not proof they lack licensing text: it
may occur in README, source headers or the upstream workspace. Their declared
license expressions are recorded separately. `triposr-rust-notice-review.json`
lists the exact gaps and pins the detailed external manifest, SHA-256
`7b080ad74357544edd2cab16cf6bab2be36fbbff866ba1f8312ba4999ab0d3ad`.

The source lock graphs include development, optional and other-platform
packages. They must be narrowed to actual Linux ARM64 build features before
being treated as a runtime bill of materials. Wheel correspondence and notice
review are still required. No installed runtime, model, catalog or production
service changed. External evidence: rust-crate-notices-16bd/collect.py,
report.json and per-package receipt/text files.


## Stage 16be — Linux ARM64 source dependency graphs

Used Cargo 1.98.0 metadata, not a build, with locked source archives, isolated
Cargo home, aarch64-unknown-linux-gnu target, default features and the
pyproject-declared pyo3/extension-module feature. All three commands exited
successfully and original lockfile bytes were unchanged. Normal/build traversal
from each Python binding root yields 33 safetensors, 123 tokenizers and 223
hf-xet registry packages (342 unique name/version pairs across all three).
All have collected notice files. The prior 29 collection gaps are outside these
selected graphs. Counts include build dependencies and are not counts of
libraries physically bundled in a wheel.

Recorded target configuration and metadata hashes in
`triposr-rust-target-review.json`; full graphs reside in external evidence
rust-target-graph-16be. hf-xet's graph includes colored 3.1.1 and option-ext
0.2.0 declaring MPL-2.0; preserve their own terms and review actual distribution
requirements rather than relabeling dependencies as Apache-2.0.

This is the source-default graph, not proof of upstream wheel build flags,
compiler, static dependencies or binary correspondence. Cargo feature resolution
and build-script behavior still need review for any qualified rebuild. No
package compilation, runtime installation, inference or production changes
occurred. Next: establish matching wheel build provenance or qualify an
explicit pinned rebuild using these source/notice receipts.


## Stage 16bf — exact-wheel published attestation discovery

Read exact filenames and hashes from the successful 16at pip install report.
Queried PyPI Integrity API for those three wheels. safetensors and tokenizers
returned HTTP 404. hf-xet returned a PyPI Publish statement whose subject name
and SHA-256 match the installed-wheel receipt; the publisher metadata names
huggingface/xet-core, release.yml, environment release. Saved raw provenance
and its SHA-256 plus decoded statement in external rust-wheel-provenance-16bf.
No cryptographic signature verification was performed; a decoded assertion
is not a verified attestation.

The available statement is a publish attestation, not SLSA build provenance.
It cannot establish compiler, target flags, source dependency graph or source
to binary correspondence. See https://docs.pypi.org/api/integrity/ and
https://docs.pypi.org/attestations/publish/v1/ .
`triposr-rust-wheel-provenance-review.json` records exact queries and results
without upgrading qualification status.

Read-only Spark rebuild preflight: cargo/rustc/maturin absent from SSH PATH,
and no cargo/rustc at the user's standard .cargo/bin paths. 1.4TB disk free,
about 61GB available RAM; swap is already in use by existing workloads. Any
rebuild needs an explicitly pinned isolated Rust toolchain and bounded CPU/RAM
execution. All four production services remained active. No package installed,
model run or production mutation occurred. Next safe implementation is an
isolated pinned rebuild recipe for the Rust wheels, followed by fresh runtime
and tracer qualification, retaining collected notices.


## Stage 16bg — isolated pinned Rust build toolchain on Spark

Rust 1.98.0 distribution manifest dated 2026-08-20 selected for Linux ARM64.
Pinned manifest and cargo/rustc/rust-std component SHA-256 values are recorded
in triposr-rust-toolchain-review.json. Downloaded three archives totaling
108,298,076 bytes under a bounded unit and verified every hash. Reviewed
installer prefix/ldconfig handling; extracted with tar data filter and installed
only into ~/runtime/vibex-rust-toolchain-16bg/toolchain using
--disable-ldconfig. Installation unit allowed AF_UNIX only, MemoryMax2G, swap0,
CPU200%, RuntimeMax180s. It exited successfully in 7.188 seconds.

Actual tool versions: cargo 1.98.0 (797e8a9bc 2026-08-05), rustc 1.98.0
(88d9e12ae 2026-08-18). Compiled and ran a small hello-world executable, yielding
“isolated Rust toolchain ready”. Both owned units subsequently inactive, PID0.
Default SSH PATH still has no cargo/rustc. All four production services active.
No model runtime or package replaced. This prepares wheel rebuilds; it does not
qualify any media capability. Systemd peak-memory reporting was implausibly low
and is not used as a measured resource floor. External evidence rust-toolchain-16bg
contains scripts, version manifest, download receipts and installed.json.
Next: pinned isolated Python build frontend and source wheel rebuild with exact
Cargo locks, followed by fresh runtime/tracer verification.


## Stage 16bh — SafeTensors source wheel rebuilt and smoke-tested

Pinned/hash-verified Maturin 1.15.0 ARM64 wheel and installed it without
dependencies into a separate frontend venv. Verified SafeTensors 0.8.0 source
archive and Cargo.lock; initial target-only cargo fetch missed metadata needed
by Maturin, so the first offline build stopped before compilation. Completed
all locked crate downloads, verified all 67 cached archive checksums, then
rebuilt with Rust 1.98.0, CPython 3.12, release mode, --locked --offline and
--compatibility linux. AF_INET sockets were confirmed denied. Bounded unit
CPU200%/Memory4G/swap0 completed successfully in 10.798 seconds.

Output safetensors-0.8.0-cp310-abi3-linux_aarch64.whl, 505,682 bytes, SHA-256
`d76b9d898895b6caa98ff231a15138e294f3e7b268fe3d968682f37183c77e51`.
Separate empty smoke venv installed it offline. First test used an outdated
serialize dictionary API; captured TypeError identified TensorSpec requirement.
Corrected test uses a live ctypes buffer with the source-documented descriptor.
Network-denied rerun passed float32 binary round trip and malformed input
rejection. Original locks remained unchanged.

This is a Linux-tagged build for this host, not manylinux portability or a
TripoSR model tracer. The wheel includes only its top-level license; collected
crate notices must accompany any qualified distribution. Next tokenizers/hf-xet
rebuilds and fresh full-runtime tracer remain required. All owned units terminal
PID0; failed test units reset after evidence capture. All four production
services active. No working runtime or catalog entry replaced. Exact recipes
and receipts: external rust-wheel-build-16bh and checked-in
triposr-safetensors-rebuild-review.json.


## Stage 16bi — Tokenizers source wheel rebuilt and smoke-tested

Verified Tokenizers 0.22.2 source archive, extracted into a new review root,
fetched all locked Cargo dependencies and verified all 147 cached crate hashes.
Used the existing pinned isolated Rust 1.98.0/Maturin 1.15.0 frontend, not the
working model runtime. Network-denied release build with --locked --offline,
Linux compatibility tag, CPython3.12, two build jobs, Memory4G/swap0/CPU200%
completed successfully in 52.689 seconds. Cargo.lock bytes remained unchanged.
Host C toolchain: GCC13.3.0 Ubuntu13.3.0-6ubuntu2~24.04.1; glibc2.39 Ubuntu
2.39-0ubuntu8.8.

Output tokenizers-0.22.2-cp39-abi3-linux_aarch64.whl, 3,326,770 bytes, SHA-256
`8ba313f1cb09983ce3adb60c84511ccec21b1f1316c33de4aaec601eccd863f6`.
Installed without dependencies in a new smoke venv. Network-denied tests passed
Unicode NFC/lowercase tokenization, unknown-token mapping, serialized tokenizer
round trip, and malformed JSON rejection. No model or hosted tokenizer loaded.
The wheel still contains no notice files, matching its source packaging gap;
collected top-level and transitive notices must accompany distribution.

All three owned units inactive/PID0; four production services active. No
existing model runtime changed. This proves this host's wheel build and basic
API behavior, not manylinux portability, complete dependency installation or
TripoSR inference. Exact source/build/smoke receipts recorded in
triposr-tokenizers-rebuild-review.json and external tokenizers-build-16bi.
Next hf-xet rebuild and fresh full-runtime tracer remain outstanding.


## Stage 16bj — hf-xet source wheel rebuilt and smoke-tested

Verified hf-xet 1.6.0 source archive, fetched exact Cargo.lock dependencies
into a new isolated review directory, and checked all 416 cached crate hashes.
Rust1.98.0/Maturin1.15.0 release build with --locked --offline and Linux tag
completed in 98.822 seconds under CPU200%/Memory4G/swap0, AF_UNIX-only unit.
The original lockfile was unchanged. Output
hf_xet-1.6.0-cp38-abi3-linux_aarch64.whl is 4,461,263 bytes, SHA-256
`0d975d386160ae8a5d98037a5afbacaea81866c2c9ec1eb169af9339a981c322`.

Installed into a separate empty smoke venv, with an isolated HF_HOME and
network denied. Module import, legacy upload/download descriptor fields,
function availability, and negative-size rejection passed. No transfer was
started, and this is not transfer acceptance. The wheel includes the top-level
LICENSE; reviewed transitive notices and source obligations still need to be
packaged. Exact receipts in triposr-hf-xet-rebuild-review.json and external
hf-xet-build-16bj.

All three owned units inactive/PID0 and four production services active. No
working runtime or catalog replaced. Three Rust source wheels are now built;
next assemble a fresh locked runtime, verify its files and notices, and run the
full bounded 3D tracer before any installer qualification. Linux tag does not
claim manylinux or other-platform portability.


## Stage 16bk — fresh rebuilt runtime and full bounded worker

Created a new lock by replacing only the three Rust wheel entries in the
verified reduced lock. New lock SHA-256
`dba2964fad6958bb4a2a5b864e47c601bfca031b70baabfceb405879e7a53462`.
Verified replacement wheel hashes/bytes before installation into a new venv.
pip require-hashes/wheels-only install and pip check passed; exact 32-package
version map matches the reduced runtime. Compared every installed entry of the
three rebuilt wheels except installer-rewritten RECORD: all 49 files match the
pinned archive contents. This check does not cover other packages or extra
unlisted files. Detailed file hashes saved in external file-integrity.json.

Actual worker with rebuilt venv, canonical inference lease, network denied,
Memory14G/swap0/CPU400%, 90-second child timeout completed successfully in
38.302 seconds. New job d*32 produced the same 410,488-byte GLB with SHA-256
`a7380f83524d536e2d4d8b7c154bcb3b8b73e3f43ec00970a7823623491984a3`.
Recovery succeeded with a nonexistent Python path, and original-profile result
verification rejected the reduced receipt. No old result or runtime changed.

The worker receipt still identifies the package-version reduced profile; the
separate install/file manifests establish these rebuilt wheel bytes. They are
not yet enforced as a distinct installer/worker profile. Notice packaging,
remaining non-Rust dependency review, portable recipe and broader quality/
platform acceptance remain incomplete. Both owned units inactive/PID0; all
four production services active. No activation or production deployment.
Checked-in triposr-rebuilt-runtime-review.json records the review, with scripts
and full receipts in external rebuilt-runtime-16bk.


## Stage 16bl — reproducible Rust source and notice bundle

Added reusable notice_bundle.build_bundle: validates relative source/archive
paths, exact byte/hash identities, duplicates and size/count bounds; rejects
symlink sources; writes deterministic ZIP metadata and an internal file manifest;
publishes exclusively without overwriting existing output. Nine focused tests
passed, covering changed inputs, invalid paths, symlinks, duplicate names,
repeatable output and existing output protection. Initial system Python lacked
pytest; the established review venv ran the tests successfully.

Built rust-sources-and-notices.zip from 342 registry package versions in the
selected source graphs plus the three pinned Python source archives. Includes
984 source, notice and index/about files, totaling 49,898,837 ZIP bytes. SHA-256
`ed6b25b93d4312c5b2065f5d20c9e5858b92d45dafac88114c28cd2ac8f9401c`.
A second build with reversed input order matched exactly. Independently opened
the ZIP and checked all 984 entry byte counts/hashes against its manifest.
Artifact and reproducible assembly script reside in external
rust-notice-bundle-16bl. Checked-in summary pins the artifact hash.

This bundles exact sources and collected notices, including build dependencies;
it does not complete runtime legal qualification. Non-Rust Python/native
dependencies, Rust standard library/toolchain and OS linkage/distribution
obligations remain separate. No capability activation, runtime replacement or
production change occurred. Full goal remains active.
