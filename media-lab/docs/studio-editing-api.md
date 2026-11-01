# Independent editing drafts (development preview)

The paired independent host exposes `vibexStudio.editingDrafts: true` in its
manifest. This is an API capability, not a separate website or completed editor UI.
The controller uses the existing independent Cut transaction engine; it does not
import the legacy app, Maestro, WanGP, or a generation runtime.

Request editing explicitly through `POST /api/gate` with the host code,
`studio_edit: true`, and the device's stable 32-character hexadecimal
`studio_device`. The response adds `editScope: editing:own`, `editToken`, and
`editExpiresIn`. No existing Library or generation token gains editing rights.
Use `Authorization: Bearer <editToken>` for the following endpoints:

- `GET /api/studio/editing/projects`: list this device's drafts.
- `GET /api/studio/editing/projects/{project_id}`: read one owned draft.
- `POST /api/studio/editing/projects`: create from Library asset IDs. Supply
  `{requestId, title, assetIds}` and a separate
  `X-Library-Authorization: Bearer <libraryToken>` header.
- `POST /api/studio/editing/projects/{project_id}/transactions`: supply
  `{transactionId, revision, commands}`. Each command has `{id, type, payload}`.

Create requests are idempotent by request ID. Reusing the ID with different inputs
returns 409. Edit transactions use Cut's journal and expected revision; a stale
edit returns 409. Reuse the same transaction ID for a retry, never a new one until
the previous result is known. Drafts persist under the user's configured state
root and retain source hashes; source files are not changed.

Example trim command payload:
`{"clip_id":"the-clip-id","trim_in_frames":0,"trim_out_frames":48}`.
Allowed types are clip.remove, clip.trim, clip.split, clip.move, transition.set,
transition.remove, caption.add, caption.edit, caption.remove, audio.mix,
color.apply, undo and redo. Frame values use the project's frame rate. Rendering,
publication, approval, adding new sources to an existing draft and full UI/agent
integration remain separate unfinished work.

Current bounds: 8 initial source IDs, each at most 256 MB, 32 commands per
transaction and 64 KB of serialized commands. Media must live directly in the
configured media root because Cut's source contract uses basenames. FFprobe must
be installed to inspect sources. Only one draft preparation is admitted at a time;
contention returns 409 for retry. A probe timeout fails preparation. Existing POSIX
Cut file locking means Windows controller qualification remains unfinished.

Verified locally with an actual PNG/FFprobe create-and-trim request, retry,
revision conflict, restart persistence, unchanged original bytes, device isolation,
separate permissions, token expiration, malformed commands and prohibited actions.
Packaged source starts without legacy imports. This does not qualify video/audio
editing output, physical platforms, deployment or render/export UI.

## Verified CPU preview rendering

Hosts now advertise `vibexStudio.editingPreview: true`. With the same owned editing
permission, POST `/api/studio/editing/projects/{project_id}/preview` with
`{"revision": <saved revision>}`. This explicitly renders that revision on the
user's server. The response contains projectId, revision, bytes, sha256, seconds,
mimeType and candidate. It contains no host filesystem paths or public URL.
GET `/api/studio/editing/projects/{project_id}/previews/{revision}/content` with
editing authorization to download the verified MP4.

Completed previews are reused, including after restart. A retry during another
render returns 409; retry the same revision. A changed timeline requires refresh.
Only files with a matching persisted receipt and SHA256 are served. Rendering
uses CPU FFmpeg, a60-second process timeout, ffprobe plus a bounded full decode,
and duration verification. It does not publish or invoke a model/provider.

Current preview limits:60seconds,16clips,30fps,1280maximum dimension and720p pixel
area,256MB per source and64MB output. Video clips must be contiguous and
non-overlapping. Larger/full exports, gap rendering and playback UI are unfinished.

## High-quality MP4 export

Hosts advertise `vibexStudio.editingExport: true`. POST
`/api/studio/editing/projects/{project_id}/export` with the saved `revision` and
owned editing authorization. Export uses the timeline dimensions and frame rate,
CPU H.264 high-quality settings, audio mix and captions. It does not publish.
The verified receipt adds `quality: high`, `width`, `height` and `fps` to the
preview receipt fields. `candidate: true` remains explicit: rendering does not
approve creative content on the user's behalf.

GET `/api/studio/editing/projects/{project_id}/exports/{revision}/content` with
editing authorization. The MP4 has an attachment filename and no-store caching.
Exports and previews occupy separate revision directories and never substitute
for one another. The same byte/hash, full-decode and duration checks apply.
Completed exports survive restart and repeated requests reuse their receipt.

Current export bounds: 10 minutes, 128 clips, 60 fps, maximum dimension 1920,
1080p pixel area and 1 GB output. Sources still have the 256 MB import limit.
The render deadline is 15 minutes and verification decode deadline is 3 minutes.
Preview and export share one render admission slot. A competing request receives
409; retry the same revision. Video gaps/overlaps remain unsupported. This is a
synchronous endpoint: a disconnected caller can retry to retrieve a completed
receipt, but durable background-job progress and cancellation are not yet exposed.
Client download controls and physical-platform qualification remain unfinished.

## Add Library media to an existing draft

`vibexStudio.editingAddSources: true` advertises source insertion. Use the existing
transaction endpoint with a `clip.add` command whose payload includes `job_id`
(the Library asset ID). Both the editing bearer and `X-Library-Authorization`
are required, including retries. Up to eight additions are allowed in one
transaction. Sources are resolved only through the configured Library catalog,
probed and limited to supported media in the root folder under 256 MB.

Default placement appends to the matching video or music track. Existing Cut
payload options for track, frame and still duration remain available. Source
probing runs inside first application of the transaction; an accepted transaction
retry reuses its journal result without probing or inserting again. Undo removes
the inserted timeline references while preserving original Library files.

## Nonblocking export jobs

POST `/api/studio/editing/projects/{project_id}/export-jobs` with
`{"revision": 0}` and the owner's editing bearer token. A newly accepted render
returns HTTP 202 immediately. Repeating that revision returns its running job or
verified completed receipt without starting duplicate work. Another active render
returns 409; this host admits one editing render at a time and does not queue work.
The accepted timeline is an in-memory snapshot, so later edits do not alter it.

GET `/api/studio/editing/projects/{project_id}/export-jobs/{revision}` returns
`projectId`, `revision`, `state`, `receipt`, and a safe optional `message`.
States are `not-ready`, `running`, `ready`, `failed`, and `interrupted`. Only ready
has a verified receipt. Both endpoints require ownership and return no-store.
Status never starts rendering. Running records belonging to an earlier host
instance are reported interrupted; jobs do not automatically resume after a host
restart. Retrying requires that revision still be current unless its verified
export already exists. Failed jobs can be retried explicitly. Client disconnects
do not cancel an admitted render; existing render time and output limits apply.

These endpoints do not publish, copy into Library, or grant an external agent
permission to render. Agent editing consent alone remains insufficient for a
render tool. The older synchronous export endpoint remains available.

Studio agents can now use `start_editing_export` after the separate **Render and save my
video drafts** grant, and `read_editing_export_job` with Library-read consent.
Existing edit-only grants do not acquire render access. Neither tool publishes or
copies output; the human can save a verified export from the editor.

`save_editing_export_to_library` uses that explicit rendering-and-saving grant to
copy a verified export of at most 256 MiB into the connected server Library. The
content-based asset ID makes repeat saves idempotent. New exports are organized in
Videos/Edits. This does not publish or transfer media to Studio. Separately approved
`import_media_asset` can copy Library assets of at most 16 MiB into project files.
