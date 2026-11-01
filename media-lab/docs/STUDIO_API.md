# Studio generation bridge (in development)

This bridge uses its own SQLite queue and device-scoped bearer tickets. It does
not dispatch to the legacy JSON queue, Maestro, WanGP or a paid provider. The
current application exposes **no qualified engines** and refuses new generation
with HTTP 503. A production worker and client integration are still required.

## Permission and reconnection

POST `/api/gate` with the family code (or the admin code), `studio_render: true` and
`studio_device`, a client-generated 32-character lowercase hexadecimal identity.
Keep that identity stable in the device's secret storage for this server. A new
identity cannot recover the old identity's jobs. The response contains
`scope: "jobs:own"`, `token` and `expiresIn`. It does not issue a session cookie.

When `studio_library: true` is also requested, the existing library response
remains unchanged; generation permission is returned separately as `renderToken`,
`renderScope` and `renderExpiresIn`. Library tickets cannot generate or inspect
jobs. Render tickets cannot read the shared library or authenticate legacy/admin
routes. Changing the server signing secret or rotating the code that issued a
ticket (`media-lab code --rotate`) revokes it. Tickets from the family studio
(app.py) last a year; the independent host keeps 30 days.
Renewing a ticket for the same device retains its job ownership.

Use `Authorization: Bearer <token>` on all bridge routes. Cookies and trusted
hostnames cannot replace the scoped ticket. Responses prohibit shared caching;
browser CORS permits explicit authorization headers without cookie credentials.

## Submission and recovery

GET `/api/studio/engines` returns version 1 and the adapter host's engine list.
An empty list means generation is unavailable, not permission to use a fallback.

POST `/api/studio/jobs` accepts:

```json
{
  "requestId": "client-persisted-request-identifier",
  "engineId": "explicit-engine-id",
  "revision": "exact-qualified-revision",
  "kind": "image",
  "prompt": "A game badge",
  "settings": {"seed": 3}
}
```

Persist the request ID and exact request before submitting. Matching retries
return the same job, including after a server restart or adapter outage. Changing
the request under that ID returns 409. Reference images belong in the library;
the request is limited to 32 KiB and must not embed media. Supported kind labels
are image, video, audio, model and sprites; a label does not establish that an
engine for that kind is installed.

GET `/api/studio/jobs/{id}` returns only the authenticated device's job ID, kind,
status and timestamps. Other owners and unknown IDs both return 404. Internal
payloads and diagnostic paths are excluded. This route supports direct recovery
of a tracked job without searching a limited global history page.

## Artifact publication contract

A worker must publish accepted output inside
`studio-artifacts/{job-id}/{filename}` and then complete the job transaction with
`result.artifact` containing relative `path`, exact `bytes` and lowercase
`sha256`. Its qualification must include decode and technical QA before success.
The API does not perform creative approval or invent a QA receipt.

GET `/api/studio/jobs/{id}/content` requires ownership and successful completion.
It verifies path containment, supported extension, byte count and SHA-256 before
serving bytes. Missing/changed outputs return 409; no caller-supplied path is
accepted. Published files must remain immutable. The adapter host must publish
atomically and must not modify an accepted file while a download is in progress.

## Work still required

- A qualified independent adapter host, single GPU lease and durable worker.
- Explicit generation consent and secret storage in the app.
- Persisted client submissions, scoped polling and artifact downloads.
- Cancellation, worker crash reconciliation, quotas and artifact retention.
- Real hardware inference, Mac-off remote operation and release testing.

The HTTP tests use a controlled test admission callback and synthetic image
bytes. They prove permission, request-retry and artifact boundaries; they do not
prove model quality, hardware compatibility or a working production renderer.

### Recovering this device's job history

`GET /api/studio/jobs?limit=50&before=<job-id>` requires the render ticket
(`jobs:own`), and works even when no engine is online. It returns
`{version: 1, jobs: [...], nextCursor: string | null}` with the same bounded
public metadata as individual job status. Page size is 1–100. Use the returned
cursor unchanged; it must identify a job owned by the paired device. Results
are ordered by creation time and job ID, newest first, so equal timestamps do
not lose entries between pages. Newer submissions do not shift later pages.

This enables recovery without a locally saved list of job IDs. It does not
recover a lost pairing/device identity, grant access to other devices, expose
legacy jobs, or publish outputs to the shared gallery. Content still requires
the owned job's authenticated content route and integrity validation. The app Library displays this history separately from locally saved requests,
with refresh and older-page loading. Completed PNG image results can be imported
through the existing size, MIME and SHA-256 checks. Other job kinds remain
visible as status entries until their import contracts are implemented.


`GET /api/studio/jobs/<id>/preview` uses the same render-ticket ownership,
succeeded-state, publication-path and stored-content SHA-256 checks as download.
Supported PNG/JPEG/WebP inputs are bounded to20MiB/16megapixels and resized to
480×320 maximum, preserving transparency. At most two decodes run concurrently.
The app requests previews only when selected, limits responses to1MiB, and offers
a retry on failure. Tokens remain in headers. A preview does not grant gallery
publication or replace the original file's verified import.
