# Sol-H3 cold-load control-plane liveness

## Incident root-cause boundary

The prior-boot evidence proves two controller defects and one missing safety boundary:

1. The queue watchdog treated a deliberately queued row behind `gpu-recovery-hold.json` as a dead worker. Its second strike restarted Media Lab at 20:28. A recovery hold cannot be cleared by restarting the controller, so this was both ineffective and disruptive.
2. H3 was stopped and restarted during that controller restart. The stopped unit reported a 55.7 GiB memory peak and 4.3 GiB swap peak.
3. The only independent pressure action was a five-second user timer that required `MemAvailable < 8 GiB` for three samples and then asked the same user manager to stop H3. It never recorded a trip. That design cannot detect sustained swap growth or PSI stalls above 8 GiB, and it depends on a control plane already being starved.

API and Tailscale SSH remained intermittent through 20:38:15, then journal activity stopped until the operator reboot. No kernel OOM, NVIDIA Xid, completed artifact, or exact terminal cause survived. Therefore it would be false to claim a proven kernel/GPU cause. The actionable software root cause is narrower and conclusive: the controller restarted around a legitimate recovery hold, while the cold load had no fresh boot-bound observer, no PSI/swap boundary, no exact direct-cgroup termination path, and no dormant-loaded unit contract. The old `MemAvailable` timer was mitigation, not a liveness guarantee.

## New fail-closed contract

The model, checkpoint, trained canvas, and output quality contract are unchanged. Unsafe loads are refused or terminated; weights are not reduced and work is never rerouted.

Authoritative names and paths for the private release receipt:

- H3 dormant unit: `media-lab-sol-h3.service`
- independent guard: `solh3-control-plane-guard.service`
- guard heartbeat: `$XDG_RUNTIME_DIR/solh3-control-plane-guard.json` (normally `/run/user/1000/solh3-control-plane-guard.json`)
- per-load private environment: `$XDG_RUNTIME_DIR/media-lab-sol-h3.env`, mode 0600
- legacy latch: `$XDG_RUNTIME_DIR/flashnext-memwatch.latch`
- durable safety stop: `$SOL_ROOT/safety-stop.json`
- durable incident receipts: `$SOL_ROOT/control-plane-incidents/`
- boot clearance: `$SOL_ROOT/boot-clearance.json` with `{"approved": true, "boot_id": "<exact current /proc/sys/kernel/random/boot_id>"}`

`media-lab-sol-h3.service` is installed but never enabled. Its safe post-reboot state is `LoadState=loaded`, `ActiveState=inactive`, `SubState=dead`, `MainPID=0`. It starts only after Media Lab has an exact load-phase durable GPU lease, writes the lease-bound runtime environment atomically, and sees a fresh same-boot guard heartbeat. The unit independently repeats the heartbeat check in `ExecStartPre`.

The H3 cgroup has `Restart=no`, `KillMode=control-group`, a ten-second stop bound, and `MemorySwapMax=2G`. The guard runs in a separate high-weight control-plane slice with both `MemoryLow=64M` and `MemoryMin=64M`. Once the exact H3 cgroup appears, it samples once per second and latches after three consecutive samples of any one condition:

- `MemAvailable < 8 GiB`;
- H3-session swap growth of at least 2 GiB;
- memory PSI `full avg10 >= 10%`.

On a trip it first writes the durable safety stop and incident receipt, then opens Linux pidfds for PIDs recursively proven inside the exact H3 cgroup, revalidates membership after pinning each process identity, sends TERM, waits at most ten seconds, sends KILL only to remaining revalidated identities, and verifies the cgroup is empty for two seconds. It never reboots, restarts H3, scans by process name, kills another cgroup, clears a fence, or releases a durable GPU lease. Existing latch/safety evidence also causes any relaunched exact H3 cgroup to be terminated rather than bypassing the latch.

Loss of the guard removes/stales admission within five seconds. The app stops an in-progress cold start, and the guard unit's `ExecStopPost` asynchronously asks systemd to stop only `media-lab-sol-h3.service` so the guard shutdown cannot deadlock the user manager. Durable GPU fencing remains authoritative; a terminated or uncertain load requires the existing exact reconciliation path.

The queue watchdog now resets its strike state and stands clear on every controller-restart path when `pool/gpu-recovery-hold.json` exists, including API-unreachable, queued-idle, and stalled-running cases. A durable recovery hold is an expected recovery state, not a restart signal.

## Installation and rollback gate

Source deployment does not install, enable, or start these units. With exact approval for the live unit change, on an idle host with H3 inactive:

    tools/install-solh3-control-guard.sh --install --apply

The installer refuses an active H3 unit or a source tree that does not resolve to the unit's canonical `%h/media-lab-simple` execution root. It backs up both prior unit files (including symlinks) and prior guard enable/active state, installs the dormant H3 and guard units, enables only the guard, checks its same-boot heartbeat, verifies H3 loaded/inactive/dead with PID 0, and writes a private mode-0600 `release-receipt.json` beneath:

    ~/.local/state/media-lab/solh3-unit-releases/<UTC timestamp>-<installer PID>/

That receipt names the deployed tag/commit, every path above, the boot-clearance schema, rollback artifact, exact rollback command, and verification command. Any post-backup installation failure automatically restores the transaction. Explicit rollback refuses while H3 is active and restores both previous unit files plus prior guard enable/active state.

Do not install, enable, roll back, clear markers, issue boot clearance, or start H3 as a diagnostic.

## Focused and synthetic verification

No GPU/model import is required:

    python -m pytest -q \
      tests/test_solh3_control_guard.py \
      tests/test_solh3_unit_contract.py \
      tests/test_watchdog_maestro_safety.py \
      tests/test_memavail_watchdog.py

The synthetic proof executes the real cgroup termination function against a local bounded `sleep` process represented in a fake exact H3 cgroup tree. Other tests prove sustained-vs-transient threshold behavior, swap baseline handling, heartbeat freshness and boot binding, ambiguous-cgroup refusal, recursive exact-member TERM/KILL, private atomic runtime-environment encoding, dormant unit properties, explicit install/rollback gating, and recovery-hold watchdog stand-clear behavior.

## Safe canary plan (not authorization to execute)

No H3 canary is permitted until all preconditions pass read-only:

1. The remote-control acceptance checklist from `t_41aa34dd` passes. Spark 1 ordinary and independent emergency access must both work. Spark 2 must report `systemctl is-system-running=running`; its current `starting` state behind graphical/plymouth/getty jobs is a hard stop.
2. The approved release receipt names the exact guard unit, heartbeat, latch, safety stop, incident directory, boot clearance, tag/commit, rollback artifact, rollback command, and verification command.
3. Spark 1 reports the H3 unit loaded/inactive/dead/PID 0 (the current `LoadState=not-found` is a hard stop), guard active with a fresh same-boot heartbeat, no H3 listener/process/cgroup, no unexplained GPU owner, and no active queue work.
4. Existing durable fence/recovery state is reconciled by its separate approved repair. Safety/latch evidence is preserved and cleared only through a separately approved operator recovery; boot clearance matches the exact current boot.
5. Run the no-GPU focused/synthetic suite on the deployed source and record the release/rollback receipt before any model load.

Only after the owner separately approves one canary: submit exactly one T2V job, observe Spark 1 simultaneously through both management paths, sample guard heartbeat, MemAvailable, swap delta, PSI, H3 cgroup and durable lease once per second, and submit nothing else. Any timeout, stale heartbeat, guard trip, reachability loss, unexpected restart, new safety marker, cgroup survivor, or fence ambiguity is a terminal abort: preserve evidence and do not retry/reboot automatically. Verify terminal job state, artifact presence, incident directory, empty H3 cgroup after settle, and management reachability. I2V remains blocked until the T2V evidence is independently accepted. No automatic hard reboot is part of this plan.
