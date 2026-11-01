# Deploying Media Lab to the Spark

`kerpopule/media-lab` is the standalone Media Lab source repository. Existing
installations may still record a `kerpopule/vibexstudio` base tag plus private
overlays; repository extraction is not a live migration. Preserve and reconcile
those overlays before a new tagged deployment. Nothing is edited on the box;
a change goes PR → main → tag → deploy.
`tools/deploy-spark.sh` is the one road, and it is what writes
`~/media-lab-simple/deployed-source.json` on the box, so "what is running"
is always answerable.

```sh
git tag v1.4.0 && git push origin v1.4.0
SPARK=user@spark media-lab/tools/deploy-spark.sh v1.4.0 --dry-run   # rsync plan only
SPARK=user@spark media-lab/tools/deploy-spark.sh v1.4.0             # ship it
```

`SPARK` falls back to `MEDIA_LAB_SSH` in the checkout's `config/local.env`.

## What the script does

1. **Archive** `media-lab/` at the tag with `git archive` — uncommitted work
   cannot ship — and refuse if `tools/identity_guard.py` fails.
2. **Stage** the archive in `~/media-lab-simple.deploy-<tag>-<stamp>` on the
   Spark.
3. **Wait for idle**: poll `/api/queue` until no job is active (default up to
   an hour). A render is never interrupted by a deploy. The probe runs on the
   Spark and proves it is local with `local-token.txt` (piped to curl, never in
   `ps`); the studio trusts no `Host: localhost` header. If the studio answers
   but refuses the probe, the deploy stops — an unknown queue is never "idle".
4. **Back up** the code files the deploy is about to touch to
   `~/media-lab-simple/.backups/deploy-<stamp>-<commit>`, then **rsync**
   staging → live with two lists:
   * **ALLOWLIST** (code owns): `app.py`, the top-level modules,
     `media_lab_core/`, `runner/`, `static/`, `tools/`, `tests/`, `docs/`,
     `systemd/`, `launchd/`, `chat-system-prompt.md`,
     `pyproject.toml`/`uv.lock`/`requirements.txt`/`install.sh`, and under
     `config/` only the `*.example` files, unit templates and the static
     catalogs.
   * **PROTECT** (the box owns, never written or deleted): `config/local.env`,
     the studio's own overlay `config/local/` (docs/LOCAL-OVERLAY.md), the
     retired `prompt-templates/` folder,
     `config/{engine-installs,fal-catalog,model-residency-policy}.json`,
     `tunnel-config.yml`, `.venv/`, every state file (`jobs.json`, `jobs.db`,
     `gallery.json`, `characters.json*`, `storyboards.json*`, `providers.json`,
     `*-status.json`, `*-state.json`, `eta-stats.json`, `auth-attempts.json`,
     `lu-*.json`, `geo-*.json`), credentials (`admin-pin.txt`,
     `access-code.txt`, `access-secret.txt`, `vapid_private.pem`, `local-token.txt`,
     `push-subs.json`, `proxy7864.env`), and every private tree
     (`productions/`, `qa/`, `research/`, `reference/`, `medialab-import/`,
     `image-svc/`, `.artifacts/`, `media/`, `jobs/`, `pool/`, `inbox/`,
     `cut/projects`, `backups/`, `runner/models`, `runner/wheels`, the
     template GIF directories).
5. **Compile** the live tree with the box's own `.venv` (`py_compile` +
   `compileall`). A failure prints the exact rollback command and stops
   before the restart.
6. **Restart** `media-lab-simple.service` and wait for `/api/queue` to answer
   (3 minutes); on failure print the rollback command and the last 40 journal
   lines.
7. **Receipt**: write `deployed-source.json`:

   ```json
   {"repository": "...", "branch": "main", "tag": "v1.4.0", "commit": "…",
    "source_subdir": "media-lab", "deployed_at": "…", "rollback": "~/media-lab-simple/.backups/deploy-…",
    "files_list": ["app.py", "…"]}
   ```

## Rollback

Every deploy leaves its pre-image at the `rollback` path in the receipt:

```sh
ssh user@spark 'rsync -rlptD --delete <same filter rules> ~/media-lab-simple/.backups/deploy-<stamp>-<commit>/ ~/media-lab-simple/ && systemctl --user restart media-lab-simple.service'
```

(the script prints the full command with the rules expanded whenever it
aborts after the rsync step). Repository-level rollback: the tag
`pre-consolidation-20260914` marks the last `main` before the September 2026
consolidation.

## Per-host values

The deploy never writes `config/local.env`. That file holds the Spark's
addresses and paths (`MEDIA_LAB_BIND_HOST`, `MEDIA_LAB_TAILNET_HOST`,
`MEDIA_LAB_PUBLIC_HOSTS`, model/runtime roots, `SOL_*`); see
`config/local.env.example`. After a deploy that adds a key, add it there by
hand — the script will tell you nothing about it, but `python3 -m
media_lab_core.local_config` on the box shows what resolves.

## Known gaps

* Systemd unit files under `config/` and `systemd/` are shipped as templates;
  installing or reloading them (`systemctl --user daemon-reload`) is still a
  manual step.
* The script assumes the user unit `media-lab-simple.service` and the box's
  `.venv`; a first-time install is `./install.sh`, not this script.
