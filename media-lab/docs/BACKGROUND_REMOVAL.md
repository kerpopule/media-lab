# Independent background-removal adapter

`media_lab_core.birefnet_cpu.BiRefNetCPU` runs the official BiRefNet checkpoint in
an isolated CPU process. It has no Maestro/WanGP dependency and does not download
anything during loading or inference. It is **experimental, disabled by default,
and not offered by the public installer**. A qualified development host can opt
in to the app's generation API as described below.

The pinned model/source manifest is `media_lab_core/data/birefnet-cpu.json`.
The [official model repository](https://huggingface.co/ZhengPeng7/BiRefNet/tree/e2bf8e4460fc8fa32bba5ea4d94b3233d367b0e4)
labels the package MIT and ungated. These upstream terms remain separate from
VibeXStudio's Apache-2.0 code. No weights or upstream Python implementation are
bundled in this repository.

## Verified development configuration

- Model revision: `e2bf8e4460fc8fa32bba5ea4d94b3233d367b0e4`.
- Weight: SafeTensors, 444,473,596 bytes; SHA-256 in the manifest.
- Platform tested: Apple M3 Max, macOS26.5.2 arm64, Python3.12.13.
- Runtime: Torch2.11.0, Transformers4.57.6; every dependency/version/wheel hash
  is recorded in `birefnet-cpu.requirements.lock`.
- Mode: CPU float32, four threads, deterministic inference, 1024×1024 model input.
- Two reference inferences: 10.61s and10.35s; identical alpha bytes.
- Peak observed resident memory: 8,157,626,368 bytes (about7.6GiB). The development
  supervisor capped the process at12GiB and600seconds; these are admission
  budgets, not promises for other hardware.
- The reusable adapter independently reproduced the same mask in9.96s.

The reference test used one synthetic cup with a known outline. Mask IoU was
0.99916. This is technical tracer evidence only: it does not establish quality
for hair, photographs, fine structures, translucent objects or arbitrary game
art. Network access was denied by an OS sandbox during the test.

## Worker contract

The caller must provide the exact manifest-pinned files in a package directory
and a separate writable cache directory. The adapter verifies file size/hash
and installed dependency versions before loading. Only SafeTensors are selected;
the pinned config disables separate backbone downloads. Custom upstream Python
is loaded only from the hash-verified local package.

Run this in a dedicated process using the separate locked environment. It must
not be imported into a shared model host and then loaded concurrently with other
models: runtime offline settings and CPU-thread policy are process-wide.
The caller must enforce single-flight execution, memory admission and deadlines.
The class itself does not provide a process supervisor or GPU lease.

`cpu_worker.run_background_job` now supplies the experimental POSIX supervisor:
one shared artifact root provides a nonblocking kernel lock, inherited by the
child so a controller crash cannot immediately admit an overlapping process.
All callers on a host must use this same root. The supervisor checks available
RAM before launch, samples the owned process tree's RSS every100ms, enforces a
deadline/cancellation callback, and stops only processes it owns. RSS sampling
is a best-effort limit, not a kernel memory reservation or hard quota.

The child uses the pinned Python environment and a scrubbed environment. It
writes into a private temporary directory; the supervisor verifies the receipt,
input/output hashes and decoded RGBA dimensions before publishing the directory
with an atomic rename. A retry with the same job ID and input recovers the
verified result. Conflicting input or corrupt existing output fails closed.
This is process-crash recovery, not a claim of power-loss durability. The worker
does not itself provide installer registration.

The actual supervised Mac tracer completed in14.54s with sampled peak RSS
6,788,431,872bytes. Its output matched the earlier qualified alpha exactly;
a second request recovered it without starting another process. Networking was
denied by the test's inherited OS sandbox. The worker itself sets model offline
mode but does not install an OS network sandbox on arbitrary hosts.

`background_jobs.run_next` connects that worker to the independent SQLite queue.
It holds the same kernel lock across claim, lease renewal, inference and terminal
publication. Only device-owned image jobs for `birefnet-cpu` are claimed; the
exact pinned revision and immutable input hash are required. A fresh worker
identity fences each claim. Expired jobs may recover only after acquiring the
process lock; expired cancellations become cancelled without running again.
Cancellation also wins a race with terminal result publication. The scoped API
provides `POST /api/studio/jobs/{id}/cancel`, including safe repeated requests.

The opt-in host starts this controller through the app lifecycle. Its default input resolver reads the
immutable SQLite snapshot joined to the job's owning device. A development
host may override the resolver explicitly. An end-to-end development tracer used the real
model, SQLite and isolated ASGI routes, simulated interruption after artifact
publication, then recovered after lease expiry without rerunning inference.
The owning device could download the verified bytes; another device could not.
This does not qualify live host integration.

## Library input snapshots

`POST /api/studio/inputs/library` accepts an existing Library asset ID. It
requires a render ticket in `Authorization` and a separate Library ticket in
`X-Library-Authorization`, each with the `Bearer` prefix. Neither permission
alone can create a snapshot. No caller-supplied filesystem path or URL is used.
The route decodes a bounded single PNG/JPEG/WebP and atomically stores its bytes,
hash and dimensions under the authenticated device identity in SQLite. Two
concurrent preparations are allowed; excess requests receive429.

Identical bytes deduplicate within that device. Another device cannot use the
input ID in a new generation request. Jobs keep their accepted bytes even when
the Library original changes, and the worker verifies their hash before loading
the model. Snapshots have logical byte limits of256MiB per device and2GiB per
server; SQLite/WAL overhead is additional. Retention/deletion UI is still needed.
The app exposes this input route but still refuses unqualified generation; no
background-removal engine is silently registered by creating a snapshot.

The app's `remote-generation.ts` client implements device-preserving pairing,
capability discovery, dual-scope snapshot preparation, exact request-ID retries,
job status/cancellation and SHA-256-checked PNG downloads. Credentials use the
existing native secure store / desktop vault / browser storage abstraction.
Pairing persists identity before network access and uses Web Locks across tabs
when available; older browsers have only an in-process pairing guard. The API
exposes the result hash response header for cross-origin browser verification.
Library now provides the action when a connected server advertises the exact
capability, saved job cards, cancellation and verified PNG import into a chosen
project. Pairing has an explicit generation-permission switch; disabling it or
removing pairing clears the local render ticket while retaining device identity
for later renewal. The access code is never saved.

`background-workflow.ts` stores one local record per request before submission,
including server, exact engine/revision and immutable input. Reloads resume the
same request ID; cancellation intent survives transport failure. Operations are
serialized per request, and preparations per source image, with cross-tab Web
Locks where supported. Unknown acceptance is recovered by exact resubmission
before cancellation; cancellation cannot guarantee that a server never began
work. Request records contain no credentials. Terminal requests stop polling.
Retention/dismissal controls and native lifecycle tests remain outstanding.

An isolated browser test paired both scopes, started real CPU inference,
reloaded while running, imported the resulting PNG into a new local project,
and cancelled a second real job. The390px layout was visually inspected.
The test host explicitly enabled only the previously qualified development
adapter; this is not public installer promotion or production host activation.

## Opt-in development host

The current CPU package can be qualified on macOS arm64 and Linux aarch64
with Python 3.12. Linux selects `birefnet-cpu-linux-arm64.json` and its matching
hash-locked requirements, including CPU-only Torch and Torchvision wheels.
Other platforms remain blocked. First install the
exact manifest-pinned package and hash-locked runtime in separate directories;
this command does not download or install them. From the Media Lab directory,
using its controller environment:

```sh
.venv/bin/python -m media_lab_core.background_host \
  --root "$HOME/media-lab-simple/studio-artifacts" \
  --runtime /absolute/path/to/pinned-runtime/bin/python \
  --package /absolute/path/to/pinned-package \
  --cache /absolute/path/to/model-cache
```

Qualification runs two bounded CPU jobs against the generated synthetic cup.
Both must pass decode/outline checks and produce identical alpha bytes. The
receipt retains artifact hashes and binds the host, interpreter binary, model
manifest, adapter sources, package/cache paths and canonical artifact root.
Activation rechecks retained PNGs, actual outline/mask evidence, pinned model
files and exact installed dependency versions in the target interpreter.
Changed evidence refuses activation. This is one technical fixture, not a broad
quality certification or a complete dependency-license audit.

Set `MEDIA_LAB_BACKGROUND_QUALIFICATION` in the app process environment to the
absolute `studio-artifacts/qualification.json` printed by that command. The app
uses its normal [FastAPI lifespan](https://fastapi.tiangolo.com/advanced/events/)
to start the host, and advertises the capability only while the verified worker
is alive. The artifact root must match the app's own `ROOT/studio-artifacts`.
Do not modify an enabled pinned environment in place; stop, install a new
isolated version and qualify it before switching.

With no receipt, a stale receipt, or
`MEDIA_LAB_DISABLE_BACKGROUND_WORKERS=1`, the capability stays unavailable.
Shutdown stops admission and waits up to630seconds for healthy current work;
it does not issue a job cancellation. A process supervisor that terminates the
app earlier can still interrupt it; the inherited process lock and durable
queue recovery handle an eventual restart. Removing the environment setting
and restarting disables the capability while retaining jobs and artifacts.

This opt-in path has been exercised through the actual app lifespan, gate,
Library input, job, content and shutdown routes using an isolated data directory
and real model. Production services were not modified. Public one-click setup,
Windows qualification remains separate work. Linux/Spark has passed two real
network-blocked CPU tracers and final host qualification in an isolated directory.
The actual app lifespan, scoped pairing, Library snapshot, job submission, content
retrieval and clean shutdown also passed on Spark with networking denied. A
restart recovered the same succeeded job without inference. This used ASGI
transport and a separate test data directory, not the deployed public endpoint;
physical Mac-off production use remains untested.

`remove_background(bytes)` accepts one PNG/JPEG/WebP, at most20MiB and16megapixels,
and returns PNG bytes plus a technical receipt. It preserves source transparency,
normalizes EXIF orientation and strips source metadata. It neither writes files
nor approves/publishes an artifact; the owning worker must validate and publish
the result atomically.

## Work required before release

- Broader background-removal quality fixtures and manual mask correction.
- Dependency/license audit and complete install/uninstall receipts.
- Windows, MPS and CUDA qualification; Linux evidence covers CPU execution only.
- Remote network transport, native lifecycle testing and retention UI.
- Public installer integration and broader end-to-end product validation.

The public setup catalog remains unchanged and does not silently make this
development package installable.

## Pinned development installation

`python -m media_lab_core.background_install --root /absolute/new/install-dir
--artifact-root /absolute/separate/artifacts --python /absolute/python3.12
--uv /absolute/uv` performs an explicit isolated development install. Run it
from the controller environment with Pillow and psutil installed. It requires
an existing Python 3.12 and uv; it does not install those tools or change services.

Stages are preflight, download-and-verify, runtime and qualification. A matching
`install.json` permits resume; unrelated directories and changed plans are
refused. Downloads use the platform manifest's exact sizes and hashes, publish
atomically, and reuse verified completed files. Interrupted partial transfers
are removed and retried from the start. Runtime installation uses the committed
hash lock, binary wheels and an isolated cache. An installation lock and the
canonical CPU worker slot prevent concurrent runtime mutation. Both real tracer
images and subsequent receipt verification must pass before status is qualified.
Rerunning a qualified install verifies its evidence and returns without inference.
Missing or changed qualification evidence never causes automatic runtime repair.

The 8 GiB free-disk check is a workspace reserve, not an exact download-size
estimate; qualification also requires 12 GiB available memory. Runtime errors
retain a private log in the installation directory. Install failure records its
stage and exception type. No activation or public catalog promotion occurs.
Fresh macOS arm64 and Linux aarch64 (Spark) installs and qualified replays have
been exercised. A receipt-write failure test confirms retry reuses completed
qualification, and missing qualified evidence refuses runtime mutation. Broader
interruption recovery, source-level dependency-license review, managed
disable/uninstall and setup UI integration remain required.

## Development removal and host lifetime

The updated host holds `.background-lifecycle.lock` for its whole active
lifetime, including idle time. Installation, qualification CLI and removal use
the same exclusion; runtime mutation/removal also requires the CPU job slot.
Maintenance refuses while a host or orphaned job is active. Stop the development
app normally first; these commands never kill or reconfigure a service.

`python -m media_lab_core.background_remove --root /absolute/install-dir`
validates the matching qualification and displays a removal plan. Add `--remove`
to remove only that install's package, runtime and two cache directories. It
retains all generated artifacts, qualification evidence, install receipts and
logs. An atomic `runtime-disabled.json` marker prevents host restart before any
deletes begin. Interrupted removal resumes from the matching journal without
requiring already deleted model files; repeated completed removal is harmless.

This has been exercised against the disposable Mac installation, including
SHA-256 verification of both retained outputs and a repeated removal. Existing
older controllers without this lifetime lock must not be used concurrently.
Run the same install command with `--reinstall` to restore a fully removed
runtime into the same installation and artifact roots. The removal marker must
match that installation, and interrupted removal must finish first. Retired
qualification receipts are kept under content-hashed names; all existing result
directories remain in place. The host stays disabled through downloads and
qualification. Only verified success clears the removal marker. Repeat the same
command to resume a failed reinstall. Setup UI integration remains work.

Each new install collects `runtime-inventory.json` from its exact target
interpreter before qualification, without importing model packages. It records
versions, package license metadata, project links and discovered LICENSE,
LICENCE, COPYING and NOTICE text with hashes. The install receipt binds the
inventory hash; changed evidence refuses replay. Older qualified installs can
collect a missing inventory on replay without new inference. The file is retained
through removal. This is a reproducible evidence inventory, not a license approval
or a guarantee that every bundled component's notices were discovered.

Linux/Spark removal and same-root reinstall have also passed against an isolated
install, retaining and verifying both old and new qualification outputs. Public
setup controls and complete dependency/source notices remain outstanding.

A supplemental, hash-verified tokenizers 0.22.2 upstream license is bundled under
`media_lab_core/data/runtime-notices`. The inventory attributes its exact source
commit and reports wheel-missing notices separately from unresolved notices.
Final scans of both qualified Mac and Spark runtimes found notice evidence for
all 31 pinned packages after this supplement. That does not certify completeness
of bundled native-library notices or finish the distribution review.

## Development Setup API

The independent setup backend is explicitly opt-in with
`MEDIA_LAB_BACKGROUND_SETUP=1`. Its three routes require a signed **admin**
session from the normal server login. The legacy `admin_guard` also accepts
ordinary signed-in users, so these new routes deliberately check the signed
admin role directly. Trusted-host access, Library tickets and render tickets do
not grant installation. No cross-origin installation bridge is added.

- `GET /api/setup/background/plan` returns resource requirements, model revision,
  model license, fixed installation/artifact paths, prerequisites and a plan ID.
- `GET /api/setup/background/status` distinguishes a live thread in this process
  from persisted stage/status. A stored qualification is not an enabled engine.
- `POST /api/setup/background/install` accepts only `planId` and optional boolean
  `reinstall`. Changed plans and duplicate active submissions are refused. The
  worker invokes the pinned installer; it does not activate the runtime.

Runtime folders are derived from the manifest/lock under
`ROOT/studio-runtimes`; artifacts stay in `ROOT/studio-artifacts`. The client
cannot supply shell commands, interpreter paths or download URLs. The plan binds
controller and notice-manifest hashes, model/lock hashes and selected tool bytes.
Server administrators may set `MEDIA_LAB_BACKGROUND_PYTHON` and
`MEDIA_LAB_BACKGROUND_UV`; defaults use the controller interpreter only when it
is Python 3.12 and discover uv on PATH. Runtime ABI is checked by the installer
before any model download. Setup UI, controlled enable/disable and full HTTP
installation integration tests remain before release.

The server page `/setup/background` presents the admin login, resource review,
advanced version/path details, explicit install/reinstall action and live stage
polling. It uses same-origin admin cookies, clears the code field after login,
and never writes access codes into URLs or browser storage. The legacy setup
wizard links to it only when development setup is opted in. Its previous claim
that blocked LTX was the default was removed.

The actual page installed and qualified a fresh runtime through the real app's
HTTP routes in an isolated Mac data directory. Both tracer output hashes and the
notice inventory were verified afterward; reload restored the completed state.
The 390×844 layout was visually inspected. Enabling the qualified host is still
separate and its UI controls remain to be implemented.
