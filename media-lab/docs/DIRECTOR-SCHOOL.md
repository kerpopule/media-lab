# Director school

A video model films one shot at a time and forgets the last one. A film only
reads as one film when everything that must match is decided before the first
render, written into every prompt, and checked at every cut after the edit.
This is the workflow Media Lab's director (and any agent driving it, such as
Sparky) follows.

The code: `media_lab_core/director_school.py` (plan and exam),
`media_lab_core/stitch.py` (the editor), `media_lab_core/seam_critic.py`
(the critic), `media_lab_core/director_cli.py` (`tools/director`).

## Why it exists

The 2026-09-18 "Velvet Melon" diner cut is the reference failure. Four H3 takes
of "the same booth" came back as four different diners with about twenty
different faces; every cut was a half-second dissolve; one take opened with a
second of a different shot; the dialogue take was 19 dB quieter than its
neighbours; and the Cut *preview* render (CRF 30, 0.75 Mbit/s) was handed over
as the film. The causes, in order:

1. Every prompt opened with the whole six-person booth, so H3 painted six
   people in every shot and ignored the two-shot framing at the end.
2. Nothing tied the takes together: no start frame, no reference picture, no
   shared seed. Each take invented new people and a new room.
3. The studio wrapped the already-formatted H3 prompt a second time
   (`integrated_multimodal_description: [Shot 1] integrated_multimodal_description: ...`,
   two soundscape and two music fields). Fixed: `h3_prompt` now passes a
   composed schema through.
4. Two speakers and up to six lines in five seconds.
5. The edit had no dead-frame trim, no level matching, no motivated transitions,
   and a draft-quality encode.

## The workflow

1. **Bible.** One per production. `style`, `palette`, `time_of_day`,
   `world`/location, `camera`/lens language, and for every character a `look`,
   a `wardrobe`, a `voice` and, when identity matters, a `reference` picture.
   These are pasted into every shot; nothing is left to the model's memory.
2. **Shot list with coverage.** Each beat has `scene`, `shot_size`
   (EWS, WS, FS, MWS, MS, MCU, CU, ECU, INSERT, OTS, TWO), `angle`,
   `screen_side` (who is left and right of frame), `characters`, `speaker`,
   one line of dialogue at most, and the `transition` into it.
3. **Exam** (`GET /api/storyboard/{id}/exam`, `tools/director exam`). Errors
   stop the production before GPU time is spent:
   - more than two faces in a shot that is not wide;
   - more than one speaker in a short shot, or a line longer than about
     2.5 words per second of the shot (minus a breath at each end);
   - a character jumping sides of the frame (the 180-degree rule);
   - format words (video, clip, montage...) that the model paints as panels.
   Warnings: a cut between the same people at nearly the same size and angle
   (jump cut), a dissolve inside one continuous scene, lettering the model
   would have to paint, a close-up of a named person with no reference or start
   frame, no establishing wide, a silent shot over 8 seconds, gaps in the bible.
4. **Routing and estimate.** `route_engine` picks the engine per shot and
   estimates wall-clock time with spin-up included:
   - `sol-t2va` (Sol-H3, the default, about 75 s warm) for shots where no
     identity is at stake;
   - `sol-fl2va` (Sol-H3 from a start frame, about 5 minutes to switch the
     first time) for identity-critical shots;
   - `real-long` (H3 Singularity, "Real / Long") for identity-critical shots
     with reference pictures and for shots longer than Sol's 5 seconds, when
     it is installed. Measured: about 285 s warm per 5 s, 490 s with
     references, plus about 460 s cold load.
5. **Start frames.** The scene is built in the still, not in the video prompt:
   an establishing master, then every other still is an edit of that master
   (same set, same light) anchored on the character's reference picture (same
   face). The video prompt then carries motion, dialogue and the constants.
6. **Takes.** `compose_h3_prompt` writes the official three-field H3 prompt:
   shot size first, the bible constants, the characters' looks and wardrobe,
   screen sides, the action, `(S1) says <d>[English] ...</d>`, eyelines ("never
   into the camera"), a closed-mouth end state, and "no lettering". One seed
   for every take.
7. **Stitch** (below).
8. **Critic** (below). Shots it rejects are re-rendered with a new seed and
   the critic's reasons written into the prompt, at most twice. A shot with an
   open re-render verdict is not delivered without a person saying so.

`tools/director produce BOARD.json --out DIR` runs steps 3 to 8 against a
studio and leaves every still, take, cut, receipt and critique in `DIR`.

## Transitions

| Transition | When | What the editor does |
|---|---|---|
| `cut` | the default | picture cut; ~0.1 s equal-power audio crossfade |
| `cut_on_action` | a movement carries across | picture cut inside the move |
| `match_cut` | shapes or motion rhyme | picture cut; never flagged as a jump cut |
| `j_cut` | the next shot opens on a reply | the next shot's sound starts 8 frames early |
| `l_cut` | a line finishes over a reaction | the outgoing sound runs 8 frames on |
| `dissolve` | time passes or the place changes | 12-frame picture and audio crossfade |
| `fade_through_black` | a large time jump, or the end | 16-frame dip to black |
| `smash_cut` | a deliberate jolt | hard picture and near-hard sound |

`choose_transition` picks one with a reason when the board does not say.

## The stitcher (`media_lab_core/stitch.py`)

The storyboard assembler (`POST /api/storyboard/{id}/assemble`) uses it by
default. For every shot it:

- trims dead frames: black frames, a frozen or "settling" hold at the tail
  (keeping 0.3 s, and never cutting into the last word), and an unrequested
  cut in the first or last 1.5 s (a different shot flashing in). A cut in the
  middle of a take is reported for re-render; the editor cannot hide it.
  Explicit trims are honoured exactly and switch auto-trim off for that shot;
- keeps the takes' own canvas when they all share one (H3's 1344x768 is no
  longer padded into 1280x704 with black slivers), crops rather than pads a
  near-matching aspect, and resamples everything to one frame rate;
- pulls each shot part of the way to its scene's median colour (per-channel
  gain and offset, clamped; a shot far outside its scene is left alone as a
  deliberate look);
- measures each shot's loudness (EBU R128) and levels it (dialogue to about
  -20 LUFS, ambience left in its bed), then a two-pass linear loudnorm puts
  the mix at -16 LUFS integrated, -1.5 dBTP;
- crossfades the sound at every seam, makes J and L cuts by trimming the
  picture where a generated take has no audio handles, and dips in and out by
  12 ms where there is no room at all (no clicks);
- with a song as the soundtrack: beat-tracks it and moves each hard cut onto
  the nearest beat when the shots have the frames to allow it; the song fades
  out instead of stopping;
- encodes once, from the original takes, at CRF 18 (`high`) or 16
  (`master`). `preview` (CRF 26) is labelled as a draft in the receipt.

Everything it decided is in `jobs/<assembly job>/assembly-receipt.json` and
summarised on the board as `assembly` (trims and why, beat alignment, notes,
the critic's verdict). `MEDIA_LAB_ASSEMBLY_ENGINE=legacy` restores the old
plain concat; `MEDIA_LAB_ASSEMBLY_QUALITY=master` raises the encode.

## The critic (`media_lab_core/seam_critic.py`)

Measured at every seam, always: colour difference of the frames either side
(CIE76), brightness jump, framing similarity across a hard cut (a jump cut),
sound level either side of the audio edit, a click at the edit, dead air.
Across the film: loudness, frozen stretches, cuts inside takes, shot lengths.

Looked at, when a local vision model answers: the before/after pair and the
bible go to the model, which answers in JSON (same people, clothes, set, light,
screen direction, looking into the lens, garbled lettering, melted faces,
fingers) with a verdict per side. The studio's own companion
(`MEDIA_LAB_VISION_URL`, the Spark 2 bridge) is tried first, but only after a
one-picture probe: an engine that silently drops images fails it, and the
report then says "vision check did not run" rather than passing.
`MEDIA_LAB_CRITIC_VISION_URL` / `_MODEL` point the look at another local
model; `MEDIA_LAB_CRITIC_VISION=off` skips it. An agent that can see (for
example Sparky with a vision tool) can look at `seams/seamNN-pair.jpg` itself
and hand its answers to `tools/director critic ... --verdicts answers.json`.

Lip-sync, when the host has a LatentSync checkout (`MEDIA_LAB_SYNCNET_PYTHON`,
`MEDIA_LAB_LATENTSYNC_ROOT`): every shot that speaks is measured with SyncNet
on the CPU (`runner/syncnet_measure.py`). In sync means an offset of at most
one frame at confidence 5 or more (the private delivery gate's bar); three
frames or more, or confidence under 2.5, is a re-render.

Output: `critic.json`, `critic.md`, the seam frames, and on the job
`critic.summary` / `critic.rerender`.

## Commands

```
tools/director exam board.json
tools/director plan board.json [--real-long]
tools/director prompts board.json
tools/director cut board.json out.mp4 --clips s1.mp4 s2.mp4 ...
tools/director critic out.mp4 out.receipt.json --frames seams/ --board board.json
tools/director produce board.json --out prod/ [--rounds 2] [--seed 4242]
```

With `--real-long` (the studio has the Real / Long engine), every shot whose
characters have reference pictures films on Real / Long: its start frame is the
opening-frame picture and each visible character's reference picture holds that
face for the whole take. All stills are made before the first take and a
re-take keeps its start frame, so the studio loads the video engine once. Real /
Long has no per-load prompt limit, so `--takes-per-load` does not apply to it.

`produce` signs in with the studio's local token on the studio machine, or
with `MEDIA_LAB_CODE` elsewhere. It never publishes.

## Sol-H3: the old two-different-takes-per-load limit (fixed 2026-09-27)

On a host where `tools/sol-h3-runtime-patch.py status` says `patched` (and the
engine's `/health` says `"stage1_geometry": "patched"`), one warm Sol load films
any number of different prompts; `--takes-per-load` is no longer needed there.
The history below explains the stop and still applies to an unpatched host.

Measured 2026-09-26: the Sol-H3 stage-1 transformer is compiled with
`fullgraph=True` and a recompile limit of 16 (set in the vendored FastVideo),
and its sparse-attention tile buffer guard recompiles for every new text length.
A warm engine process films its warm-up and two different prompts; the third
different prompt fails with `FailOnRecompileLimitHit`, which writes the sticky
`safety-stop.json` and a recovery hold that needs an operator. The 09-18 diner
session hit the same stop. No Sol-H3 process on record has completed three
renders.

Cause and fix: `docs/SOL-H3-SPARK.md`, "Different prompts on one warm load".
On an unpatched host,
`tools/director produce --takes-per-load 2` films in batches: each batch's start
frames are image jobs, which make the studio reload H3 fresh before the batch's
takes. The storyboard and the queue do not batch by themselves; a person or an
agent queueing a third different H3 take on the same load will trip the stop.

## Limits, honestly

- Colour matching corrects drift, not a different room. Continuity of set and
  faces comes from the start frames and references, not from the edit.
- The vision look is only as good as the local model that answers it; the
  measured checks are the floor, not the ceiling.
- Beat alignment moves a cut by at most half a beat and only into frames the
  take actually has.
- Sol-H3 shots are 5 seconds. Longer single takes need Real/Long.
