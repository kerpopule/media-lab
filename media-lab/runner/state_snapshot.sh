#!/bin/bash
# Hourly snapshot of the small state files that hold irreplaceable user data.
# Exists because characters.json was silently clobbered to a 2-entry stub some
# time before 2026-08-23 and only an ad-hoc backup saved the cast.
#
# Same disk as the data: this protects against a bad write, not a dead disk.
# The off-box copy is the nightly pull described in docs/BACKUP.md.
set -u
ROOT="$HOME/media-lab-simple"
case "${MEDIA_LAB_HOME:-}" in
  /*) ROOT="$MEDIA_LAB_HOME" ;;
  "~/"*) ROOT="$HOME/${MEDIA_LAB_HOME#\~/}" ;;
esac
DEST="$ROOT/backups"
STAMP=$(date +%Y%m%d-%H)
DAY=$(date +%Y%m%d)
mkdir -p "$DEST"
# storyboards.json is where storyboards live (the old list named boards.json,
# which never existed, so storyboards were not covered at all until 2026-09-26).
for f in characters.json voices.json gallery.json storyboards.json storyboards-archive.json; do
  [ -s "$ROOT/$f" ] || continue
  # skip if unchanged since the newest snapshot of this file
  last=$(ls -t "$DEST/$f".* 2>/dev/null | head -1)
  if [ -n "$last" ] && cmp -s "$ROOT/$f" "$last"; then continue; fi
  cp "$ROOT/$f" "$DEST/$f.$STAMP"
done
# jobs.json is ~10 MB and changes constantly: once a day, compressed.
if [ -s "$ROOT/jobs.json" ] && [ ! -e "$DEST/jobs.json.$DAY.gz" ]; then
  gzip -c "$ROOT/jobs.json" > "$DEST/.jobs.json.$DAY.gz.tmp" && mv "$DEST/.jobs.json.$DAY.gz.tmp" "$DEST/jobs.json.$DAY.gz"
fi
# keep 7 days
find "$DEST" -name "*.json.*" -mtime +7 -delete
# Residency receipts that changed nothing (retain/commit only) pile up at ~900 a
# day; keep 14 days of them. Receipts that did something are never pruned.
PY="$(command -v python3 || true)"
if [ -n "$PY" ] && [ -f "$(dirname "$0")/prune_residency_receipts.py" ]; then
  "$PY" "$(dirname "$0")/prune_residency_receipts.py" "$ROOT/pool/residency/receipts" --days 14 || true
fi
