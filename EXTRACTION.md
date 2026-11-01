# Media Lab extraction provenance

## Source

- Upstream: https://github.com/kerpopule/vibexstudio
- Public upstream main used for reconciliation: `0b8cd24e848ea5da89148fb74cbc2399f394186e`
- Sanitized dual-delivery candidate snapshot: `066c4ebe9da159cd3a87cfbbd71d1696098ff41b`
- Candidate discussion: https://github.com/kerpopule/vibexstudio/pull/61

## Method and bounds

A new repository is initialized from `git archive` of the pinned candidate. Copied paths are `media-lab/`, root `LICENSE`, root `.gitignore`, the identity-guard and Media Lab CPU-suite workflows, and `docs/CAPABILITY-STATUS.md`. This intentionally creates a source snapshot rather than importing unrelated app/desktop history. The original public repository and its complete history remain unchanged.

Extraction-specific additions are this provenance record and the root README. The deployment guide identifies the new source repository and explicitly keeps existing installations on their captured provenance until a verified migration. The application directory remains `media-lab/`, preserving its relative paths and archive/deployment contract.

No private live Spark source, deployment credentials, state, models, media, voices or likeness references were copied. The published source and a particular private installation are not identical: the latter has additional overlays that must be preserved before any deployment. Repository creation is not a live cutover.

## Verification

Run the identity guard from the new root, the focused dual-delivery/source-bundle/deploy-policy suite, and the broader CPU suite. Require GitHub exact-head checks before declaring the extraction qualified. Source-snapshot identity and source-root-relative links are checked locally; host/GPU and physical-device limitations remain as stated in the delivery verification document.
