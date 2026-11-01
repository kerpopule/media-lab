# Moving an existing library

Migration copies files into a separate library. Keep the original library until
the new app can open its media, characters, voices, storyboards and editing projects.
Copy verification alone does not prove that these relationships work.

## Finding things

The organized copy uses these folders when it has matching content:

| Folder | Contents |
| --- | --- |
| Videos | Music Videos, Talking Heads, Storyboards, Clips, Edits |
| Images | Generated, Character References, Supporting Images, uploads and masks |
| Audio | Music, Voices, Voice and Recordings |
| Characters | A named folder for each preserved character |
| Projects | Video Edits, Productions and supporting project files |
| 3D Assets | Recognized 3D model files |

The importer uses existing catalog information and file types. These categories
are initial classifications, not a claim that every legacy file has perfect tags.
Files retain a readable name plus a stable suffix to prevent collisions. Character,
voice and storyboard IDs are retained. The migration receipt maps original paths
to copied paths; `.studio/original-metadata` retains original catalog bytes.

Use the app's Library to browse and reuse media. Avoid manually renaming migrated
files: saved project and character references may still address those paths.
Storyboards have both rendered media and structured records; copying only their
videos would lose the editable story. Missing original character IDs are reported
for recovery, never silently replaced with another character.

## Before switching libraries

From the repository's `media-lab` directory, inventory the original source into a
new private report outside that source, comparing it with the preservation receipt:

```sh
python -m media_lab_core.media_inventory /path/to/original \
  --output /path/to/private/fresh-inventory.json \
  --compare-receipt /path/to/copied-library/migration-receipt.json
```

Inspect `snapshotComparison`. Added, changed or removed source files require
review; do not erase earlier preserved versions when handling those changes.
External references require separate source checks, including container files.

Recheck the bytes in the copy using each receipt:

```sh
python -m media_lab_core.media_migration \
  /path/to/copied-library/migration-receipt.json \
  /path/to/copied-library --verify-copy
python -m media_lab_core.media_migration \
  /path/to/copied-library/external-reference-receipt.json \
  /path/to/copied-library --verify-copy
```

The check is read-only and returns a failing exit status for missing, changed or
linked files. It allows new files such as subsequent editing exports. It checks
only recorded files, so it does not replace source inventory or relationship tests.
Run checks while the preservation copy is not being modified; this is an integrity
check, not protection against hostile concurrent filesystem changes.

Also export projects held in each browser or installed app, restore them into a
separate project, and open their attachments. Server inventory cannot see a
phone's or browser's private project database. Large project archives are a
separate manual backup/restore path, not automatic device synchronization.

Finally, verify representative media playback, character/voice references,
storyboard songs and editing renders through the new app. Record unresolved items
and retain the originals and receipts through the switch and recovery period.
Do not describe migration as complete while these checks remain outstanding.


## Project planning notes

New Studio manual project backups include recent Sparky plans in
`Notes/Sparky.json`. After restoring, open Sparky and choose **Saved Sparky plans**
to continue a plan with an AI connection on the new device. These notes contain
conversation text, not AI account connections. Prior saved snapshots remain in the
file; clearing the current chat does not erase those backup snapshots.

Full backups preserve the note; code-only bundles and GitHub publishing exclude
it. This does not import older Media Lab director logs or automatically synchronize
new chat messages between devices. Keep the original director data during migration.


Archived characters are retained with their original IDs and an archived label.
Use **Show archived characters** to find them; migration does not reactivate them.
An older storyboard can therefore keep its archived cast. Character sheets and
individual reference images are checked separately: an intact sheet does not
prove every original reference image is still available. Missing references stay
visible as recovery issues, with their original links preserved.
