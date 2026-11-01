# Media Lab web-only mode during paired GPU serving

The tunneled web origin and the GPU render workers are separate lifecycles. A paired GLM reservation must not take down the user's Library and sign-in page.

## Services

- `media-lab-simple.service` is the normal Media Lab controller with its existing render workers. Its existing paired-reservation condition is retained.
- `media-lab-web.service` serves the same authenticated UI on the same port, with `MEDIA_LAB_WEB_ONLY=1`, background workers disabled, voice prewarming off and `CUDA_VISIBLE_DEVICES=-1`. Startup skips orphan reaping, GPU handoff, residency recovery and background execution. It has a 1.5 GiB memory cap and a one-core CPU quota.
- The two services conflict and are ordered, so the normal controller can replace web-only mode without a port collision. A paired-mode stop queues the web-only service through `start-web-only-if-reserved.py`; this hook does not change a model, clear a lease or restart the pair controller.

The shipped service is a template. Its bind default is loopback. A private installation whose existing tunnel points to a tailnet address must set the approved bind address in a per-host systemd override. Never publish private deployment identity in the template.

For an already approved private installation, install `systemd/media-lab-web.service` as the user service and `systemd/media-lab-simple-web-only.conf` as a drop-in on the existing normal service. Review port and bind mapping first. User-daemon reload and service activation are live actions requiring the owner's approval. Do not substitute this procedure for the paired controller's model switch protocol.

## Admission and authentication

Web-only mode preserves existing family/owner authentication. Anonymous requests still reach the family-code gate, not the protected app HTML or script files. An HTTP 401 carrying the legitimate sign-in page is expected; it is not an outage.

`GET /api/compute-mode` reports the configured web-only admission and optional Mac-worker heartbeat. A mode banner explains that browsing is online while Spark rendering is paused.

Compute mutations, residency changes, model setup and privileged retries return an explicit `spark_gpu_reserved` response before the handler can run. Only the existing small safe-write allowlist for sign-in/out, bounded upload/import and push registration remains available. Chat and other compute mutations are paused. This is not a claim that all creative workflows are available during GLM mode.

Mac image workers are a separate scoped job interface. UI restoration does not activate a Mac worker, register an unqualified model or give a Spark caller Mac shell/file-write access. If the worker's heartbeat is stale, report it offline. Never silently route to a paid provider or unload GLM to make an image fit.

## Deployment safeguards

The deploy allowlist includes `web_only_policy.py`, so a future deployment cannot ship an importing app without its policy module. When the paired reservation exists, the deploy script selects the installed web-only service rather than trying to restart the excluded GPU controller. It refuses this selection if the web-only unit is absent.

A current private runtime can carry pre-existing changes beyond the canonical source, including its own GPU-handoff hook. Apply only the reviewed hunks to each baseline; never overwrite one with the other merely to make hashes match.

## Verification

Run `tests/test_web_only_ui.py` plus the existing Studio gate and engine-transition suites. Assert:

1. Web-only startup invokes no reaper, residency recovery, render worker or GPU handoff.
2. Normal startup retains its original baseline behavior.
3. Authentication remains outermost and compute rejection happens before the handler.
4. Public family-code gate and manifest transfers remain stable over multiple refresh periods, with TLS verification.
5. Protected app HTML/assets stay protected without a valid family session. Do not weaken authentication to make an anonymous HTTP test return 200.
6. Read back deployed file hashes and verify the canonical GPU lease is unchanged.
7. The existing paired-model verifier still confirms both ranks and produces its real answer.

Record authenticated-browser interactions separately. A native CLI token is not a family browser session; do not forge cookies, extract family codes or claim browser coverage from a sign-in-page fetch.

## Rollback

Keep original app and index hashes, backups of any pre-existing files and absence receipts for new files. Stop only the new web-only service, restore the guarded code and remove only this change's own unit/drop-in/helper files if rollback is necessary. Leave the paired GPU controller and its reservation untouched. While GLM owns the pair, the prior state of the old web origin was stopped; do not start its GPU workers as a rollback shortcut.
