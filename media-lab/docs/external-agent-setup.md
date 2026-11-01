# External agent setup contract

## Product goal

A fresh user can give this repository URL to a capable coding agent or run one documented terminal command, choose capability packs, review terms and resource costs, install verified local components, and open Media Lab with a working first-use flow.

The public controller does not bundle model weights, private deployment settings, production media, voices, likenesses, credentials, or restricted runtimes.

## Required setup sequence

An external agent must:

1. Read `README.md`, `docs/architecture.md`, `docs/installation-and-onboarding.md`, and `docs/open-source-readiness.md`.
2. Verify Python 3.11+, OS/architecture, GPU/driver/CUDA, RAM, disk, Docker/runtime availability, and writable data/model/cache roots.
3. Run the catalog planner. Never bypass blocked or unqualified entries.
4. Show selected capabilities, dependencies, disk requirement, measured memory floor, swap groups, sources/revisions/hashes, and license terms.
5. Obtain the user's direct acceptance for each gated model/runtime. An agent cannot accept third-party terms for the user.
6. Download only manifest-pinned sources and verify bytes plus SHA-256.
7. Install into versioned isolated adapter/runtime directories. Permit SafeTensor weights only unless another format has a separate security review.
8. Run fake-engine checks, bounded hardware preflight, and one deterministic tracer per selected engine.
9. Register only engines whose actual manifest matches the requested immutable manifest and whose qualification receipt passes.
10. Start the app and complete preference onboarding. Use a selected local LLM only after its own health check passes.
11. Report installed and blocked capabilities, health endpoints, revisions, and rollback/uninstall instructions.

## External control contract

Agents use supported CLI/API operations rather than editing state files or killing processes:

- inspect catalog and machine compatibility;
- produce and review an install plan;
- install, verify, enable, disable, and swap adapters;
- inspect queue, manifests, residency, and health;
- submit/cancel jobs by scoped request ID;
- collect technical QA receipts;
- export/import sanitized configuration.

The API never uses client IP as authentication. Behind Cloudflare every request may appear local.

## Hard rules

- Local inference only; no cloud-model fallback.
- Never silently substitute an engine, model variant, quantization, audio contract, or pipeline.
- One GPU and one canonical owner/lease; every GPU consumer participates.
- Never render around a lock, interrupt healthy work, or broadly kill processes.
- Preserve exact pre-state and verify restoration.
- Keep credentials and accepted-license receipts out of repositories and logs.
- Never commit models, media, characters, voices, jobs, caches, or generated secrets.
- File presence is not installation proof: require hash, load, deterministic tracer, decode, and receipt.
- Publication, customer data, external sharing, paid actions, public endpoints, signing, and production cutover require separate approval.

## Installer lifecycle

The installer is resumable and idempotent:

`inspect → select → license review → disk plan → download → hash → install → preflight → tracer → register → onboard`

Each stage writes a receipt. A failure stops at its real stage and never becomes a silent creative fallback.

## Local-chat onboarding boundary

The selected local setup model may ask about default engines, orientation, quality-versus-speed preferences, storage/retention, and optional capabilities. It may explain errors and propose configuration.

It must not:

- invisibly repair a broken base installer;
- accept licenses or voice/likeness authorization;
- change networking, credentials, services, security, or models without a deterministic action preview;
- claim backend health without a machine-readable preflight receipt.

The complete deterministic terminal/web onboarding remains available when no LLM is selected.

## Setup completion receipt

Require platform/hardware facts, selected/skipped IDs, terms identifiers, source revisions, bytes and SHA-256, installer exit codes, adapter-manifest matches, tracer and decode results, app health, model-swap restoration, no-secret checks, and uninstall/rollback instructions.
