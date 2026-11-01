# Watching the Sparks from outside, and backing them up

Two small tools in `tools/ops/` run on a third machine that can already ssh to
both boxes (the "monitor"). Nothing is installed on the Sparks.

## Health watch (`tools/ops/spark_health_watch.py`)

- Every 5 minutes. Recommended: two jobs, so the check never depends on the
  chat gateway being up:
  - `spark_health_watch.py --outbox` from launchd/cron (`StartInterval 300`).
    It checks, writes the status files, and queues any message in
    `outbox.jsonl`. It prints nothing.
  - `spark_health_watch.py --deliver` as a job whose stdout is delivered (for
    example a Hermes `no_agent` cron job every 5 minutes). It prints the queued
    messages exactly once, and says so when the check itself has not run for
    20 minutes. Empty output means nothing to say.
  - Or a single stdout-delivering job with no flag (the check prints directly).
- A guard pressure trip during an H3 cold load is a warning for its first
  40 minutes (the studio clears that kind itself when
  `MEDIA_LAB_HOLD_AUTORECOVER=1`), then an action. H3 cold for an hour with
  nothing running is an action.
- It reads each host with `tools/ops/probe_hosts.py`, sent over ssh on stdin.
  The probe only reads.
- It speaks **only when a person must act**, and every message carries the
  next step.
- Messages are deduped:
  - a message when a problem starts;
  - a reminder at most every 6 h;
  - one "Resolved" line if it had alerted;
  - an unreachable host hides its own sub-checks.
- Quiet hours (default 22:00-07:00) hold messages, then send one merged
  message when they end.
- A maintenance marker mutes a host for up to 2 h, then the watch reports the
  marker as left behind.
- Warnings never message. They go to `daily.json` for a morning brief.
- **Dry run.** Set `"dry_run": true` or `SPARK_HEALTH_DRY_RUN=1`. Would-be
  messages go to `dry-run.log` and nothing is printed.
- `spark_health_watch.py --status` prints one line per host from the last run.
  Add `--refresh` to check live without touching alert state, or `--json` for
  machine-readable output.

Config (`~/.config/spark-health/config.json` or `$SPARK_HEALTH_CONFIG`; addresses
below are documentation examples):

```json
{
  "state_dir": "~/reports/spark-health",
  "dry_run": true,
  "quiet_hours": [22, 7],
  "hosts": {
    "studio": {"label": "Spark 1", "role": "studio", "ssh": "user@100.64.0.1",
               "public_url": "https://media.example.com/manifest.json"},
    "text":   {"label": "Spark 2", "role": "text", "ssh": "user@100.64.0.2",
               "text_url": "http://100.64.0.2:8004"}
  },
  "maintenance_markers": [{"path": "~/.config/ops/gpu-baton", "mutes": ["studio"]}],
  "local": {"label": "monitor", "disk_path": "~", "warn_gb": 60, "action_gb": 40},
  "backups": {"dir": "~/Backups/sparks", "sets": ["studio-library"], "max_age_h": 36},
  "persist": {"unreachable": 840}
}
```

**Studio host checks:**

| Check | Level |
|---|---|
| a GPU recovery hold | action after 20 min, or immediately if it is not the harmless restart kind or auto-recover gave up |
| a stuck queue, or a job past 3x its estimate | action |
| the studio units | action if not active for 10 min |
| the text bridge lists `media-lab-text` | action after 15 min |
| H3 boot clearance after a reboot (the studio clears it itself since 2026-09-27; alerts only if it gave up or is not cleared 50 min after boot) | warn, then action |
| H3 cold | warn after 30 min |
| disk free | warn under 15%, action under 8% |
| GPU thermal slowdown rising | warn |
| the public edge | action on 5xx or no answer for 15 min |

**Text host checks:**

| Check | Level |
|---|---|
| `text-model-state.json` is `serving` | action after 15 min |
| the endpoint lists `media-lab-text` | action after 15 min |
| a 4-token completion, every 15 min | action after 2 misses |
| Sparky's gateway is running with Telegram connected (read from its state file; nothing is sent) | action after 15 min |
| the memwatch latch | warn |
| disk free | warn under 15%, action under 8% |

**Monitor checks:**

| Check | Level |
|---|---|
| free disk | warn under 60 GB, action under 40 GB |
| each backup set | action if older than 36 h or under 90% of the previous size |

## Nightly pull backup (`tools/ops/spark_backup.py`)

`spark_backup.py run` backs up every set in `~/.config/spark-backup/config.json`.
For each set it:

1. Checks the space guard.
2. Stages the live JSON, SQLite (through the online-backup API) and command
   output on the host, at `nice 19`.
3. Runs `rsync -a --numeric-ids --delete --link-dest=<previous>` into
   `<root>/<set>/<date>.partial`, then renames it into place.
4. Writes a manifest (JSON hashes, plus 20 random media files hash-checked
   against the host) and `last-success.json`.
5. Prunes only its own dated snapshots (daily / weekly on Sundays / monthly on the 1st).

It never pulls secret files: every set is an include-list, and a fixed exclude
list is applied on top.

- `spark_backup.py verify <set> --restore-to <empty dir>` re-hashes the newest
  snapshot and restores a sample into a scratch directory. It never restores
  over live data.

```json
{
  "root": "~/Backups/sparks", "min_free_gb": 60, "min_free_after_gb": 50,
  "sets": {
    "studio-library": {
      "ssh": "user@100.64.0.1",
      "paths": ["media-lab-simple/media/", "media-lab-simple/jobs/", "media-lab-simple/cut/"],
      "stage_files": ["media-lab-simple/storyboards.json", "media-lab-simple/jobs.json"],
      "keep": {"daily": 14, "weekly": 8, "monthly": 6}
    }
  }
}
```
