# Sol H3 loader safety

The wrapper serializes preload, family switching, and generation with one reentrant lock. A cold preload must not race a generate request into a second heavyweight pipeline. Health marks startup as `loading=true,busy=true` rather than implying inference readiness.

`Pipeline.finish()` is report finalization, not teardown. It can reject a warm-only or failed batch. Teardown must still call `close()` and verify the known worker processes and their process groups are gone before loading another family. Unknown or surviving workers fail closed; no broad process killing is used by the wrapper's verification.

## Sticky failure boundary

A transition or generation exception sets an in-process block and atomically writes `safety-stop.json` beneath the configured `SOL_ROOT`. A subsequent process also honors this marker. The wrapper additionally honors the existing `flashnext-memwatch.latch` under `XDG_RUNTIME_DIR` (legacy watchdog compatibility). Health returns 503 with `blocked=true` while latched. New allocation is refused, but existing cached successful output can still be returned.

The marker records only reason, exception type, PID, task family, and timestamp. It does not contain a prompt or credentials. Failure to persist is logged and still blocks the running process; a filesystem failure or abrupt kill before a marker is written is not durable recovery proof.

The memory watchdog retains its marker but can stop a newly active service under pressure again. A stale marker no longer makes it ignore a controller relaunch. This is a last-resort stop, not an admission allocator.

## Boot quarantine

The wrapper also requires an operator-issued `boot-clearance.json` under `SOL_ROOT`, with `approved: true` and the exact current Linux `boot_id`. No clearance, malformed clearance, or a reboot denies new allocation. Clearance never overrides a sticky stop. See [boot-safe recovery](boot-safe-recovery.md) for the system-level rescue boundary, synthetic supervisor policy, limitations and approval gates. Since 2026-09-27 (owner decision) the token is issued automatically after a reboot by `runner/hold_autorecover.py` when `MEDIA_LAB_BOOT_AUTOCLEAR=1`, only after it has preserved the previous boot's evidence and checked memory, GPU, this boot's kernel log, the guard, and that no latch or safety stop is from this boot; otherwise it gives up with a reason and the health watch alerts. An operator can still issue it by hand (`spark1-clear-h3`).

## Operator maintenance hold

The app's existing `.engine-maintenance` marker now also gates `auto_requeue()` on every pass. While present, automatic retries leave job status, retry counters and both queues unchanged; the existing idle-residency restoration is held too. This is not a cancellation and does not pause manually submitted or already queued work. Once this source is loaded, marker changes are observed without another app restart. On older source the marker gates only idle restoration, not automatic retries. Preserve and clear it only under operator-approved recovery.

## Recovery requires an operator gate

Two narrow exceptions were approved by the studio owner on 2026-09-26 and run
only with `MEDIA_LAB_HOLD_AUTORECOVER=1`, through `runner/hold_autorecover.py`
(which uses the same `tools/reconcile-gpu-recovery.py`): the harmless startup
restart hold, and a control-plane guard pressure trip that happened during an
H3 cold load (lease still in phase `load`, the load record says `guard-lost`,
the job failed with no output, memory and PSI back to normal, no Xid / hung
task / OOM kill in the kernel log since, at most once a day). It preserves the
markers and incident in `.backups/autorecover-trip-*` before setting them
aside, and puts them back if the reconcile fails. Everything else below still
needs a person.

Do not clear a marker merely because a TCP socket opens, systemd reports active, or MemAvailable momentarily rises. Preserve journal, worker diagnostics, memory/pressure samples and queue checkpoints first. Quiesce approved work through the controller. Verify worker groups are gone, driver allocation/hung-task errors are not continuing, and the exact task-family phase budget fits with the approved operating margin. Preserve markers before approved clearing; never delete jobs or media to recover memory.

The app owns the cross-process inference transaction and pool lease. The reentrant lock here is only intra-process protection, not a replacement for those locks or protection against unmanaged GPU consumers. Do not acquire the app-held inference lock a second time inside this wrapper.

No automatic canary or job resubmission is part of this change. Memory estimates and the OS reserve are intentionally unchanged: only an approved, instrumented single-load qualification can establish whether a larger reserve and revised phase estimates fit the hardware. Controller retry policy and reliable terminal-state notification remain separate responsibilities.

## Fixture verification (no GPU/runtime import)

Run from `media-lab` with the project's test dependencies installed:

    python -m pytest -q tests/test_sol_engine_safety.py tests/test_memavail_watchdog.py tests/test_residency.py tests/test_residency_runtime.py tests/test_h3_timeout_recovery.py

Fake pipelines reproduce concurrent construction, finish-before-close leakage, partial-start cleanup, surviving worker/group rejection, sticky retry blocking across fresh process state, loading/blocked health, and generation failure. Watchdog tests execute the real shell script against stub commands; no real systemctl call or GPU load occurs.
