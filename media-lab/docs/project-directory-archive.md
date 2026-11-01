# Native project directory preservation

The `project_archive` module preserves file-backed native project directories in a
streaming ZIP_STORED archive. Unlike the existing JSON snapshot, binary assets are
stored as bytes. Current bounds: 1 GiB per file, 4 GiB total, 10,000 files, and a
4 MiB manifest. Processing uses 1 MiB read chunks. These are archive limits, not
new app import, sync, publishing, or browser storage limits.

From the repository with the controller source on PYTHONPATH:

```sh
PYTHONPATH=media-lab python -m media_lab_core.project_archive pack /path/to/project /path/to/new-backup.vibexdir
PYTHONPATH=media-lab python -m media_lab_core.project_archive restore /path/to/new-backup.vibexdir /path/to/new-staging-directory
```

The source must contain project.json. Optional chat.json, files/ and media/ content
are retained byte-for-byte. Empty directories are not recorded. Symbolic links,
nonportable paths and unknown files outside that layout are rejected. Source file
identity, size and modification signatures are checked during and after copying.
The archive is published exclusively without replacing an existing backup.

The manifest (`vibex/project-directory-archive`, version 1) lists every file's
relative path, size and SHA-256. Restore rejects undeclared/duplicate entries,
compression, unsafe paths and checksum mismatches. It verifies in a temporary
staging directory before reserving the requested new destination and transferring
verified files. It never replaces an existing destination or activates a project.
An interruption during the final directory transfer can leave an incomplete
staging destination; a success receipt is only returned after all transfers finish.

Existing file URI references and project identity are intentionally retained.
Application restore must reconcile attachment references and select a fresh
identity before activation. Web IndexedDB and mobile backup controls do not yet
use this format. Existing JSON backups and their readers are unchanged. This is a
preservation foundation, not completed large-asset sync or a portable app bundle.

The app-side `writeProjectDirectoryArchive` writes the same stored ZIP format from
async byte-chunk sources into an export sink. SHA-256 and ZIP CRCs are computed
incrementally; no entire-project JSON envelope is constructed. It uses ZIP32 and
rejects output above 4 GiB (including container overhead), with at most 1 MiB per
input chunk. The sink must discard partial writes on abort and publish on finish.
A cross-language test writes a 36 MiB fixture in JavaScript and restores/checks its
exact bytes with this Python module. Project-storage adapters and UI activation
are still pending; this writer alone does not change existing backup behavior.

`readProjectDirectoryArchive` reads the stored ZIP subset through a random-access
byte source. It validates the central directory, local entry names/ranges, manifest,
per-file size and path rules, ZIP CRC and SHA-256. Its receiver must consume every
chunk and stage the output; the function returns success only after all files
verify. It does not activate or write projects itself. Current app reader supports
ZIP32 central directories and ZIP64 local size headers; archives requiring ZIP64
central offsets/end records are rejected rather than truncated. Broader ZIP64
reader support is still needed for the largest Python-produced archives.
