# Open-source readiness

Status: **published** (2026-08-30) at github.com/kerpopule/media-lab-studio,
Apache-2.0, at the owner's direction. Public snapshots are produced ONLY by
the maintainers' refresh tooling, which excludes internal operations
documents and machine identities and refuses to stage an export that still
contains them.

What the public snapshot deliberately does NOT contain:

- Model weights of any kind — the catalog/installer contracts in
  `media_lab_core/` exist precisely so owners fetch and license models
  themselves (hash-pinned, terms accepted by the human).
- The MiniMax H3 model or any H3-licensed material — its license covers
  private single-machine use only; this repo carries integration code.
- Production/deployment state: job queues, galleries, voices, characters,
  push subscriptions, access codes, machine identities, and internal
  operations documents stay on the operator's machine.
- Credentials of any kind. Cloud rendering (fal.ai) is bring-your-own-key,
  stored server-side by the operator.

Community-derived prompt templates ship with their attribution and
usage notes intact — see CREDITS.md. If your work appears here and you
want different credit (or removal), open an issue.
