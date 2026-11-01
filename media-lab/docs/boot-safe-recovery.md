# Boot-safe recovery candidate — not activated

## Guarantee and limits

The Sol wrapper refuses to allocate unless `SOL_ROOT/boot-clearance.json` is valid JSON with `approved: true` (a JSON boolean) and `boot_id` equal to `/proc/sys/kernel/random/boot_id`. Missing, stale, malformed or unreadable clearance fails closed. Each reboot requires a NEW operator clearance. `SOL_BOOT_ID_PATH` is an internal test/platform injection path, not an API setting; production should leave it unset. The existing sticky failure and watchdog markers independently deny allocation. An approved boot token never overrides either latch.

This is an operational interlock, not an authorization/security boundary against the runtime account that owns its configuration. It protects this wrapper, not alternate launch scripts, other engines or a root process. It does not pause already-running/queued controller work, and it does not prevent a wrapper restart within the same cleared boot before a durable failure marker exists. Approved rollback to old code removes this protection; retain system-level containment throughout rollback.

No source deployment is authorized by this document. Keep all workload launchers quarantined until source review, tagged deployment and verification have completed. Do not generate a boot token automatically from an init unit, health monitor, residency controller, or deployment script. No canary is part of rescue boot.

## Three separate recovery layers

1. Firmware/hardware watchdog: last-resort host reset when keepalives cease. PID1 owning a hardware watchdog is not a GPU workload progress check. A responsive PID1 can continue feeding while application/GPU/IO operations stall. Verify the actual device, owner, driver action and effective timeout per machine; never infer the second host from its peer. Do not open `/dev/watchdog*` to inspect it. Do not run a second watchdog daemon against PID1's device.
2. Independent OS supervisor: a FUTURE root-owned, small-memory service outside the media user's cgroup. It has no model/GPU imports, no free-form shell/API execution, no dependency on the media controller, and separately bounded probes and resource limits. Never wire `FailureAction=reboot` directly to an H3 failure or HTTP timeout. The supervisor's own failure raises an external alert, not a reboot loop.
3. Workload admission: persistent system-level quarantine plus this boot clearance. Host boot/recovery does not restore H3, controller retry, residency or other GPU workloads. Operator review of evidence and approved single-pipeline qualification is required for later release.

## Supervisor design for independent review

The included `media_lab_core/recovery_policy.py` is ONLY an executable advisory policy model. It contains no collection daemon, durable ledger writer, privileged executor, watchdog access or outbound notifier. It cannot recover a live host. It validates ledger epochs and sample types but does not authenticate callers or persist anything. An explicit trusted `begin_boot` transition must be authenticated and durably committed by a future coordinator; observations cannot establish a boot. Incident identity, strictly increasing epoch and retired boot IDs survive ledger reload, and boot changes never replenish the reboot budget. Corrupt/missing ledgers require quarantine and alert, never reconstruction from an empty State. Do not install it as a live service.

Proposed production sequence:

- Root-owned persistent incident ledger, private permissions, exclusive lock, schema/range validation, atomic replace plus fsync(file) and fsync(parent). Missing/corrupt ledger on an established installation means quarantine and alert; never reset budget to zero implicitly. Provisioning is a separate operator action.
- Root-owned persistent quarantine must deny all approved launch paths BEFORE failure escalation. On initial rescue, block the entire affected user manager until root/cron/container launch paths have been inventoried. The narrower normal-operation interlock must deny controller launch, timers, residency and direct engine start; wrapper clearance alone is insufficient.
- Store workload admission as dirty before a heavyweight load; after an unclean controller/worker exit, remain quarantined. Clear only under explicit qualification policy. This pre-load dirty ledger/executor is not implemented in this candidate.
- CPU-only sampling with boot ID, monotonic sequence/time, memory/IO PSI, cgroup memory events, bounded driver-log excerpts and freshness of authenticated external host observations. No model load or indefinite nvidia-smi. All blocking probes run with hard deadlines in a resource-capped helper. Proposed 30-second cadence and three confirmed samples are REVIEW DEFAULTS, not validated hardware thresholds.
- Low memory or H3 failure: durably quarantine, capture bounded evidence, stop ONLY the approved workload cgroup, alert. No reboot for merely slow generation, expired inference ETA, HTTP 503, peer/network loss, or a single failed probe. No broad kill.
- Host-level stall escalation requires independent confirmation, verified durable quarantine, saved evidence, writable persistent budget and a separately approved actuator. Network silence alone cannot distinguish link loss from a host failure. If evidence/ledger persistence fails, no software reboot proposal; external observer alerts. Hardware watchdog may still reset independently.
- At most ONE software reboot proposal per incident across process restart and boot changes. Commit the spent budget BEFORE an idempotent actuator runs; if the actuator outcome is uncertain, leave it spent. Budget is not replenished on time passage or a healthy probe. Root operator resets it only after review.
- After reboot, all workloads remain quarantined. A healthy host probe never clears H3. No cycle of reboot → load H3 → reboot. Hardware resets cannot be counted/limited by software that is already wedged; quarantine and external observation reduce but cannot eliminate that risk.
- Authenticated out-of-band observer stores heartbeat/incident IDs and sends deduplicated alerts with acknowledged delivery. Alert failure remains a fault, not a successful notification. New endpoints, ACLs, credentials or chat routes require approval; none added here.

The fixture suite exercises the policy against missing evidence/ledger/quarantine, lost peer confirmation, stale observations, healthy samples, and a persisted budget across a simulated boot change. It does NOT validate the proposed cadence, collectors, authentication, crash-consistent ledger, host reset or real watchdog timing.

## Offline container rescue

See `offline-container-rescue.md`: Docker `unless-stopped` containers may restart independently of the user manager. An approved emergency boot must contain daemon/socket activation too; inventory persistent metadata without invoking a Docker client or SDK. Persistent containment requires exact approval including unrelated container impact before leaving emergency mode. No container names or complete live inventory are inferred from repository launch scripts.

## Acceptance before activation

Independent code/security review; real source provenance and exact service inventory; preserved queue/assets; staged source from a reviewed tag; effective system-level quarantine verified with all launchers attempted under safe CPU fixtures; root-owned ledger fault injection; observer alert receipt; controller/engine absence after a deliberate approved maintenance boot. Only then consider a separate nonproduction watchdog timeout exercise with backups and an available console. Intentional panic/reset, firmware/AC policy changes, power cycling and live H3 qualification each require exact approval.

If no tested remote reset/power path exists, a currently unreachable host still needs one supervised local/console intervention. Do not promise unattended recovery retroactively. Factory recovery is NOT rescue: NVIDIA's documented factory recovery erases the internal SSD.

## References checked 2026-09-16

- https://www.man7.org/linux/man-pages/man5/systemd-system.conf.5.html — RuntimeWatchdog and second-phase RebootWatchdog semantics. Consult the installed systemd version too.
- https://raw.githubusercontent.com/torvalds/linux/master/drivers/watchdog/sbsa_gwdt.c — upstream WS0/WS1 behavior; not proof the deployed vendor module has identical fixes. action=1 is panic at WS0 with later WS1 hardware reset; action=0 ignores WS0 and uses WS1.
- https://docs.nvidia.com/dgx/dgx-spark/system-recovery.html — console/USB factory recovery prerequisites and SSD-erasure warning. Does not establish a working BMC or AC-recovery path for a particular installation.

## CPU tests

    python -m pytest -q tests/test_sol_engine_safety.py tests/test_recovery_policy.py tests/test_memavail_watchdog.py tests/test_residency.py tests/test_residency_runtime.py tests/test_h3_timeout_recovery.py tests/test_auto_retry_hold.py
