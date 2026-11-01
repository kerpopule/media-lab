# HDR + vibrant SDR delivery candidate

## Outcome and scope

One AI-generated source produces two technically verified downloads. The engine original is retained byte-for-byte outside the Library. Optional face repair runs once, before both grades. The source is not regenerated, resized, sharpened, truncated or frame-rate converted. Existing uploads/Library entries are not retroactively regraded.

The implementation was qualified locally and is **not a live deployment**. It builds on the existing managed HLG policy. This standalone repository was extracted with owner authorization; deployment still requires preservation of existing private overlays and passing release checks. No model changes, GPU lease, paid generation or media publication are introduced.

## Runtime contract

- `_finish_video` calls `hdr_delivery.prepare(..., sdr_target=...)`. Both versions derive from the same pre-HDR working source; the raw engine output is separately retained.
- HDR remains the inherited H.264 High **8-bit** / HLG / BT.2020 managed delivery. This is not a sensor-native HDR master, a new 10-bit mode, recovered highlight detail or Dolby Vision.
- SDR is H.264 High 8-bit, BT.709, limited range, CRF20/slow. It uses endpoint-preserving contrast `.08` and adaptive vibrance `.18`, with gamut-limited chroma. No resolution-specific bitrate cap is imposed on a master download.
- Native HLG sources are explicitly tone mapped before SDR vibrance. PQ, foreign gamut, inconsistent HLG signaling and unqualified full-range conversions fail closed. Untagged engine SDR uses the existing explicit BT.709 assumption.
- Both outputs copy audio streams rather than regenerating or re-encoding the soundtrack. Unsupported container/codec combinations fail instead of silently changing the audio.
- SDR verification covers codec profile, pixel format, range, BT.709 codec VUI **and** MP4 colr signaling, full error-sensitive decode, source dimensions/frame count/cadence/duration and audio stream format/duration. HDR keeps its existing codec/container/decode checks.
- Owned encoding/decoding processes have bounded execution and cancellation/reaping. Metadata probes/header checks are separately bounded. These operations do not stop shared services or acquire a GPU.
- The produced-MP4 Library gate covers legacy stitch/music-video/boardfilm/enhancement callers too. It creates both outputs before committing the Library row, or validates a receipt-bound pair from the main finalizer. Stale siblings cannot be adopted merely by filename.
- Main-finalizer encoder, validation and Library-write failures leave the job unfinished/error and retain the engine original. Its new delivery files are removed on handled publication failure. Per-file replacement is atomic; **there is no multi-file filesystem transaction**. A process crash between file moves can leave unlisted files requiring reconciliation. This is not a crash-consistency guarantee for every legacy caller.
- `variants: {hdr, sdr}` is exposed by Library records, queue history and individual-job projections without exporting private finishing receipts. Players and job details expose Save HDR / Save vibrant SDR; missing/legacy metadata retains Save. Late lookup responses cannot attach another clip's SDR file.

## Reproduction

Use Python 3.11+ with the project's dev/image extras, NumPy, ffmpeg and ffprobe. Keep test temporary files in an owned workspace by setting `TMPDIR`.

```sh
PYTHONPATH=. python -m pytest -q tests/test_hdr_delivery_default.py tests/test_dual_delivery.py
PYTHONPATH=. python -m pytest -q -m 'not spark and not gpu'
PYTHONPATH=. python tools/benchmark_dual_delivery.py SOURCE.mp4 NEW_OUTPUT_DIR
```

The benchmark preserves full source duration, writes new files only, records encoding and verification time separately, and invokes no AI engine. Its output is a measurement on the host used, not a promise about another machine.

For optional offline Chromium UI verification, install Playwright in an isolated dev environment and run `tools/verify_dual_delivery_ui.py OUTPUT --chrome /absolute/path/to/chrome` inside an owned, bounded resource lease. The harness uses exact production functions/CSS and synthetic API fixtures, intercepts all networking, checks 320/390/1280px, 200% zoom, dark/light contrast, keyboard actions, unsafe URLs, legacy/error states and stale responses. It closes only its own browser. It is not a full application/device acceptance test.

## Evidence and remaining release gates

The representative dirtbike engine source was **15.083333 seconds / 362 frames / 24 fps / 1216×672**, rather than an exact 15-second trim. One local arm64 Mac run took **41.864 s** for both outputs, of which **20.504 s** was the additional SDR stage (19.640 s encode/LUT + 0.865 s verification; rounded components differ by 0.001 s). Source SHA-256 was unchanged. Both decoded soundtracks matched the source frame hashes. Representative SDR comparison frames retained composition without evident severe clipping or crushed detail; that sampled review is not full temporal or HDR-display approval.

Before release: exact owner approval for public source publication and tagged deployment, canonical deployment/preimage compatibility checks, an idle-host canary through each real renderer/export path, native device Save/share behavior, and compatible physical HDR-display review. The standalone controller's separate engine worker pipeline has not been changed to claim universal dual delivery. Remaining whole-product quality work is not declared done by this feature.

Inherited proof logs have only private home prefixes redacted in this candidate; measurements/hashes are unchanged. Public branch preparation must start from canonical main and apply the sanitized combined change, not publish the older local HDR branch's private-path history.
