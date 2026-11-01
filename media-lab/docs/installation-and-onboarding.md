# Installation and onboarding product specification

## Current read-only capability planning

The example flow below is a product target, not a list of installed or qualified
engines. To inspect the checked-in catalog today, run from the Media Lab checkout:

```sh
python -m media_lab_core.setup_wizard --list
```

To choose available qualified entries interactively and collect resource facts:

```sh
python -m media_lab_core.setup_wizard --inspect-host --storage-root /path/to/model-storage --output-plan /path/to/install-plan.json
```

Replace the paths with your intended model directory and plan output. The model
directory need not exist; inspection measures its nearest existing parent and
does not create it. Writing `--output-plan` creates only the requested plan file
and its parent directory. No models are downloaded, installed, or enabled.

Run this command **on the execution host**. For a Spark reached over SSH, open
an SSH session to that Spark, change to its Media Lab checkout, and run it there.
Running it on a laptop measures the laptop even if `--hardware-profile` names a
Spark; that option is a label, not remote discovery or hardware verification.

`host_resources` reports available RAM and free disk in bytes. Catalog GB values
use decimal GB. `catalog_resources_fit` checks those catalog estimates only;
`hardware_verified` remains false. Unknown estimates, no selection, or inadequate
resources produce `blocked_reasons`. GPU/driver support, installation scratch
space, concurrent workloads, model loading and tracer qualification are separate
checks. Recheck immediately before installation because resource availability
changes. Required third-party terms still need direct acceptance.

## Experience target

### Human terminal path

```text
$ media-lab setup

✓ DGX Spark detected: GB10 / 128 GB unified memory / CUDA 13
✓ 1.4 TB free in model storage

Choose video engines:
  [x] LTX-2.5 — Balanced speed; strong motion and cinematic continuity
  [x] H3 FL2VA — Slower; strong native audio and multimodal control
  [ ] H3 Ref2VA — Adds reference identity/style/video/audio weights

Choose image engines:
  [x] Qwen-Image-2.1 — the studio's only image model: generation, editing and transparency.
      Research-license weights (evaluation only; commercial use needs a separate Qwen license).

Choose speech/audio:
  [x] Local speech

Choose setup/chat models:
  [x] Qwen 27B NVFP4 — Recommended fastest promoted 27B Spark profile
  [ ] Qwen 27B alternate variant — Similar class; separate behavior/safety evaluation

Download: … GB  |  Maximum measured residency: … GB  |  Runtime swaps: video, image, LLM
```

Every displayed number comes from the signed catalog and qualification receipts. Unknown values block selection rather than rendering as estimates.

### Agent-driven path

The user gives the repository URL to an agent. The agent reads `docs/external-agent-setup.md`, runs machine inspection and the same deterministic planner, shows the proposed plan, gathers direct user acceptance for gated terms, executes resumable stages, and reports the setup receipt. Agent-driven installation and terminal installation invoke the same APIs and state machine.

## Capability catalog

Each option contains:

- stable capability ID and display name;
- category and supported generation modes;
- concise speed, quality, and best-use summary;
- exact platform/hardware compatibility;
- measured disk bytes and phase-specific memory floor;
- dependencies and mutually resident/swap group;
- exact source, immutable revision, artifact SHA-256;
- code and weight license identifiers and terms URL;
- gated-access/acceptance state;
- qualification status and receipt revision;
- installer adapter and uninstall behavior.

Only `qualified` entries with complete immutable metadata become selectable. Experimental/planned/blocked entries can appear under an advanced transparency view but cannot install.

## Packs without hiding individual choices

Offer one-click presets as selections over the same catalog:

- **Starter Studio:** one video engine, recommended image engine, local speech, setup LLM.
- **Video Studio:** LTX plus H3 FL2VA; optional Ref2VA.
- **Full Studio:** every qualified media capability that fits the machine.
- **Custom:** individual selections.

Presets never override license gates, hardware fit, or swap constraints.

## Deterministic installer stages

1. **Bootstrap:** verify supported Python/OS and run a signed or checksummed installer entrypoint.
2. **Inspect:** record hardware, driver/CUDA, memory, disk, container runtime, ports, and permissions.
3. **Plan:** resolve dependencies, download bytes, runtime swap groups, and measured phase floors.
4. **Terms:** present every distinct code/model license and gated source; record consent locally without committing protected receipts.
5. **Fetch:** resumable downloads into staging with bounded retries.
6. **Verify:** expected bytes, SHA-256, SafeTensor metadata, archive path containment, and source revision.
7. **Install:** atomic move into a content-addressed model store; isolated adapter/runtime installation.
8. **Preflight:** architecture, package/ABI, kernel backend, writable persistent caches, ports, and memory admission.
9. **Qualify:** deterministic minimal tracer for each selected backend under the shared GPU scheduler.
10. **Register:** enable only exact manifest matches; otherwise leave disabled with a repair receipt.
11. **Launch:** app liveness/readiness plus capability health.
12. **Onboard:** deterministic questions first; optional local Qwen conversation for preferences and explanations.
13. **Receipt:** installed/skipped/blocked capabilities and rollback/uninstall instructions.

Every stage is idempotent and resumable. Interrupting a download or qualification run does not corrupt the prior working setup.

## Local Qwen onboarding

The local model is an enhancement, not a bootstrap dependency. The app can open with a setup chat after deterministic installation:

> Your core studio is working. I have a few preferences to finish setup: Which video engine should be the default? Do you prefer faster previews or maximum quality? What output orientation do you use most? How long should Media Lab retain source files and failed candidates?

The model receives a typed setup schema and produces a proposed patch. The deterministic controller validates it and shows the user the resulting actions before application. Free-form model output never directly executes shell commands or mutates services.

If Qwen cannot load, the same questions render as a standard form. Setup remains fully usable.

## Model swapping

Installing both models does not imply simultaneous residency. The catalog declares swap groups such as `video-engine`, `image-engine`, and `llm-residency`. The lifecycle manager:

1. inspects the active owner;
2. acquires the canonical GPU lease;
3. drains the exact service;
4. stops only that owner;
5. starts the requested immutable manifest;
6. verifies actual identity/readiness;
7. runs or queues work;
8. restores the prior desired state when policy requires it.

The UI says **Installed** separately from **Loaded now**.

## Release acceptance criteria

- Clean supported machine reaches the app from one documented command.
- Human and agent paths produce the same deterministic install plan.
- All downloads are revision- and hash-pinned.
- Interrupted setup resumes safely.
- No agent or LLM can accept terms for the user.
- Unqualified/unsupported options fail before download or GPU work.
- Both-engine installs can swap repeatedly without stale residency or lock leakage.
- The app works without the optional setup LLM.
- Every selected engine completes its tracer and reports its actual manifest.
- Uninstall removes application/runtime state selected by the user without deleting shared models or user media unexpectedly.


### Agent-readable planning

`tools/media-lab setup --list --json` (or installed `media-lab setup`) emits the complete catalog,
including blocked entries and their refusal reasons. Use `--json --select MODEL_ID`
to produce a plan without interactive questions. With no selection, JSON mode
returns an empty plan whose `next_stage` is `choose-capabilities`; it never
chooses a model on the user's behalf. `--inspect-host` and `--storage-root PATH`
can be combined with JSON mode to inspect this execution host.

Successful JSON output contains no explanatory text, including when
`--output-plan PATH` also saves the same plan. Catalog/selection/resource failures
caught by the planner return exit code 2 and an `error` JSON object on stderr.
Argument-parser usage errors retain standard argparse behavior. This command
only plans: it does not download, accept terms, or register an engine.


### Inspect an existing independent host

`tools/media-lab studio inspect /absolute/host/root` validates the private
credential file, required data directories, and bounded Library catalog without
starting a process, installing an engine, or printing credentials. It emits
JSON with `configuration_valid: true` on success. `running` and
`engines_qualified` remain null because file inspection cannot establish either.
Invalid configuration returns exit 2 and a JSON error category. This uses the
independent host's existing POSIX credential-storage requirements; it does not
qualify Windows credential storage or migrate a legacy Media Lab directory.


### Independent controller source package

`python -m media_lab_core.studio_source_bundle --output /new/path/controller.zip`
creates a deterministic ZIP containing the explicit independent controller
module list, required catalog metadata, and Apache license. It refuses to
replace an existing output and records per-file SHA-256 hashes in `manifest.json`.
It excludes the legacy `app.py`, `runner`, static website, and deployment config.

This is a source-only development artifact, not an installer: Python and the
controller's dependencies must already be installed. It includes no models,
engine runtime wheels, credential files, or dependency-license approval. The
packaging test starts the extracted host in a fresh isolated Python process,
pairs a synthetic device, and verifies that no engines are silently enabled.
The desktop staging/launcher has not yet been switched to this package.


The development controller dependency snapshot for macOS ARM64 / Python 3.14
is `media_lab_core/data/controller-macos-arm64.requirements.lock`. Its 15 packages
were installed with required hashes and binary wheels into a fresh environment;
the extracted source package then paired over loopback HTTP, returned an empty
engine list, and had no legacy chat route. This is separate from model runtime
installation and does not qualify other Python versions/platforms or establish
complete third-party redistribution notices. Do not use legacy
`requirements.txt` as an independent controller dependency lock.

### Independent controller: Linux ARM64 development check

`media_lab_core/data/controller-linux-arm64.requirements.lock` pins the 15 base controller dependencies with hashes for Python 3.12. An isolated Spark environment running Python 3.12.3 passed a binary-only, hash-required installation and dependency check. The separately extracted independent source bundle served loopback HTTP, paired a test device, returned an empty engine list, and rejected the legacy chat route with 404. The test server was stopped afterward.

This qualifies that development controller check only. It does not establish model execution, production domain routing, physical Mac-off operation, native installer integration, or third-party dependency redistribution closure.

### Packaged desktop browser origin

The independent controller accepts `--origin tauri://localhost` as an explicit allowed origin for the packaged desktop webview. The packaged webview's origin differs by platform -- `tauri://localhost` on macOS and Linux, `http://tauri.localhost` on Windows -- so allowing either one also allows its platform twin; a host configured only for the macOS/Linux origin cannot be paired from Windows at all. The default allowed-origin list remains empty. Only this exact custom origin is accepted; arbitrary custom schemes, paths, ports and opaque `null` origins are refused. CORS permission does not replace the access code or scoped token checks. HTTP(S) origins still require their exact explicit entries. Previously staged installations must be rebuilt/reinstalled to include this change; desktop startup and in-app local pairing are not wired to enable it automatically yet.


### Import your existing media into an independent host

Run on the machine owning the host:

```sh
python -m media_lab_core.studio_cli import /absolute/host/root /path/to/scene.mp4 --title "Opening scene"
```

This copies one supported image, video or audio file (up to256 MiB) into the
host's private media directory and registers it in Library. FFprobe must be
installed. The original remains unchanged. Repeating the same file and file
type returns its existing asset ID without creating another Library entry.
The JSON receipt includes its ID, byte count and SHA256. Paired clients can
read it from Library and select it for editing. This does not upload files to
a central service or grant an external agent access to the host. Browser file
upload is a separate flow and is not implemented by this command.
