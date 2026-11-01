# Studio library bridge

Studio can reuse completed Media Lab creations across devices without embedding
private server links or credentials in a shared project.

In Studio, connect a Media Lab address and enter its family code. The optional
code field requests a **library:read** ticket from `POST /api/gate` with
`{"code":"...","studio_library":true}`. The response contains a scoped ticket
and expiry duration; it does not create a normal Studio-manager session cookie.
The ticket expires after a year on a family studio (30 days on the independent
host) and is invalidated at once by rotating the corresponding family/admin code
or the server signing secret.

The client stores the ticket under a hashed server-origin key in its existing
secret-storage abstraction: native/desktop OS vault, or the existing browser
storage fallback. It uses the code only for connection, not as a stored credential.

Only these APIs accept the ticket in an `Authorization: Bearer ...` header:

- `GET /api/studio/library`: versioned metadata for completed, supported files
  currently present inside Media Lab's media directory.
- `GET /api/studio/library/{id}/content`: bytes for an offered gallery ID.
- `GET /api/studio/library/{id}/preview`: a PNG thumbnail for a supported image
  or existing local video poster. No external poster URLs are fetched.

No tokens in URLs. No cookie or trusted-host shortcut grants bridge access. The
ticket cannot authenticate normal queue, generation, administration, or gallery
routes. The bridge permits browser CORS without credentialed cookies and returns
private/no-store cache headers. Normal server UI cookie behavior remains separate.

The catalog omits absolute local paths and remote source URLs. Missing files,
uncompleted records, unsupported formats, traversal, and symlinks outside the
media root are excluded. Studio imports selected bytes into its project assets;
sharing that project does not require the server to remain online.

Current limits: library access does not yet authenticate generation/queue calls;
full video/audio playback remains separate work. Thumbnails are bounded to
480×320, 20 MiB source files, and 16 million source pixels, with at most two
concurrent decodes. Output omits source metadata. Automatic builder reuse is
documented in `app/docs/AGENT-LIBRARY.md`. Downloads are buffered with a bounded request deadline. Large-file
streaming and per-ticket revocation require further implementation.
