# Automatic retry recovery contract

Automatic retries are recovery, not proof that an entire studio is healthy. The
reaper must not start or reload a model to discover whether recovery is possible.

## Admission

`auto_requeue()` preserves the existing `.engine-maintenance` entry hold: a held
pass is a no-op for jobs, both queues, counters and persistence. It also rechecks
the hold before committing a retry. This hold does NOT stop active work, manual
submissions, already-queued jobs, startup recovery, or other supervisors.

Without the hold, a job is considered only when both queues and all active job
states are empty. Explicit cancellation and non-error terminal states are left
untouched. Explicit `retryable=false` is authoritative. Only legacy records with
no retryable field fall back to infrastructure-message classification.

The existing three-attempt/six-hour limits remain; malformed counters/timestamps,
future timestamps and exhausted limits refuse recovery. Nothing resets a retry
budget. Unknown evidence never makes a job eligible.

Only local single-engine `video` and `filmbeat` jobs have an automatic recovery
contract here. Composite jobs (including `musicvideo`), cloud jobs, imported work,
and unsupported engines require operator recovery. This is intentional: a video
health response cannot prove recovery of every component of a composite job.

Recovery identity follows the actual kind-specific runner, not the scheduling
helper's selector precedence. `video` requires an explicit top-level `engine` of
`h3` or `ltx25`, as emitted by `make_video_job`; absent/unsupported values are held,
not guessed from request metadata. `filmbeat` always executes LTX. Recognized but
contradictory request selectors do not override those execution contracts, and
the original request is never rewritten. Malformed requests or present selectors
outside `h3`/`ltx`/`ltx25` are held without a health probe. Top-level `ltx` is not a
valid video execution value: the current runner only recognizes `ltx25` as LTX.

The exact execution engine must answer a bounded read-only health request with
matching identity, `ok=true`, `loaded=true`, and `busy=false`. H3 additionally
requires explicit `blocked=false` and `loading=false`, so the older H3 shim without
the safety-latch health contract remains held. Cold engines are never booted by
the probe. LTX's current health contract lacks those additional keys; if present,
they must be false.

Memory must be finite and available from the actual host. Recovery calls
`_mem_available_gb(strict=True)`: unreadable procfs, missing `MemAvailable`, invalid
non-negative integer syntax or missing/incorrect `kB` units raise and persist an
unknown-memory hold, never a permissive numeric sentinel. Other callers keep the
existing default-reader behavior; its legacy `999.0` fallback is explicitly not
recovery evidence. This patch does not claim to harden unrelated admission paths.
The current residency
policy must contain valid positive cold-load, sampler and decode budgets plus an
operational reserve. The conservative threshold is the maximum of those phase
budgets plus the reserve; it gives no credit for hypothetical weight releases or
current residents. This may intentionally prevent all automatic H3 retries on a
loaded unified-memory host. A future relaxation needs measured qualification and
review, not guessed subtraction. These policy values are NOT measurement proof.

## State and escalation

An unsafe/ineligible error remains an error, with its message, detail, original
request, source references, checkpoints and retry count preserved. Its separate
`auto_retry_hold` object contains a reason and an operator action. This is persisted
in the ordinary jobs store (`jobs.json`, `jobs[ID].auto_retry_hold`); the current
public job/queue response and UI do not expose this new field. Use the existing
trusted operator state-inspection path, not a public metadata endpoint. Repeated
identical holds do not rewrite the store.

The probe performs no model/service/reconciliation/allocation call. Before changing
state, the controller rechecks maintenance, active jobs/queues and the exact job
snapshot under the queue condition lock. Only one job can be admitted per pass.
The saved queue must succeed before workers are notified. On save failure, the
in-memory job and queue are restored; failed hold persistence is retried later.

## Limits

This is a pre-queue gate, not a cross-process GPU lease or execution-time memory
reservation. Health/memory can change after admission. Existing execution-time
inference locks, admission, loader serialization/unload verification and safety
latches remain necessary. It does not repair the H3 loader or watchdog itself.

It does not re-run old failed storyboards automatically. Storyboard generation now
uses one custom-plus-known character snapshot for resolution, composition and
likeness lookup; recovery of an existing failed job remains an explicit action.

CPU tests execute extracted controller functions with synthetic engine responses
and real temporary-file persistence. They do not import app startup, start a
service, contact an engine or validate a GPU render. Full-app/deployed integration,
GPU qualification, original-job recovery and creative approval are separate gates.
