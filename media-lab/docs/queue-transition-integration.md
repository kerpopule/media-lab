# Queue transition integration: staged partial implementation

This is NOT a release qualification or an authoritative cross-process GPU lease.
Do not deploy this branch as a completed solution to mixed local/cloud scheduling.
No engines, cloud requests, production queues or services were changed by these tests.

## Lineage

Base: canonical VibeXStudio main `e9e033f`.
Consumed incident lineage through `e214db48a888fda87dd782bda1ef2e2c7631f0b5`
and retry/storyboard lineage through `da8fdbeb368f528f0fb4c566942aabba4276a6ac`.
The source commits were cherry-picked, not copied over another owner's worktree.
Incident approval applies to its staged patch, not this integrated branch.

## Executed improvements

* A transport timeout/disconnect is an unknown remote outcome, not proof of an
  exited engine. The controller no longer restarts/reissues such a request.
* A GPU recovery marker is exclusively created and fsynced before releasing the
  render transaction lock. The first receipt is retained; an empty/partial marker
  is blocking too. Job retry classification preserves `recovery_required`.
* Controller admission, variant switching, engine boot, residency transaction
  entry, idle restoration and startup recovery refuse unresolved GPU ownership.
  The local worker retains queued rows while held; the existing cloud lane runs
  separately. This does not grant provider authorization or create a paid fallback.
* Exact service/container shutdown commands are bounded. Success additionally
  requires inactive/failed unit with MainPID zero and old/new cgroups removed or
  unpopulated, or a successful Docker inventory excluding the removed container.
  Unavailable inspection, timeout or surviving processes latches recovery.
* Stopping no longer resets the unit's failure evidence. Best-effort Docker logs
  are bounded and retained before the verified stop.

The marker has deliberately NO automatic clearance. A late callback, controller
restart, reboot, healthy HTTP result or cancellation is not clearance authority.
A reviewed recovery flow must reconcile exact processes, containers, children,
boot identity and memory before clearing both the marker and affected job flags.
The in-process latch also remains set for the life of that controller process.

## Actual source admission inventory / remaining bypasses

| Route | Current evidence and remaining work |
|---|---|
| UI/API local queue -> `run_queued_job` | In-process idle mutex, JSON jobs and recovery hold. Not a durable transactional claim with owner epoch. |
| `ensure_engine` / `stand_down_other_companions` | Eviction begins before the inference-lock scope. Health-only `engine_up` can omit an unhealthy surviving owner. New stop verification helps only when stop is actually selected. |
| `engine_generate` | Controller takes inference lock around HTTP; marker now written before release on uncertain transport. Other processes do not yet all consult it. |
| `_ResidencyRuntime` | Takes inference lock, but health-based snapshot still conflates reachability/residency and local/remote text placement. Integrate process/cgroup inventory and explicit remote Spark2 text topology. |
| Sol HTTP direct request | Reviewed incident patch uses process-local request/load locks and boot-bound safety gate; no controller lease delegation/fencing protocol. |
| Sol boot preload | `SOL_PRELOAD` defaults to variant load. Empty string falls back to preload. Direct service launch is not covered by the controller's new marker. |
| YuE2 shim/preload | Runtime contains inference-lock helper, but preload calls `ensure_pipeline` from a background thread. Comment contracts disagree on caller vs shim lock ownership; audit both before changing either. |
| LTX Docker startup | Container model allocation and direct engine HTTP admission require the same lease protocol, not only a lock held by the HTTP client. |
| Image service / direct ComfyUI | Controller/HTTP paths and standalone ComfyUI startup are separate admission surfaces; no claim that controller guard secures direct ports. |
| Music/image/lab shell runners | `run_music_render.sh`, `run_image_render.sh`, `run_lab_render.sh` use pool reservation/flock lifecycle, distinct from inference transactions. Must migrate explicitly. |
| Avatar/MuseTalk/LatentSync scripts | Their shell inference flocks do not implement durable ownership fencing or restart reconciliation. |
| Cloud queue | Existing separate worker; CPU fixture proves it advances while local hold exists. Provider wait must stay off GPU lease; any local GPU postprocess needs separate admission. |
| Raw Docker/CLI | A cooperative lease cannot stop an unrestricted runtime owner bypassing it. Managed launch paths and their authorization must be enumerated/controlled; do not claim OS isolation. |

## Durable protocol candidate

`media_lab_core/durable_gpu_protocol.py` now supplies the fenced SQLite + flock
owner, strict transition phases, fail-closed measured-capacity admission,
idempotent durable local/cloud jobs, cancellation and restart reconciliation.
`tests/test_durable_gpu_protocol.py` exercises the mixed
T2VA -> FL2VA -> Ref2VA -> LTX sequence and the fault matrix. See
[`gpu-queue-rollout.md`](gpu-queue-rollout.md) for the complete route inventory,
source/terms evidence, kernel/RDMA gate and exact gated activation/rollback
packet.

This is still not activated. Wiring only the app while resident servers and shell
launchers retain their own lock behavior could deadlock or create a third advisory
lock. All listed paths and the pool reservation must move in one reviewed cutover.

## Open acceptance (not implemented or not proven)

1. Wire the tested owner/fence through every route above, including delegated
   engine ownership after HTTP timeout, and remove the replaced lock paths in the
   same cutover.
2. Import measured cold/warm peaks plus OS reserve for T2VA, FL2VA, Ref2VA
   reference budgets and every additional admitted local engine. The protocol
   refuses unqualified pairs; current static GB estimates are not receipts.
3. Run the synthetic suite against the final adapters, then perform the separately
   approved paid-provider overlap canary if one is required. CPU tests already
   prove cloud queue progress while a local recovery hold exists.
4. Reviewed live qualification on a safely quiesced host, exact boot/process/memory
   receipts, output decode/QA and restoration. No mid-denoise resume is promised.

## Public implementation comparison (read-only research)

* NVIDIA Sol-H3 Spark README: two stages, resident prompt worker, 1344x768/121
  frames at 24 fps. Requires full GB10 memory availability. FL2VA adds native
  input encoders; Ref2VA uses separate partition/LoRA. Warm sessions and task
  partition changes do not establish safe mixed-engine controller scheduling.
  https://github.com/NVlabs/Sana/blob/sol-engine/models/minimax_h3/Sol-H3-Spark/README.md
* NVIDIA validation: bounded T2VA, FL2VA and selected Ref2VA functional runs.
  Explicitly says input/reference activations add memory; untested inputs cannot
  inherit text-only fit. No identity/voice or clean-environment guarantee.
  https://github.com/NVlabs/Sana/blob/sol-engine/models/minimax_h3/Sol-H3-Spark/docs/validation.md
* trahane wrapper pins Sana `8e0db4f`, reports 70.64 seconds warm Ref2V on one
  Spark. This is not cold-start, controller fencing or memory-admission evidence.
  https://github.com/trahane/dgx-spark-sol-h3-ref2v

No external installer/scripts or model downloads were executed.

## Deployment / rollback boundaries

This partial branch is NOT an activation candidate. Finish the open acceptance
and obtain independent integrated review first. Then publish a reviewed code-only
PR/tag using the canonical deploy road; never edit the live app in place. Exact
operator approval must identify preserved active work, admission/relaunch holds,
services allowed to stop/restart, qualification inputs and restoration target.
Do not use a live deploy dry-run as a read-only probe: it may stage remote files.
Rollback must retain recovery/incident latches, queue/checkpoint assets and evidence;
return to the previously recorded deployed tag without automatically resuming
unknown work. No reboot, global Docker/user-manager shutdown, paid cloud request,
endpoint change, actor voice cloning or public output is authorized by this document.
