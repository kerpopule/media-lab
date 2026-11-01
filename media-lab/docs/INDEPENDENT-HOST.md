# Independent development host

This starts the owned-job API, Library, and optional local Sparky controller
without importing the legacy app. It is a development entry point, not the
finished one-click installer or a qualified public deployment. Install this
repository's server dependencies in a separate Python environment first.

From the `media-lab` directory:

```sh
python -m media_lab_core.cli studio init /absolute/path/to/new-studio-data
python -m media_lab_core.cli studio serve /absolute/path/to/new-studio-data
```

The default listener is `127.0.0.1:7864`. `init` refuses an existing directory and
creates private credentials plus empty `media`, `artifacts`, `state`, and Library
catalog storage. Restarting `serve` uses those same credentials and saved jobs.
To display the pairing code deliberately in your terminal:

```sh
python -m media_lab_core.cli studio pair /absolute/path/to/new-studio-data
```

For browser access, add `--origin` with the exact app origin, including its port.
Repeat it for additional origins. No browser origin is allowed by default.
`--bind` and `--port` are explicit networking choices; this command does not
configure TLS, domains, tunnels, firewall rules or system services.

The Library reads `library.json` under this data directory and serves only files
inside its `media` directory. An empty catalog is valid. This command does not
copy or discover an existing production gallery automatically.

No engine or director is enabled by default. `--background-receipt` accepts an
existing absolute qualification-receipt path; its artifact root must match this
host's `artifacts` directory. It does not install or qualify a model.

## Administrator model setup (background removal)

Model installation is a separate, explicitly enabled administrator capability.
Device pairing codes, Library tickets and render or edit tokens never grant it.

```sh
python -m media_lab_core.cli studio admin-init /absolute/path/to/new-studio-data
python -m media_lab_core.cli studio serve /absolute/path/to/new-studio-data \
  --model-setup --setup-python /path/to/python3.12 --setup-uv /path/to/uv
```

`admin-init` prints a 32-byte administrator code once and stores only a salted
PBKDF2 hash in a private `admin.json` beside the pairing credentials. Use
`admin-init --rotate` to replace the code, and `admin-revoke` to end every
administrator session without changing the code. `serve --model-setup` refuses
to start without that enrollment and then:

- advertises `modelSetup: true` in `/manifest.json`, which is what the app's
  "Manage server models" card checks before linking to `/setup/background`;
- serves the setup page and signs the administrator in at `/api/setup/session`
  with an HttpOnly, SameSite=Strict, `/api/setup`-scoped cookie (Secure over
  HTTPS). Sessions live only in the server process, expire after eight hours or
  one idle hour, are bounded to ten sign-in attempts per ten minutes, and every
  write requires the page's same-origin request header plus a matching Origin;
- runs the deterministic plan → install → qualify → enable → disable → remove →
  reinstall controller against this host's fixed stores: runtimes under
  `runtimes/`, and qualification plus job outputs under the same `artifacts/`
  directory the job host serves. Generated creations are retained by removal;
- restores a saved "enabled" choice after restart only after the host
  re-verifies the qualification receipt. `--background-receipt` makes activation
  externally controlled, so the page cannot change it.

Only the BiRefNet CPU background-removal pack (macOS arm64, Linux aarch64) is
managed this way today. Weights are downloaded from the pinned manifest sources
under their own MIT terms. This is one capability's lifecycle, not a general
model manager or a GPU capacity planner.

To connect an already running, separately qualified local vLLM runtime, provide
all three options together: `--director-model` with its exact model ID,
`--director-port` with its canonical loopback port, and `--inference-lock` with
the absolute canonical lock shared by every GPU consumer and runtime switcher.
The host never selects an alias, swaps models or falls back to another provider.
It checks runtime identity and idle scheduler metrics before each request.

Graceful shutdown stops director admission and waits for active or uncertain
inference to drain. It can remain pending if the runtime cannot be verified idle.
A forced process kill cannot preserve the OS lock. Supervisor behavior, public
authentication, Windows credential storage and physical Mac-off acceptance still
need qualification before this is advertised as a supported deployment.

## Speech and 3D packs: on/off from the setup page

`serve --speech-config` and `serve --triposr-config` name operator-placed JSON
files for the reviewed Chatterbox and TripoSR packs. With `--model-setup`, the
administrator page lists both packs and can turn a configured pack on or off;
the choice is saved in `studio-packs.json` beside the host data and restored at
start, after the host re-verifies the pack's files. Turning a pack off waits for
healthy owned work to finish. Installing or removing these packs' files is not
offered yet: their runtimes need wheels that today exist only as local builds.

## Music pack: warm renderer

`serve --music-config <json>` runs ACE-Step from an operator config. By default the
host keeps one renderer process warm for ten minutes after a job (`resident_idle_seconds`,
optional, default 600; `0` starts a fresh process per job). The warm process is owned
exactly like a per-job one: the same memory and time budgets apply while it renders,
cancellation or any budget breach terminates it and drops the model, and it is reaped
after the idle period so GPU memory is not held indefinitely. The canonical inference
lock is still taken only for the duration of each render; between renders the warm
model occupies memory without the lock, so size `resident_idle_seconds` for hosts that
share the GPU with other engines. The manifest engine row reports `warm: true` while a
renderer is resident.

## Video pack (Wan2.2 TI2V-5B, GPU)

`serve --video-config <json>` serves experimental text-to-video. The config names the
pinned runtime interpreter and its SHA-256, the pinned Wan2.2 source checkout and the
commit it must be at, the checkpoint directory and its per-file manifest, the shared
inference lock and the exact revision; optional `resident_idle_seconds` (default 600)
and `memory_gib` (default 48) size the warm renderer. Results are gated as MP4 containers
with one H.264 track of a supported size and bounded length before download or Library
save (`Videos/Generated`). On hosts with unified GPU memory (DGX Spark) the renderer is
started with `THP_MEM_ALLOC_ENABLE=1`; without it CPU-side model loading is hundreds of
times slower. When the GPU lease or memory is busy the job returns to the queue untouched.

## Image pack (Qwen-Image-2.1, GPU)

`serve --image-config <json>` serves experimental text-to-image. The config names the pinned
runtime interpreter and its SHA-256, the checkpoint directory and its per-file manifest,
the shared inference lock and the exact revision; optional `resident_idle_seconds`
(default 600) and `memory_gib` (default 48). Results are gated as single-frame PNGs of a
supported size before download or Library save (`Images/Generated`). The pipeline is
loaded with `device_map='cuda'`; on unified-memory hosts a CPU load followed by a device
copy doubles the footprint and starves CUDA context creation.

Pinned runtime: `diffusers` at commit `80c7ed262aeffbeb43ef13ae04baeb9b84515a69`, the first
`main` commit that carries `QwenImage21Pipeline` (PyPI 0.40.0 predates it), with
`transformers>=5.17` and `torch 2.14.0+cu130`. Inference runs the reference DGX Spark image
lab settings, `true_cfg_scale=1.0` with `use_kv_cache=True`; `guidance_scale` is not a
parameter of this pipeline. On the media Spark the staged paths are
`~/runtime/qwen-image-21/venv` and `~/runtime/qwen-image-21/models/Qwen-Image-2.1`, verified
against `config/qwen-image-21-weights.json`. Do not enable `torch.compile` on this
transformer (upstream issue: Qwen Image 2.1 transformer is incompatible with it).

## Run it as a service (Linux systemd --user)

```sh
python -m media_lab_core.cli studio service install main \
  --root /absolute/path/to/new-studio-data --python /path/to/venv/bin/python \
  --port 7864 --origin https://your.host:8450 --model-setup \
  --setup-python /usr/bin/python3.12 --setup-uv ~/.local/bin/uv
python -m media_lab_core.cli studio service status main
python -m media_lab_core.cli studio service uninstall main
```

`install` checks that the interpreter can import the server dependencies,
writes `~/.config/systemd/user/vibex-studio-<name>.service` with exactly the
serve arguments given (marked as managed; it refuses to overwrite a unit it
did not write and keeps a dated copy when it rewrites its own), enables and
starts it, waits for `/manifest.json`, and reports whether lingering is on so
the studio survives logout. A unit that does not answer is stopped and
disabled again and the last log lines are shown. `uninstall` removes only the
unit; data, credentials, Library and models stay. macOS and Windows are refused
with a plain message; run `serve` yourself there.

## Always-on server acceptance

The server owns the data directory, credentials, model runtimes, inference lock,
and network tunnel. A desktop or phone is a client; closing that client must not
stop a job or remove its output. A tunnel pointing at a desktop proxy does not
meet this requirement, even when the model itself runs on another machine.

Before enabling a supervised deployment, verify these separately:

1. The tunnel and application run on the intended always-on machine, and the
   tunnel's upstream resolves to that machine without a desktop relay.
2. The service survives logout and restarts after a host reboot with the same
   private data directory. Pairing and completed owned jobs remain available.
3. Graceful stop during inference preserves the canonical lease until the runtime
   is confirmed idle. Repeat with temporarily unavailable runtime metrics.
   A supervisor's default stop timeout followed by forced termination is not an
   acceptable substitute for this test. Do not reuse legacy service settings
   without verifying this behavior.
4. With the desktop physically off, a separate phone can pair, submit a supported
   job, reconnect, retrieve its result, and use it in a project. Record the exact
   engine and client tested; routing inspection alone is insufficient.
5. Restart and rollback preserve credentials, owned job records and artifacts.
   Keep the existing host available until the replacement passes acceptance.

These are deployment acceptance requirements, not an instruction to change
public routing or enable a production service. The foreground development host
does not yet provide a qualified service installer.

`tests/test_director_process.py` exercises a real Uvicorn subprocess over HTTP
with a synthetic inference transport. On macOS ARM64, SIGTERM was observed by
the server while an active request or unavailable idle probe retained the lease;
recovery allowed graceful exit and another process started on the same data
root. This does not establish systemd behavior, reboot persistence, real model
recovery, or Mac-off client acceptance. The test uses its own temporary lock and
never signals an existing inference service.

A separate Linux ARM64 Spark experiment also passed a real `systemctl restart`
under systemd 255, using a disposable transient user unit and synthetic
transport. With `TimeoutStopSec=infinity`, `SendSIGKILL=no`,
`KillMode=control-group` and `Restart=on-failure`, the old process retained its
lease in `stop-sigterm` while the probe was unavailable. After recovery, a new
process served HTTP from the same data root; final stop left no service process.
These settings deliberately allow an indefinite pending stop when runtime idle
cannot be established. They do not protect against crashes, OOM or host power
loss, and do not constitute a qualified production installer. Reboot persistence,
real model recovery and end-to-end client acceptance remain outstanding.
