# Cut — the Media Lab editor

Cut is the timeline editor inside Media Lab. Every video, picture or song in the
gallery can be opened on a timeline (`✂️ Cut` on any gallery tile, or `/cut`),
trimmed, split, moved, dissolved, captioned, mixed, colour-graded and rendered —
by a person on a phone, or by an agent over HTTP / the CLI. A finished render goes
straight back into the gallery as a new item, so it can be re-cut.

Everything a person can do, an agent can do, through the same store. The only thing
agents cannot do by themselves is *apply* a Sparky proposal or approve a master:
those are one tap for a person.

```
static/cut.html / cut.css / cut.js    the UI (vanilla, no build step)
media_lab_core/cut.py                 manifest, commands, journalled store, ffmpeg planner
media_lab_core/cut_cli.py             the CLI  (tools/cut wrapper)
app.py  /api/cut/...  /cut            HTTP routes and the render thread
chat_operator.py  inspect_cut, cut_propose   Sparky's typed tools (proposal-only)
```

## The manifest

Schema `media_lab.editing_project.v2` (`schema_version: 2`), one per project at
`~/media-lab-simple/cut/projects/<project_id>/project.json` with an append-only,
fsync'd `journal.jsonl` next to it. Every field below is stable-ID addressed.

```jsonc
{
  "schema": "media_lab.editing_project.v2", "schema_version": 2,
  "project_id": "cut-3f9a1c0b2d", "title": "Beach day", "revision": 4,
  "source":   { "kind": "gallery_import", "path": "gallery:1a2b3c,4d5e6f", "sha256": "…", "immutable": true, "job_ids": ["1a2b3c","4d5e6f"] },
  "settings": { "width": 1280, "height": 720, "fps": 24, "timebase": "1/24", "still_seconds": 4.0 },
  "duration_frames": 216, "duration_seconds": 9.0,
  "assets": [ { "id": "asset-1a2b3c", "kind": "video", "job_id": "1a2b3c", "title": "Take A",
                "source": { "path": "/media/1a2b3c.mp4", "sha256": "…", "immutable": true, "exists": true },
                "poster": "/media/1a2b3c.jpg", "duration_seconds": 5.0, "width": 1280, "height": 720, "fps": 24.0, "has_audio": true } ],
  "storyboard": { "scenes": [ /* one scene per gallery item; candidates hold the asset */ ] },
  "candidates": [ /* candidate-<job>-a … media_state ready|missing */ ],
  "timeline": {
    "tracks": [
      { "id": "track-video-main",    "name": "V1",       "type": "video",    "clips": [
        { "id": "clip-1a2b3c-main", "label": "Take A", "asset_id": "asset-1a2b3c", "media_kind": "video",
          "start_frame": 0, "duration_frames": 96, "trim_in_frame": 12, "trim_out_frame": 108,
          "source_duration_frames": 120, "audio": { "linked": true, "gain_db": 0.0, "muted": false },
          "scene_id": "scene-1a2b3c", "candidate_id": "candidate-1a2b3c-a", "effects": [], "approval_state": "candidate" } ] },
      { "id": "track-music-main",    "name": "Music",    "type": "music",    "clips": [] },
      { "id": "track-captions-main", "name": "Captions", "type": "captions", "clips": [] } ],
    "transitions": [ { "id": "transition-clip-a-clip-b", "from_clip_id": "clip-a", "to_clip_id": "clip-b", "kind": "dissolve", "duration_frames": 12 } ],
    "captions": { "items": [ { "id": "caption-0-1f2e3d4c", "text": "Hello", "start_frame": 0, "end_frame": 48, "approval_state": "needs_review" } ], "style": "media-lab-readable-v1" },
    "color": { "clip-1a2b3c-main": { "preset": "warm", "intensity": 0.6 } },
    "mix": { "dialogue": { "normalize": false, "target_lufs": -16.0, "gain_db": 0.0 }, "music": { "gain_db": -12.0 }, "master": { "gain_db": 0.0 } }
  },
  "approval": { "candidate_not_final": true, "publication_authorized": false, "master_render_approved": false, "…": "…" },
  "history_state": { "undo_stack": ["tx-1"], "redo_stack": [] },
  "version_history": [ … ], "render_plans": [ … ], "media_status": { "state": "ready", "missing_asset_count": 0 }
}
```

Rules the store enforces (`validate_manifest`): unique stable ids, no dangling
references, `trim_out − trim_in == duration`, asset sources are basename-only
`/media/<file>` with a SHA-256, original asset records are never mutated by a
command (new ones may be appended), the project stays a private unpublished
candidate, and a master render plan needs explicit approval.

**Units are frames** at `settings.fps`. `start_frame` is where a clip sits on the
timeline; `trim_in_frame`/`trim_out_frame` are where it reads from inside its
source. A picture becomes a `still_seconds` (4 s) clip and may be trimmed to any
length (`source_duration_frames` is `null`). A video clip's own sound is *linked*:
it follows the clip's trims and is controlled by `clip.audio` and the `dialogue`
bus. Songs live on the Music track and are mixed under everything.

Dissolves overlap the two clips by their length (xfade), so the rendered video is
shorter than the timeline by each transition's duration; `render_duration_frames`
and `timeline_to_render_frame` do that arithmetic and captions/music are placed in
rendered time.

## Commands

A transaction is `{commands: [{id, type, payload}, …], transaction_id, expected_revision}`.
The batch validates and applies atomically, the journal is appended and fsync'd,
then `project.json` is atomically replaced. `expected_revision` must equal the
project's current revision or the call fails with `revision conflict`; the same
`transaction_id` with the same payload is idempotent, with a different payload it is
a `transaction id collision`. `undo`/`redo`/`restore` must be the only command in
their transaction.

| type | payload | what it does |
|---|---|---|
| `clip.add` | `{job_id, track_id?, start_frame?, duration_seconds?, clip_id?}` | import a gallery item as a new asset + clip (end of the track by default; pictures get `duration_seconds`, default 4) |
| `clip.remove` | `{clip_id}` | drop a clip (and its transitions / look) |
| `clip.trim` | `{clip_id, trim_in_frames, trim_out_frames}` | set source in/out; `out ≤ source length` for video |
| `clip.split` | `{clip_id, at_frame, new_clip_id?}` | split at an **absolute timeline frame** inside the clip |
| `clip.move` | `{clip_id, track_id, start_frame}` | place a clip; videos/pictures only on video tracks, songs only on music |
| `transition.set` | `{from_clip_id, to_clip_id, kind?, duration_frames}` | dissolve between two **touching** V1 clips; kinds `dissolve fadeblack fadewhite wipeleft wiperight slideleft slideright` |
| `transition.remove` | `{from_clip_id, to_clip_id}` | remove it |
| `caption.add` | `{text, start_frame, end_frame, caption_id?}` | a caption card |
| `caption.edit` | `{caption_id, text, start_frame?, end_frame?}` | change it |
| `caption.remove` | `{caption_id}` | remove it |
| `caption.generate` | `{scene_id, text?}` | one caption spanning the scene's clip (storyboard narration, or the gallery title) |
| `audio.mix` | `{target: dialogue\|music\|master, gain_db?, normalize?, target_lufs?}` or `{target: "clip", clip_id, gain_db?, muted?}` | buses (−60…+24 dB) or one clip's own sound |
| `color.apply` | `{clip_id, preset, intensity?}` | `neutral warm cool punchy soft bw vintage dramatic`, intensity 0…1 |
| `scene.replace` | `{scene_id, candidate_id}` | switch a scene's active candidate (storyboard projects) |
| `project.approve` | `{master_render_approved: bool}` | people only; unlocks `master` renders |
| `render.preview` / `render.master` | export settings | record a render *plan* in the manifest (the render route executes) |
| `undo` / `redo` | `{}` | walk the history (new journal entries, history is never rewritten) |
| `restore` | `{revision}` | jump back to an applied revision |
| `diff` | `{}` | no-op transaction |

Example transaction:

```json
{ "transaction_id": "agent-7f3c", "expected_revision": 4, "commands": [
  { "id": "c1", "type": "clip.trim", "payload": { "clip_id": "clip-1a2b3c-main", "trim_in_frames": 12, "trim_out_frames": 108 } },
  { "id": "c2", "type": "clip.split", "payload": { "clip_id": "clip-1a2b3c-main", "at_frame": 48 } },
  { "id": "c3", "type": "transition.set", "payload": { "from_clip_id": "clip-1a2b3c-main", "to_clip_id": "clip-1a2b3c-main-split-48", "kind": "dissolve", "duration_frames": 12 } },
  { "id": "c4", "type": "caption.add", "payload": { "text": "Summer, finally", "start_frame": 0, "end_frame": 60 } },
  { "id": "c5", "type": "color.apply", "payload": { "clip_id": "clip-1a2b3c-main", "preset": "warm", "intensity": 0.5 } },
  { "id": "c6", "type": "audio.mix", "payload": { "target": "music", "gain_db": -14 } } ] }
```

The answer carries `status` (`applied` / `proposed` / `superseded`), the new
`revision`, per-command `outputs` (e.g. the `new_clip_id` of a split) and an exact
`diff` (`[{op, path, before, after}]`).

## HTTP API

All routes sit behind the studio gate: a signed session cookie from
`POST /api/gate {"code": …}` (either door code), or a request from localhost/tailnet.

| route | body | notes |
|---|---|---|
| `GET  /api/cut/projects` | | summaries (id, title, revision, clips, poster) |
| `POST /api/cut/projects` | `{job_ids:[…], name?, still_seconds?}` or `{storyboard: "<file inside MEDIA_LAB_CUT_STORYBOARD_DIR>"}` | creates and returns `{project_id, project}` |
| `GET  /api/cut/projects/{id}` | | the manifest |
| `GET  /api/cut/projects/{id}/pending` | | Sparky proposals on the current revision, each with its diff |
| `GET  /api/cut/projects/{id}/renders` | | render records (live and finished) |
| `POST /api/cut/projects/{id}/commands` | transaction | applies as `human` |
| `POST /api/cut/projects/{id}/sparky/commands` | transaction + header `X-Media-Lab-Sparky-Token` | **proposal only**; the token is HMAC(access secret, `cut-sparky-v1`) and lives only in process memory |
| `POST /api/cut/projects/{id}/review/{transaction_id}` | `{approve: bool}` | applies or rejects that exact proposal; a proposal made on an older revision is `superseded` |
| `POST /api/cut/projects/{id}/render` | `{quality: preview\|medium\|high\|master, format: mp4\|webm, include_audio, burn_captions, range_mode, range_start_seconds, range_end_seconds, explicit_approval, import_to_gallery}` | starts a CPU render thread → `{render_id}`; `master` needs `project.approve` **and** `explicit_approval: true` |
| `GET  /api/cut/renders/{render_id}` | | `{status: queued\|running\|done\|error, stage, progress 0…1, receipt, url, gallery_job_id}` |
| `GET  /cut` / `GET /cut?project=<id>` | | the editor page |

Errors are `{error: "…"}` with 400 (bad request), 403 (not allowed), 404 (unknown),
409 (revision conflict / command refused). The store never applies half a batch.

The render receipt (`media_lab.render_receipt.v1`) records the exact ffmpeg argv,
exit code, bytes, SHA-256, ffprobe result, sources, caption mode
(`burned` cards / `embedded` mov_text / `sidecar` .srt), project revision and the
manifest hash. Renders use `-threads 1`, `+bitexact`, `-map_metadata -1` and a fixed
creation time, so the same manifest renders to the same bytes.

## CLI

`tools/cut …` or `python -m media_lab_core.cut_cli …`. Defaults to
`http://127.0.0.1:7863` (`--url` / `MEDIA_LAB_URL`), signs in with `--code` or
`MEDIA_LAB_CODE` when the studio asks for one, prints raw JSON with `--json`.

```
cut projects
cut new --job 1a2b3c --job 4d5e6f --name "Beach day"      # gallery ids → project
cut show <project>
cut add <project> --job 9e8d7c [--seconds 3]                # another gallery item at the end
cut trim <project> <clip> --in 12 --out 108
cut split <project> <clip> --at 48
cut move <project> <clip> --start 0 [--track track-video-main]
cut remove <project> <clip>
cut transition <project> <from-clip> <to-clip> --kind dissolve --frames 12 [--clear]
cut caption <project> add --text "Summer" --start 0 --end 60
cut caption <project> edit <caption-id> --text "…" | cut caption <project> remove <caption-id>
cut caption <project> generate --scene scene-1a2b3c [--text "…"]
cut mix <project> --target music --gain -14 | --target dialogue --normalize true --lufs -16 | --target clip --clip <clip> --mute true
cut color <project> <clip> --preset warm --intensity 0.5
cut apply <project> --json '[{"type":"clip.trim","payload":{…}}, …]'
cut undo|redo|diff <project> ; cut restore <project> --revision 2
cut pending <project> ; cut approve <project> <transaction> ; cut reject <project> <transaction>
cut approve-master <project> [--revoke]
cut render <project> --quality preview [--format webm] [--range 1.5:6] [--no-captions] [--wait]
cut render <project> --quality master --explicit-approval --wait
cut status <render-id>
```

## How an agent edits a video

1. **Find the material.** `GET /api/gallery` (or the job id the user pointed at).
2. **Make a project.** `cut new --job <id> --json` → note `project_id`, `settings.fps`
   and the clip ids on `track-video-main`. `cut show` prints the timeline in
   seconds and frames.
3. **Plan in frames.** Read `duration_frames`, `trim_in/out_frame` and
   `source_duration_frames`; every edit is frame-addressed, so compute
   `seconds × fps` yourself.
4. **Send one transaction per intention.** `cut apply <project> --json '[…]'` sends
   the batch with the current revision; the answer's `outputs` give you the new ids
   (a split's `new_clip_id`) for the next batch. A `revision conflict` means
   someone else edited — re-read and retry.
5. **Check, then render.** `cut show`, then `cut render <project> --quality preview --wait`.
   The record's `url` and `gallery_job_id` are the finished file, now a gallery item.
   Hand the user `/cut?project=<id>` to review and `/?job=<gallery_job_id>` to watch.
6. **Master is theirs.** Only a person runs `cut approve-master` and then
   `cut render --quality master --explicit-approval`.

Sparky (the in-app chat) does the same through `inspect_cut` and `cut_propose`,
except its transactions are proposals: they appear in the Cut page as an exact
diff with **Approve / Reject**, and never change the project until a person taps.

## Deploy notes

`static/sw.js` `CACHE` must be bumped whenever `cut.*` change (the studio's deploy
rule). Set `MEDIA_LAB_CUT_STORYBOARD_DIR` only if you want storyboard imports; the
default project source is the gallery. `MEDIA_LAB_DISABLE_BACKGROUND_WORKERS=1` with a
throw-away `HOME` runs the API without the GPU queue, for tests and CLI drives.
