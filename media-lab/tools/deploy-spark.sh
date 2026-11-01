#!/usr/bin/env bash
# deploy-spark.sh — ship media-lab/ from a git tag to the Spark, safely.
#
#   media-lab/tools/deploy-spark.sh v1.4.0                # deploy the tag
#   media-lab/tools/deploy-spark.sh v1.4.0 --dry-run      # show the rsync plan only
#   SPARK=user@host media-lab/tools/deploy-spark.sh v1.4.0
#
# The road (see docs/DEPLOY.md):
#   1. `git archive` media-lab/ at the tag (nothing uncommitted can ship);
#   2. copy it to a staging dir on the Spark next to the live tree;
#   3. wait until /api/queue reports no active work;
#   4. rsync staging -> live with an ALLOWLIST of what code owns and a PROTECT
#      list of everything the box owns (state, media, credentials, local.env);
#   5. py_compile the live tree with its own venv;
#   6. restart media-lab-simple.service and verify /api/queue answers;
#   7. write deployed-source.json (repo, branch, tag, commit, files) with a
#      rollback pointer to the pre-deploy backup.
# Every step is fail-closed: an error before step 4 leaves the live tree
# untouched; an error after it prints the rollback command.
set -Eeuo pipefail

TAG="${1:?usage: deploy-spark.sh <tag> [--dry-run | --scoped-plan PLAN [--dry-run]]}"
if [[ "${2:-}" == "--scoped-plan" ]]; then
  [[ -n "${3:-}" && ( $# -eq 3 || ( $# -eq 4 && "$4" == "--dry-run" ) ) ]] || { printf '%s\n' 'invalid scoped release arguments' >&2; exit 2; }
  [[ "$TAG" =~ ^[A-Za-z0-9][A-Za-z0-9._-]*$ ]] || exit 2
  SCOPED_TOOL="$(mktemp)"; trap 'rm -f "$SCOPED_TOOL"' EXIT
  git show "refs/tags/$TAG:media-lab/tools/scoped_release.py" > "$SCOPED_TOOL"
  python3 "$SCOPED_TOOL" "$@"
  exit $?
fi
[[ $# -eq 1 || ( $# -eq 2 && "$2" == "--dry-run" ) ]] || { printf '%s\n' 'unknown deployment option' >&2; exit 2; }
DRY=0; [[ "${2:-}" == "--dry-run" ]] && DRY=1
SPARK="${SPARK:-${MEDIA_LAB_SSH:-}}"
REMOTE_HOME="${REMOTE_HOME:-media-lab-simple}"          # relative to the remote $HOME
REMOTE_PORT="${REMOTE_PORT:-7863}"
SERVICE="${SERVICE:-media-lab-simple.service}"
QUEUE_WAIT_S="${QUEUE_WAIT_S:-3600}"
SUBDIR=media-lab

REPO_ROOT="$(git rev-parse --show-toplevel)"
cd "$REPO_ROOT"
if [[ -z "$SPARK" ]]; then
  # Fall back to the checkout's local.env (MEDIA_LAB_SSH) when SPARK is unset.
  if [[ -f "$SUBDIR/config/local.env" ]]; then
    SPARK="$(sed -n 's/^MEDIA_LAB_SSH=//p' "$SUBDIR/config/local.env" | tr -d '"'"'" | head -1)"
  fi
fi
[[ -n "$SPARK" ]] || { echo "set SPARK=user@host (or MEDIA_LAB_SSH in $SUBDIR/config/local.env)" >&2; exit 2; }

say()  { printf '\033[1m==> %s\033[0m\n' "$*"; }
die()  { echo "deploy-spark: $*" >&2; exit 1; }
rssh() { ssh -o BatchMode=yes -o ConnectTimeout=10 "$SPARK" "$@"; }
REMOTE_ABS_HOME="$(rssh 'printf %s "$HOME"')"
# A receipt with private/scoped code is not a pristine whole-tree preimage.
# Refuse before staging, even for dry-run; use an explicit hash-bound plan.
rssh "python3 -c 'import json,sys; from pathlib import Path; d=json.loads(Path(sys.argv[1]).read_text()); sys.exit(1 if d.get(\"private_overlays\") or d.get(\"scoped_releases\") else 0)' '$REMOTE_HOME/deployed-source.json'" || die "whole-tree deployment refused: missing receipt or private/scoped integrations; use --scoped-plan"
# The public web origin stays CPU-only while the paired GPU reservation exists.
if [[ "$SERVICE" == "media-lab-simple.service" ]] && rssh 'test -f "$HOME/.local/state/spark-pair-control/reservation.json"'; then
  rssh 'test -f "$HOME/.config/systemd/user/media-lab-web.service"' || die "pair reserved but web-only service missing; refusing legacy restart"
  SERVICE="media-lab-web.service"
  say "pair reserved: restarting only the web-only origin"
fi
# Media Lab binds MEDIA_LAB_BIND_HOST (often the tailnet IP), not loopback: read it from the Spark's local.env.
REMOTE_BIND="$(rssh "sed -n 's/^MEDIA_LAB_BIND_HOST=//p' ~/${REMOTE_HOME:-media-lab-simple}/config/local.env 2>/dev/null | tr -d '\"' | head -1" || true)"
REMOTE_BIND="${REMOTE_BIND:-127.0.0.1}"
# The studio's queue, asked ON the Spark. The studio trusts no Host header, so
# the probe proves it runs on the box with the local tool token (local-token.txt,
# mode 0600, written by app.py at start). The token is piped to curl on stdin
# (-H @-), so it never appears in argv / `ps`. Before the first deploy of this
# build the file does not exist yet and the old app still honours the legacy
# "Host: localhost" probe, which is sent only in that case.
remote_queue() {  # $1 = extra curl flags
  rssh "tok=\$(cat ~/$REMOTE_HOME/local-token.txt 2>/dev/null); \
    if [ -n \"\$tok\" ]; then printf 'X-Media-Lab-Local: %s\\n' \"\$tok\" | curl -s $1 -m 5 -H @- http://$REMOTE_BIND:$REMOTE_PORT/api/queue?hist=0; \
    else curl -s $1 -m 5 -H 'Host: localhost' http://$REMOTE_BIND:$REMOTE_PORT/api/queue?hist=0; fi"
}

# ---------------------------------------------------------------- 0. identity
git rev-parse -q --verify "refs/tags/$TAG" >/dev/null || die "no such tag: $TAG"
COMMIT="$(git rev-list -n 1 "$TAG")"
REPO_URL="$(git remote get-url origin 2>/dev/null || echo unknown)"
BRANCH="$(git branch -r --contains "$COMMIT" 2>/dev/null | sed -n 's|^ *origin/||p' | grep -vx HEAD | head -1 || true)"
BRANCH="${BRANCH:-$(git branch --contains "$COMMIT" --format='%(refname:short)' | head -1)}"
STAMP="$(date -u +%Y%m%dT%H%M%SZ)"
STAGE="$REMOTE_HOME.deploy-$TAG-$STAMP"
BACKUP="$REMOTE_ABS_HOME/$REMOTE_HOME/.backups/deploy-$STAMP-$COMMIT"
say "deploying $SUBDIR/ at $TAG ($COMMIT) to $SPARK:~/$REMOTE_HOME"

# Guard: the tree must not carry private identity (the same check CI runs).
python3 "$SUBDIR/tools/identity_guard.py" >/dev/null || die "identity_guard failed on the checkout"

# ---------------------------------------------------------------- 1. archive
WORK="$(mktemp -d)"; trap 'rm -rf "$WORK"' EXIT
git archive --format=tar "$TAG" "$SUBDIR" | tar -x -C "$WORK"
[[ -f "$WORK/$SUBDIR/app.py" ]] || die "archive has no $SUBDIR/app.py"
( cd "$WORK/$SUBDIR" && find . -type f | sed 's|^\./||' | sort ) > "$WORK/files.txt"
say "archived $(wc -l < "$WORK/files.txt" | tr -d ' ') files"

# What code owns on the box (rsync --include rules, evaluated in order).
ALLOW=(
  '+ /app.py' '+ /web_only_policy.py'
  '+ /chat-system-prompt.md'
  '+ /chat_operator.py' '+ /qwen_activity.py' '+ /residency.py' '+ /screenshot_song.py'
  '+ /image_template_context.py' '+ /local_studio.py' '+ /proxy7864.py'
  '+ /coupled_av_trim.py' '+ /README.md' '+ /LICENSE' '+ /AGENTS.md' '+ /CREDITS.md'
  '+ /pyproject.toml' '+ /uv.lock' '+ /requirements.txt' '+ /install.sh'
  '+ /media_lab_core/***'
  '+ /runner/***'
  '+ /static/***'
  '+ /tools/***'
  '+ /patches/***'
  '+ /tests/***'
  '+ /docs/***'
  '+ /systemd/***'
  '+ /launchd/***'
  '+ /config/'
  '+ /config/*.example'
  '+ /config/*.service' '+ /config/*.timer'
  '+ /config/capacity-budget.json' '+ /config/capacity-policy.json'
  '+ /config/gpu-capacity-receipts.json'
  '+ /config/companion-residency-policy.json'
  '+ /config/model-manifests.json'
  '+ /config/render-eta.json'
  '- *'
)
# What the box owns: never written, never deleted (rsync --exclude, first wins).
PROTECT=(
  '._*' '.DS_Store'
  '/config/local.env' '/config/local' '/config/engine-installs.json' '/config/fal-catalog.json'
  '/config/model-residency-policy.json'
  '/productions' '/qa' '/research' '/reference' '/medialab-import' '/image-svc'
  '/.artifacts' '/media' '/jobs' '/pool' '/inbox' '/cut/projects' '/backups' '/.backups'
  '/gallery.json' '/jobs.json' '/jobs.db' '/characters.json*' '/storyboards.json*'
  '/providers.json' '/pilot-status.json' '/supervisor-state.json' '/watchdog-state.json'
  '/eta-stats.json' '/auth-attempts.json' '/lu-*.json' '/geo-*.json' '/voices.json'
  '/admin-pin.txt' '/access-code.txt' '/access-secret.txt' '/vapid_private.pem' '/local-token.txt'
  '/push-subs.json' '/tunnel-config.yml' '/proxy7864.env' '/deployed-source.json'
  '/.venv' '/.worktrees' '/app.py.*' '*.bak*' '/HANDOFF.md' '/V2-BUILD-REPORT.md'
  '/QWEN38-CUTOVER.md' '/backfill-*.py' '/screenshot-songs' '/voices' '/uploads*'
  '/runner/models' '/runner/wheels' '/static/templates' '/static/template-library/images'
  '/prompt-templates' '/runner/maestro_refresh.sh'
)
# One rsync filter file on the Spark: protect lines first (first match wins, and
# excluded receiver files are never deleted), then the allowlist, then "- *".
# A merge file avoids shell quoting entirely (macOS bash 3.2 lacks ${arr[@]@Q}).
RULES_FILE="$REMOTE_ABS_HOME/$REMOTE_HOME/.backups/deploy-rules-$STAMP"   # absolute: later steps cd into the live tree
{ for p in "${PROTECT[@]}"; do printf '%s\n' "- $p"; done
  for a in "${ALLOW[@]}";   do printf '%s\n' "$a"; done; } > "$WORK/deploy-rules"
RSYNC_FILTER="--filter=\"merge $RULES_FILE\""   # one argv on the remote shell: --filter="merge /path"

# ---------------------------------------------------------------- 2. stage
say "staging to $SPARK:~/$STAGE"
rssh "mkdir -p '$STAGE' '$REMOTE_HOME/.backups'"
rssh "cat > '$RULES_FILE'" < "$WORK/deploy-rules"
COPYFILE_DISABLE=1 tar --exclude='._*' --exclude='.DS_Store' -C "$WORK/$SUBDIR" -cf - . | rssh "tar -xf - -C '$STAGE'"   # no macOS AppleDouble shadows
rssh "test -f '$STAGE/app.py'" || die "staging failed"

# ---------------------------------------------------------------- 3. idle
say "waiting for the queue to go idle (up to ${QUEUE_WAIT_S}s)"
deadline=$(( $(date +%s) + QUEUE_WAIT_S ))
while :; do
  # -1: nothing answered (the app is down, so nothing can be rendering);
  # -2: the app answered but refused the probe ({"error":"locked"}) — its queue
  #     is UNKNOWN, so never read that as idle and deploy mid-render.
  active="$( { remote_queue '' || true; } \
            | python3 -c 'import json,sys
try:
    d=json.load(sys.stdin)
except Exception:
    print(-1); raise SystemExit
if isinstance(d, dict) and ("active" in d or "active_total" in d):
    print(d.get("active_total", len(d.get("active", []))))
else:
    print(-2)')"
  if [[ "$active" == "0" ]]; then break; fi
  if [[ "$active" == "-1" ]]; then echo "  /api/queue not answering; treating as idle after a grace period"; sleep 15; break; fi
  if [[ "$active" == "-2" ]]; then die "the studio refused the queue probe (local-token.txt missing or rejected); not deploying blind. staging left at ~/$STAGE"; fi
  (( $(date +%s) < deadline )) || die "queue still busy ($active active) after ${QUEUE_WAIT_S}s; staging left at ~/$STAGE"
  printf '  %s active job(s)… ' "$active"; sleep 30
done
echo "idle"

if (( DRY )); then
  say "dry run: rsync plan"
  rssh "rsync -rlptD -n -iv --delete $RSYNC_FILTER '$STAGE/' '$REMOTE_HOME/'" | sed 's/^/  /'
  rssh "rm -rf '$STAGE'"
  exit 0
fi

# ---------------------------------------------------------------- 4. backup + rsync
say "backing up the live code files to ~/$BACKUP"
rssh "mkdir -p '$BACKUP' && cd '$REMOTE_HOME' && \
  rsync -rlptD $RSYNC_FILTER ./ '$BACKUP/' >/dev/null"
say "rsync staging -> live"
rssh "rsync -rlptD --delete $RSYNC_FILTER '$STAGE/' '$REMOTE_HOME/' && rm -rf '$STAGE'"

# ---------------------------------------------------------------- 5. compile
say "installing requirements.txt into the box's venv"
rssh "cd '$REMOTE_HOME' && [ -x .venv/bin/pip ] && .venv/bin/pip install -q -r requirements.txt 2>&1 | tail -3 || echo '  (no .venv/bin/pip; skipped)'"
say "py_compile with the box's venv"
if ! rssh "cd '$REMOTE_HOME' && PY=\$( [ -x .venv/bin/python ] && echo .venv/bin/python || echo python3 ); \
      \$PY -m py_compile app.py \$(ls *.py 2>/dev/null) && \
      \$PY -m compileall -q media_lab_core runner tools 2>&1 | grep -E 'Error|error' | head -20; test \${PIPESTATUS[0]} -eq 0"; then
  echo "compile FAILED — roll back with:" >&2
  echo "  ssh $SPARK 'rsync -rlptD --delete $RSYNC_FILTER $BACKUP/ ~/$REMOTE_HOME/ && systemctl --user restart $SERVICE'" >&2
  exit 1
fi

# ---------------------------------------------------------------- 6. restart + verify
say "restarting $SERVICE"
rssh "systemctl --user restart '$SERVICE'"
ok=0
for _ in $(seq 1 60); do
  if remote_queue '-f' >/dev/null; then ok=1; break; fi
  sleep 3
done
if (( ! ok )); then
  echo "service did not answer on :$REMOTE_PORT after restart — roll back with:" >&2
  echo "  ssh $SPARK 'rsync -rlptD --delete $RSYNC_FILTER $BACKUP/ ~/$REMOTE_HOME/ && systemctl --user restart $SERVICE'" >&2
  rssh "journalctl --user -u '$SERVICE' -n 40 --no-pager" >&2 || true
  exit 1
fi

# ---------------------------------------------------------------- 7. receipt
say "writing deployed-source.json"
python3 - "$REPO_URL" "$BRANCH" "$TAG" "$COMMIT" "$SUBDIR" "$BACKUP" "$WORK/files.txt" > "$WORK/deployed-source.json" <<'PY'
import json, sys, datetime
repo, branch, tag, commit, subdir, backup, files = sys.argv[1:8]
print(json.dumps({
    "repository": repo, "branch": branch, "tag": tag, "commit": commit,
    "source_subdir": subdir,
    "deployed_at": datetime.datetime.now().astimezone().isoformat(timespec="seconds"),
    # BACKUP is already absolute ($REMOTE_ABS_HOME/...): "~/" + it made "~//home/...".
    "rollback": backup,
    "files_list": [l.strip() for l in open(files) if l.strip()],
}, indent=1))
PY
rssh "cat > '$REMOTE_HOME/deployed-source.json'" < "$WORK/deployed-source.json"
say "deployed $TAG ($COMMIT). rollback: $BACKUP"
