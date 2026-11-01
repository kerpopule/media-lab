# Screenshot songs

**Exact Auto-fit** is the default. Reviewed screenshot text is immutable: every word must be sung exactly once, in order, with no paraphrase.

## Automatic planning

- Each part is capped at 60 words, and exact text is reflowed into short
  seven-word melodic phrases without changing or reordering any word.
- Long submissions split into the fewest balanced parts.
- Normal screenshots stay whole and in order.
- Each part is its own `screenshotsong` queue job and video output.

## Auto duration

With **Auto** selected, each planned part receives its own runtime ceiling from its exact word count and scene count (as short as 30 seconds for a tiny lyric; up to 180 seconds). Adding or removing reviewed text recalculates the estimate immediately. Music 3 may end naturally before that ceiling; the rendered duration drives screenshot timing, so the audio is never stretched just to fill a slider value.

## Director gate and queue visibility

Each part must pass both local word-order ASR QA and a CPU pitch-movement test before it is published. Exact Auto-fit allows no more than one isolated ASR uncertainty per part; two missing/substituted words, repeated runs, confident additions, prompt leakage, or recitative/spoken delivery trigger a retry or rejection. A passing part creates both a `screenshotsong` queue record and a `screenshotmusicvideo` child record.

A submission with more than one included screenshot creates exactly one editable group Storyboard immediately—even when Auto-fit splits it into several one-screenshot songs. Every screenshot becomes a beat with its exact text and estimated timing; every split song and its screenshot-video child share the same `board_id`, so **Open storyboard** is available from queue history and the Music gallery on every paired Media Lab surface. Completed multi-scene jobs from other Media Lab workflows use the same backend invariant, while one-shot jobs stay one-shot.

**Force one exact song** bypasses automatic splitting. It still preserves the text and must pass the same melody gate, so long prose that forces read-like delivery is rejected; Exact Auto-fit is the recommended default.
