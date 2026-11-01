# Unified local GPU queue and transition rollout

Status: implementation candidate only. Not deployed. No live service, kernel,
bootloader, cloud billing, or public endpoint was changed by this work.

## Root cause and protocol

The controller previously had two advisory lock families and several direct
starts. `/run/user/1000/spark-gpu.lock` is normally held by the pool reservation;
`/run/user/1000/media-lab-inference.lock` serializes selected render calls. Neither
lock carried a durable owner/fence or proved process and memory reclamation before
another engine was admitted. A client timeout could release a render lock while
unknown engine work survived.

`media_lab_core/durable_gpu_protocol.py` adds a stdlib-only protocol that can be
shared by controller, shims, recovery tools and shell launchers:

* SQLite WAL state plus the canonical POSIX flock;
* monotonic fencing tokens and exact owner/job/engine/task identity;
* fail-closed task-specific peak + OS reserve admission;
* drain -> unload -> reclaim -> load -> render -> unload -> reclaim phases;
* exact-fence release and delegated engine-request validation;
* a durable recovery state that stale owners, late callbacks and reboot recovery
  cannot clear;
* idempotent local/cloud job records, durable cancellation and fenced claims;
* local and cloud lanes claim independently; cloud work never takes the local
  GPU lease.

`tests/test_durable_gpu_protocol.py` covers mixed
T2VA -> FL2VA -> Ref2VA -> LTX transitions, duplicate submission, cancellation,
cloud progress under a local hold, stale job callbacks, crashed process recovery,
stale release, capacity refusal and uncertain-transition quarantine. These are
controller tests only; they are not hardware qualification.

No peak is inferred from a model name. Every `(engine, task)` must have a measured
peak, explicit reserve and evidence receipt before admission. In particular,
T2VA results do not qualify FL2VA or Ref2VA.

## Actual admission/bypass inventory

All of these must be migrated before enforcement is activated:

1. `app.py`: local and online queue workers; `ensure_engine`; `engine_generate`;
   image, video, music, TTS, face, background, lipsync and direct engine routes.
   Raw inference-lock use remains at the current call sites around lines 2272,
   2515, 3566, 3796, 6080, 6104, 6231, 6410, 7606 and 10800.
2. Boot/autoload: `runner/sol_engine_server.py` (`SOL_PRELOAD`), transient
   `media-lab-sol-h3.service`, `media-lab-yue2.service`, image/video resident
   hosts and any systemd unit that starts a GPU process.
3. Docker/direct starts: `runner/start_h3_engine.sh`, `start_ltx_engine.sh`,
   `run_lab_render.sh`, `run_image_render.sh`, `run_music_render.sh`,
   `run_tts_speak.sh`, `hunyuan_avatar_video.sh`,
   `latentsync_video.sh` and `musetalk_video.sh`.
4. Python-side workers: `media_lab_core/image_host.py`, `video_host.py`,
   `music_worker.py`, `background_remove.py`, `background_install.py`,
   `director_adapter.py`, plus `runner/yue2_engine_server.py` and
   `text_runtime_switch.py`.
5. Pool ownership: `runner/pool_lock.sh` and the existing
   `media-lab-gpu-reservation.service`. This reservation cannot coexist as an
   unrelated permanent holder when the new canonical flock is activated.
6. CLI: any direct Docker/systemctl invocation outside the wrappers remains an
   OS-user bypass. Cooperative locking cannot stop an unrestricted shell; service
   hardening/policy is the enforcement boundary.

The new protocol is intentionally not wired into only a subset of these paths.
Partial activation would create a third lock and a false safety claim. Activation
is one reviewed cutover after adapters for every listed route are present.

## Sol-H3 source and terms reconciliation

Live read-only evidence on 2026-09-16:

* deployed VibeXStudio source is tag `deploy-20260915-1354`, commit
  `e9e033f6e62b3aed477213b515180150244349da`;
* live `SOL_PKG` resolves beneath the service user's home as
  `$HOME/src/Sana-sparse/models/minimax_h3/Sol-H3-Spark` at
  `8e0db4fa562d727ea28b8d63015c196db7d97cae`, clean in the read-only check;
* upstream `sol-engine` moved to `bb60499af0e675095ff67424196d8c18e265f32a`
  on 2026-09-16; that commit's parent is the installed `8e0db4f` revision.
  The newer commit adds opt-in LoRA branch fusion and is not silently adopted.

Official upstream evidence says Sol-H3-Spark is a two-stage 384p draft to
1344x768/121-frame/24-fps refinement pipeline. T2VA has bounded resident testing;
FL2VA and Ref2VA have bounded functional cases. Input-conditioned tasks require
an H3 VAE encoder and extra reference memory not covered by T2VA residency.
Task families are separate invocations/partitions, not mid-request swaps.

Terms remain component-specific. GitHub reports the repository default branch as
Apache-2.0, but the `sol-engine` branch root listing does not contain a `LICENSE`
file; do not infer that this relicenses bundled components or weights. Its upstream
notice identifies Apache-2.0 FastVideo, BSD-3-Clause FlashAttention 4, separately
installed GPL-3.0 ComfyUI, LTX community terms, MiniMax-H3 model-specific terms,
gated LTX-2.5 terms, and components whose selected revisions declare no
redistribution license. The integration repository does not relicense models or
dependencies. Existing private/internal H3 authority remains the governing gate;
this work does not extend it to public, customer, redistributed,
production-external, or actor-voice-cloning use.

Sources:

* https://github.com/NVlabs/Sana/blob/sol-engine/models/minimax_h3/Sol-H3-Spark/README.md
* https://github.com/NVlabs/Sana/blob/sol-engine/models/minimax_h3/Sol-H3-Spark/docs/validation.md
* https://github.com/NVlabs/Sana/blob/sol-engine/models/minimax_h3/Sol-H3-Spark/THIRD_PARTY_NOTICES.md

## Kernel/RDMA compatibility gate

`media_lab_core/kernel_compat.py` and `tools/kernel-rdma-gate.py` block
multi-node/RoCE qualification on `7.0.0-1019-nvidia` and when no RDMA link is
present. They explicitly do not attribute single-node H3 allocation failures to
this advisory.

Read-only live evidence on 2026-09-16:

* Spark 1: `6.17.0-1029-nvidia`, 4 KiB pages, no `rdma link`; it still logged
  a same-boot `NV_ERR_NO_MEMORY`, so the RoCE thread does not explain that fault.
* Spark 2: `7.0.0-1019-nvidia`, 4 KiB pages, no `rdma link`; two-Spark
  NCCL/RoCE qualification is blocked.
* both apt caches offer, but have not installed,
  `linux-image-6.17.0-1032-nvidia`, `linux-modules-6.17.0-1032-nvidia` and
  `linux-headers-6.17.0-1032-nvidia`, version `6.17.0-1032.32`.

The NVIDIA forum report records repeated `ibv_reg_mr_iova2 ENOMEM` on 1019 and
healthy 1032 A/B outcomes for two-node RoCE. It is relevant evidence, not proof
for this no-RDMA single-node H3 incident:
https://forums.developer.nvidia.com/t/383023

## Exact gated activation packet

Do not run this packet piecemeal. It requires one specific approval after code
review, route migration and capacity receipts.

1. Preserve current boot/package/GRUB/service/container/memory receipts on both
   Sparks and export the current boot entry names.
2. Install, without removing the currently bootable kernels, exact arm64 packages
   `linux-image-6.17.0-1032-nvidia=6.17.0-1032.32`,
   `linux-modules-6.17.0-1032-nvidia=6.17.0-1032.32`, and
   `linux-headers-6.17.0-1032-nvidia=6.17.0-1032.32`; install the exact matching
   NVIDIA open module package selected by the reviewed installed driver branch.
   Do not move the HWE meta-package until that dependency choice is reviewed.
3. Verify initrd and GRUB entries. Set a one-boot saved entry for 1032; do not
   delete or overwrite Spark 1's 1029 or Spark 2's 1019 fallback.
4. Drain/checkpoint supported Media Lab work, preserve queue state, stop only the
   named services in the approved packet, then reboot one Spark at a time.
5. After each boot verify exact kernel, driver/GPU health, network, services,
   queue, lock state and rollback boot entry before touching the other Spark.
6. Roll back by selecting the recorded prior GRUB entry and rebooting; do not
   purge 1032 during emergency rollback.
7. Only after both hosts have an actual RDMA link and 1032 is stable, run an
   isolated two-node NCCL/RoCE tracer. No model or paid-provider workload is part
   of the kernel tracer.
8. Separately stage the unified queue cutover, import measured per-task capacity
   receipts, restart only reviewed Media Lab units, run CPU/synthetic proofs,
   then one approved local canary per engine family. Preserve the current H3
   batch; do not cold-load while memory pressure is high.
9. A paid cloud overlap canary requires explicit provider, model and spend
   approval. Public endpoint mutation and deployment remain separate approvals.

## Rollback of the queue candidate

No live rollback is currently needed because nothing was deployed. For a future
approved tagged deployment, use the deploy road's generated backup path and tag;
do not edit live `app.py`. If the durable DB cannot be reconciled, stop admission,
retain its recovery row and restore the prior tagged release. Never delete a
lease/recovery database as a recovery shortcut.

## Ownership adapter for the legacy pool shim (2026-09-21)

Once the durable controller owns `/run/user/1000/spark-gpu.lock` itself, the
legacy residency handshake has to agree with that owner instead of fighting it.
It did not: on the media Spark the controller held the lock for a fenced image
operation, `runner/pool_lock.sh acquire` matched no managing unit and no stale
`flock ... sleep infinity` holder and answered `BUSY`, and the shim in front of
the image engine turned that into

```
HTTP 503 {"error": "gpu reserved elsewhere", "detail": "pool_lock reports an external holder"}
```

on an idle box — every image job waited at "waiting for the camera crew" while
nothing rendered. The "external holder" was media-lab-simple itself.

The adapter is `runner/lease_owner_probe.py` plus one call site in
`runner/pool_lock.sh`:

* `acquire` grants `OK` when the live controller lease owns the lock (exclusion
  is genuinely held, and the pool unit could not take the flock anyway);
* `release` becomes a no-op in that case, so the idle reservation is never
  started against a lock we already hold (it would restart-loop);
* everything else keeps the pre-existing behaviour, including `BUSY` for an
  outside production batch, a stale/duplicate holder, a dead holder, a
  `gpu-recovery-hold.json` (unresolved outcome), or unreadable evidence.

The probe is read-only: it never takes, releases, starts, stops or kills
anything, and it grants nothing that memory/capacity admission does not already
grant. Ownership proof is layered: lock present and not free; exactly one
exclusive FLOCK holder read from `/proc/locks`; holder alive and inside the
`media-lab-simple.service` cgroup; command line matches the controller when it
is readable; no unresolved-outcome marker.

Note this does not remove the older holders: `media-lab-pool.service` (warm
engines) and `media-lab-gpu-reservation.service` (cold idle) stay authoritative
whenever the controller is not the holder, and `handoff` still transfers the
lock to the controller before a durable acquire.

