# PPLX-27B + One Companion Qualification

Date: 2026-08-26
Scope: the private DGX Spark studio host (ssh target `MEDIA_LAB_SSH` in config/local.env)
Source mirror: the operator's private `media-lab-simple/` checkout
Runtime: `~/media-lab-simple/`

## Contract

`pplx-computer-qwen-3-8-27b-dflash2-20260824` stays resident on canonical `:8004` under alias `media-lab-text`. Exactly one heavyweight companion is resident or rendering at a time:

1. LTX-2.5
2. MiniMax H3
3. Qwen-Image
4. Qwen-Image-2.1 (declared bounds; not yet run on this host)
5. MiniMax Music 3
6. one loaded Voicebox TTS model (Qwen3-TTS or another installed preset engine)

LTX is the default idle companion. Image and Voicebox service shells may remain up only when their model state proves unloaded.

## Evidence already obtained

### Read-only live snapshot — PASS

`runner/verify_companion_snapshot.py` executed over SSH without changing runtime state:

- PPLX model ids: `pplx-computer-qwen-3-8-27b-dflash2-20260824`, `media-lab-text`
- Concurrent text probe: exact `PPLX_PRIMARY_OK`
- Resident companions: `ltx` only
- LTX health: `loaded=true`, `busy=false`
- H3: cold
- Image model: unloaded (`loaded_model=null`)
- Music 3: cold
- Voicebox models: all unloaded
- `MemAvailable`: 55.745 GiB
- Operational floor: 24.000 GiB
- Active Media Lab jobs: none

### PPLX + LTX render canary — PASS

Report: `~/media-lab-simple/reports/pplx-ltx-co-residency-20260826/canary-summary.json`

- Job: `fd57b5302367`
- Artifact: `~/media-lab-simple/media/fd57b5302367.mp4`
- SHA-256: `863f6a95f7783b472c183e6aba978614b06308d74d02e544c341f11eef4e72ec`
- Decode: pass
- PPLX probes: 5/5 pass
- Maximum PPLX latency: 1.028 s
- Minimum `MemAvailable`: 36.124 GiB
- Floor margin: 12.124 GiB

## Required canary matrix before live promotion

Every canary must enter through the visible Media Lab queue. Sample `MemAvailable` every 1–2 seconds, probe PPLX every 10–15 seconds, validate the artifact, compare OOM/journal counters before and after, and prove restoration to PPLX + LTX.

| Companion | Smallest representative canary | Promotion criteria | Current status |
|---|---|---|---|
| LTX-2.5 | 3-second 1280×704 T2V with native audio | Valid H.264/AAC artifact; all PPLX probes pass; floor ≥24 GiB | Qualified |
| MiniMax H3 | 3-second 864×480 private FL2VA/Ref2VA canary | Valid artifact; preserved input audio when used; all PPLX probes pass; floor ≥24 GiB | Not yet run |
| Qwen-Image | One 1024×1024 4-step image | Valid PNG; `loaded_model=qwen`; only one companion; floor ≥24 GiB | Not yet run |
| Qwen-Image-2.1 | One bounded text-to-image run at 1024² | Valid PNG; outgoing weights freed first; only one companion; floor ≥24 GiB | Not yet run |
| Music 3 | One shortest supported 60-second instrumental | Valid FLAC/MP3; all PPLX probes pass; floor ≥24 GiB | Not yet run |
| Qwen3-TTS 1.7B | One short sentence using an approved internal voice | Valid WAV/MP3; PPLX probes pass; `/models/status` shows all models unloaded afterward | Not yet run |
| Kokoro preset | One short sentence | Same as TTS above | Not yet run |

Do not batch-launch this matrix. Run one canary, inspect its artifact and memory trace, approve or adjust, then proceed to the next model.

## Source implementation prepared (not deployed)

- `config/companion-residency-policy.json`
- `app.py`
  - hard PPLX health admission on `:8004`
  - one-companion stand-down logic
  - busy companions are never interrupted
  - Music 3 uses the shared inference transaction
  - Voicebox unloads every loaded model through `/models/{model_name}/unload`
  - faster-whisper and resemble-enhance run inside the same governed audio-companion transaction
  - Music/TTS/image jobs block idle LTX restoration
  - automatic hourly TTS preview rendering is disabled by default
- `runner/verify_companion_snapshot.py`
- `tests/test_pplx_ltx_co_residency.py`
- `tests/test_residency_runtime.py`
- `runner/test_reliability_contract.py`

Focused verification: `49 passed, 29 subtests passed`. The last repository-wide run returned `250 passed, 17 failed`; the two residency reliability failures from that run were reconciled and now pass in the focused suite. The other 15 failures were unrelated pre-existing coordinator, harness catalog, intake, static UI, known-character, H3 UI, and private-template work and were not rerun as a full suite after this narrow fix.

## Deployment and rollback gate

1. Confirm the live queue is empty and no external inference transaction owns `/run/user/1000/media-lab-inference.lock`.
2. Capture service/container/memory pre-state and exact runtime-file backups.
3. Deploy only the reviewed residency files; verify source/runtime hashes.
4. Restart `media-lab-simple.service` once.
5. Run `runner/verify_companion_snapshot.py`; require PPLX + LTX, no other loaded companion, and ≥24 GiB available.
6. Run the canary matrix one model at a time.
7. On any failed PPLX probe, OOM evidence, artifact failure, unload failure, or floor breach: stop, restore the exact backups, restart once, and re-run the read-only snapshot.
