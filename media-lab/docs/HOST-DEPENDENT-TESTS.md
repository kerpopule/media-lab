# Host-dependent and environment-dependent tests

This repository's suite is meant to run to completion on any checkout — a
laptop, a CI runner, or the studio host — and to say out loud what it could not
cover. Two things make that true:

* `tests/conftest.py` skips cases that read the Spark host's private trees
  (`productions/`, `image-svc/`), naming the capability in the skip reason.
* `pytest -rs` prints those reasons, and `pytest -m "not spark"` excludes the
  cases that need a live engine on that host.

## How to run it

```bash
cd media-lab
python -m pytest -q -rs -m "not spark"     # everything a plain checkout can run
python -m pytest -q -rs -m spark           # only the host-only cases (12)
```

Use `-m`, not `-k`. `-k "not spark"` matches the marker *and* every test id
containing the word "spark", which silently drops 13 runnable cases (Sparky
proposal review, deploy-tag policy, RDMA two-Spark qualification, Maestro H3
safety ceilings). `-m` excludes only what actually requires the host.

## What is excluded, and what it needs

Marker `spark` — needs the studio host (Spark 1) or a live engine on it:

| Case | Needs |
| --- | --- |
| `test_pplx_ltx_co_residency.py` (1 case) | the private `image-svc/` tree |
| `test_yue2_music.py::test_live_yue2_shim_health` | a live YuE2 engine (`YUE2_PORT`) |
| `test_sol_h3_runtime_patch.py::test_probe_reproduces_the_stop_and_the_patch_plateaus` (2 cases) | the Sol-H3 stage-1 environment and `SOL_PKG` (CPU only; no GPU) |

Marker `gpu` — needs a resident GPU engine on the machine running the suite.
No tracked test requires it today: the engine boundary is mocked in
`tests/test_storyboard_retry_recovery.py`, `tests/test_h3_ltx_two_stage.py` and
friends. The marker exists so a future case can be excluded without inventing a
new convention.

Skipped by the test itself, with the reason in the log (`-rs`):

| Case | Needs |
| --- | --- |
| `test_triposr_conversion.py` | a qualified torch/safetensors interpreter |
| `test_background_install.py` (3 cases) | macOS arm64 or Linux aarch64 — `media_lab_core/background_install.py` refuses any other platform |
| `test_lease_owner_probe.py` (2 cases) | `/proc/locks` (Linux) |
| `test_solh3_control_guard.py::test_terminate_cgroup_stops_a_real_synthetic_member` | `os.pidfd_open` (Linux) |
| every Cut media case | `ffmpeg` + `ffprobe` on `PATH` |

`.github/workflows/media-lab-suite.yml` installs `ffmpeg` before it runs the
suite, because a GitHub ubuntu runner does not ship it: without it 61 cases
skipped and 25 failed on real rendered media. The job also fails loudly if
either binary is missing, so a runner image change cannot silently downgrade
the run into a green-but-shallow one. It runs on `ubuntu-24.04-arm` for the same
reason it excludes the studio host: Media Lab ships for macOS arm64 and Linux
aarch64, and on x86_64 the three `background_install` cases can only fail.

A run is only fully green *for the capabilities it had*. Read the skip list
before claiming a suite covered the studio host.

## Cases that look host-dependent but are not

Three cases used to fail on a plain checkout. None of them needed the Spark, a
GPU, or any engine: each read a tracked, CPU-only input and was wrong about it.
They are fixed rather than marked, because marking them would have hidden real
defects behind a skip.

* `test_cut_api.py::test_cut_end_to_end_over_http` — the studio resolver handed
  Cut a bare poster basename where `docs/CUT.md` specifies a `/media/<file>`
  reference, so *every* gallery project with a poster failed closed with
  `asset source must be a /media/ path`. Fixed in `app.py::_cut_gallery_item`;
  the test now also asserts the poster reference and that the player can fetch
  it.
* `test_storyboard_retry_recovery.py::test_h3_memory_floor_is_fail_closed` — the
  fixture carried the literal `117.9` as "below the admission floor", written
  when the floor was 118 GiB. The approved boundary is 117 GiB (115 GiB decode
  + 2 GiB operating floor, `config/model-residency-policy.json`), so the literal
  no longer meant what it said. The fixture now derives the boundary from the
  live policy and a companion test pins the boundary itself.
* `test_triposr_runtime.py::test_rebuilt_profile_requires_exact_five_wheels_and_preserves_reduced` —
  the `rebuilt-rust-v1` build manifest pinned three evidence records
  (`docs/triposr-*-rebuild-review.json`) that were later redacted to `~/` for
  the identity guard (commit `324a98b`), so the pins never matched the tracked
  files. The manifest now pins the tracked artifacts.

## Keeping the contract honest

* Never add a bare `pytest.mark.spark`: state the capability in a comment and,
  if it is a whole module, add the module → reason entry in `tests/conftest.py`.
* Never "fix" a red test by marking it; check whether the input it reads is
  tracked. If it is, the test or the code is wrong, and the difference matters.
* Never pin a fixture to a number that a reviewed config already owns. Read the
  config.