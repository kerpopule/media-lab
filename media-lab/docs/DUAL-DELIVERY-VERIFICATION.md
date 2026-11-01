# Dual delivery release verification

Candidate implementation reconciled with canonical main at `0b8cd24`; tested implementation revision `00396dc304652ae4526dcfb543a4a5bdc72fbfc7`.

## Executed checks

- Before reconciliation: CPU/non-GPU suite, 1189 passed, 16 skipped, 12 deselected, 564 subtests passed. This is baseline evidence, not exact-head release acceptance. Warnings included deprecated APIs and an online-worker fixture writing to an absent host directory.
- After reconciliation: HDR defaults, dual delivery, unpacked source bundle, web-only lifecycle and deployment-policy tests: 46 passed in 38.56 seconds.
- Identity guard: 1849 files clean. Whitespace/error-marker diff check passed.
- Offline real Chromium over the exact production component functions/CSS: eight check groups passed. Coverage includes 320/390/1280px, 200% CSS zoom, light/dark composited contrast, keyboard Tab/Enter selecting SDR, unsafe/same-file URLs, missing/legacy metadata and late asynchronous replies. Browser exited and its bounded resource lease was released. This fixture intentionally places the job-detail and player controls together; their duplicate actions are not proof that both surfaces appear together in the full application.
- Screenshot review: narrow and forced-color controls have readable, distinct, unclipped Save HDR / Save vibrant SDR labels. Screenshots do not establish physical-device save/share behavior.

## Exact application-path benchmark

`tools/benchmark_application_delivery.py` executes the owned application's actual `_finish_video` and `gallery_add` functions with isolated state, no inference, and no face repair. It verifies one completed job, one local Library record with matching variants, an unchanged retained original, and a generated JPEG poster.

On one arm64 Mac run using the 15.083333-second, 362-frame, 24-fps, 1216×672 reference engine source:

- Whole finalizer/Library path: 50.642 seconds
- SDR encode/LUT: 25.735 seconds
- SDR technical verification: 1.294 seconds
- Source SHA-256: `e059be5765706de671e40f2f79529e740d25f8d7db12d0c725cd468fd3e8fad2`
- HDR SHA-256: `52f4c1910dcfac4092b989c252fd55d7788bb1f8581dd4ce62eef491eafb90cc`
- SDR SHA-256: `68a8c0e792a70f2b12f782f48f8858bc1f3dac05573c05bdd29f2a30fb7e254c`
- HDR codec VUI and MP4 nclx: 9/18/9; full decode exit 0

This run measures local CPU finishing, not Spark throughput, generation time, physical HDR appearance, full temporal creative quality or universal renderer/export-path acceptance.

## Deployment compatibility finding

Read-only comparison found the live installation contains additional image adapters, queue restoration policy, model provenance and private 3D integration not represented by its base deployment tag. The candidate's whole `app.py` differs by 126 additions and 542 deletions relative to that live preimage. A plain whole-tree deployment would remove existing functionality. No such deployment or service restart has been performed.

Preserve these private overlays. Do not publish the captured live source or use this finding as authority to remove it. Resolve canonical source versus approved private overlay packaging before activating a tagged release. Current base-tag metadata alone is insufficient provenance.

## Repository identity

The canonical repository currently contains `app`, `desktop` and `media-lab`. Renaming the entire repository is not a safe Media Lab-only rename. A dedicated open-source Media Lab repository has since been authorized and extracted; see the root provenance record. The original VibeXStudio repository remains intact. The extraction does not migrate private overlays or activate a live deployment.
