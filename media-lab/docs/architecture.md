# Target architecture

## Why the private prototype must change

The current prototype combines HTTP routes, authentication, mutable in-memory jobs, flat-file persistence, queue scheduling, GPU admission, model lifecycle, post-processing, and recovery in one import-time process. Multiple ASGI workers would each start their own queue worker against shared JSON state. HTTP health failures are sometimes interpreted as absence of a GPU owner, and cancellation is not job-scoped across every subprocess and ComfyUI request.

The migration keeps the product behavior but replaces the control plane.

## Components

### 1. API service

- Created through an application factory with no import-time side effects.
- Validates requests and authorization.
- Writes jobs and cancellation requests transactionally.
- Serves artifacts only after an atomic publication transition.
- Exposes `/live`, `/ready`, and dependency-rich `/health` separately.

### 2. SQLite state service

SQLite WAL is the supported single-host default. PostgreSQL can be added for real multi-host deployments.

Every job has:

- immutable request and provenance inputs;
- enumerated status and stage;
- monotonic epoch;
- idempotency key;
- worker owner and expiring lease;
- cancellation timestamp;
- stage heartbeat and deadline;
- engine request/prompt ID;
- artifact and QA receipts.

A malformed database is quarantined and restored from backup; it is never interpreted as an empty queue.

### 3. Single GPU scheduler

One scheduler actor serializes every heavyweight consumer: video, image, music, enhancement, voice, and externally reserved work.

It maintains two leases:

- **residency lease:** which process/cgroup owns loaded model memory;
- **compute lease:** which job is actively executing kernels.

A dead health endpoint does not release either lease. Release requires process/cgroup exit or a verified shutdown handshake. Admission uses measured phase budgets and an immutable memory floor.

### 4. Engine adapters

Adapters implement one protocol:

- `inspect()` returns immutable manifest and actual capabilities;
- `start()` and `stop()` are idempotent and bounded;
- `ready(expected_manifest)` proves the desired backend is loaded;
- `submit(job)` returns an engine-scoped request ID;
- `progress(request_id)` returns a monotonic stage/step heartbeat;
- `cancel(request_id)` affects only that request;
- `collect(request_id)` returns files plus hashes;
- `diagnostics()` reports selected kernels and memory/runtime facts.

No adapter may silently change model, pipeline, audio contract, resolution, conditioning mode, or seed.

### 5. Artifact/QA service

Artifacts move from staging to accepted storage only after:

1. independent video decode;
2. independent audio decode where required;
3. probe, dimensions, duration, frame count, bytes, and hash;
4. engine manifest and input hash receipt;
5. required visual/audio/human review state.

Technical validity is not creative approval.

## Backend status for the current Spark

- **General H3:** migrate the promoted source-pinned R12 ComfyUI workflow into an adapter. Its existing immutable image, workflow, model hashes, seed/settings, and transaction receipts are the reference.
- **Maestro H3 warm server:** quarantined experimental adapter. The tested container reports a PyTorch arch list ending at `sm_120` on an SM121 GB10 and lacks several dedicated kernel packages. It must not report qualified readiness.
- **LTX:** keep as an external adapter. Maestro/WanGP software terms are independent from the controller and must not be bundled under the controller's future license.
- **Image/Music/Qwen:** external adapters with explicit memory and lifecycle contracts. Their current host-local paths and units belong in a private deployment overlay.

## H3 qualification sequence

1. Prove the official NVIDIA GB10 control path in an isolated, pinned CUDA 13 container.
2. Re-run the R12 ComfyUI baseline at a small deterministic fixture with standard SDPA.
3. Add Sol-Attn pointer mode at a revision containing architecture-safe autotuning; persist and time the Triton cache.
4. Test Sage independently.
5. Test caching independently, preserving paired video/audio outputs.
6. Add combinations only after each single-variable arm passes identity, motion, prompt, audio, and synchronization review.
7. Record first compile separately from warm inference.

## Migration order

1. Freeze and snapshot the private app.
2. Introduce the transactional store and event model beside the legacy queue.
3. Move the worker into its own process and require a durable lease.
4. Introduce fake-engine integration tests.
5. Move one backend at a time behind the adapter protocol, beginning with R12 H3 and current LTX.
6. Replace global interrupts with engine request IDs.
7. Replace flat JSON and import-time threads.
8. Add authentication/ownership and public-safe asset storage.
9. Run clean-clone, fault-injection, security, and optional GPU CI gates.
10. Only then select a code license and prepare a public release candidate.
