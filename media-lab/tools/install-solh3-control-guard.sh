#!/usr/bin/env bash
# Install or roll back the dormant H3 + independent guard user units.
# This mutates live user-service state and intentionally requires an explicit flag.
set -Eeuo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
USER_UNITS="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
STATE_ROOT="${XDG_STATE_HOME:-$HOME/.local/state}/media-lab/solh3-unit-releases"
H3_UNIT=media-lab-sol-h3.service
GUARD_UNIT=solh3-control-plane-guard.service
usage() {
  echo "usage: $0 --install --apply | --rollback BACKUP_DIR --apply" >&2
  exit 2
}
[[ "${*: -1}" == "--apply" ]] || usage

restore_backup() {
  local backup="$1" unit state target enabled active
  systemctl --user disable --now "$GUARD_UNIT" >/dev/null 2>&1 || true
  while IFS='|' read -r unit state; do
    target="$USER_UNITS/$unit"
    rm -f "$target"
    if [[ "$state" == "present" ]]; then
      cp -a "$backup/$unit" "$target"
    fi
  done < "$backup/manifest"
  systemctl --user daemon-reload
  enabled="$(<"$backup/guard-enabled")"
  active="$(<"$backup/guard-active")"
  case "$enabled" in
    enabled) systemctl --user enable "$GUARD_UNIT" ;;
    enabled-runtime) systemctl --user enable --runtime "$GUARD_UNIT" ;;
  esac
  if [[ "$active" == "active" ]]; then
    systemctl --user start "$GUARD_UNIT"
  fi
}

if [[ "${1:-}" == "--rollback" ]]; then
  BACKUP="${2:-}"
  [[ -d "$BACKUP" && -f "$BACKUP/manifest" ]] || { echo "invalid rollback artifact" >&2; exit 2; }
  systemctl --user is-active --quiet "$H3_UNIT" && {
    echo "refusing rollback while $H3_UNIT is active" >&2; exit 3;
  }
  restore_backup "$BACKUP"
  echo "rolled back from $BACKUP"
  echo "verify: systemctl --user show $H3_UNIT $GUARD_UNIT -p Id -p LoadState -p ActiveState -p SubState -p MainPID --no-pager"
  exit 0
fi

[[ "${1:-}" == "--install" && "${2:-}" == "--apply" ]] || usage
EXPECTED_ROOT="$HOME/media-lab-simple"
[[ -d "$EXPECTED_ROOT" && "$ROOT" -ef "$EXPECTED_ROOT" ]] || {
  echo "refusing install: source root must resolve to $EXPECTED_ROOT (got $ROOT)" >&2
  exit 2
}
systemctl --user is-active --quiet "$H3_UNIT" && {
  echo "refusing install while $H3_UNIT is active" >&2; exit 3;
}
STAMP="$(date -u +%Y%m%dT%H%M%SZ)-$$"
BACKUP="$STATE_ROOT/$STAMP"
mkdir -p "$BACKUP" "$USER_UNITS"
enabled="$(systemctl --user is-enabled "$GUARD_UNIT" 2>/dev/null || true)"
active="$(systemctl --user is-active "$GUARD_UNIT" 2>/dev/null || true)"
printf '%s\n' "${enabled:-disabled}" > "$BACKUP/guard-enabled"
printf '%s\n' "${active:-inactive}" > "$BACKUP/guard-active"
: > "$BACKUP/manifest"
for unit in "$H3_UNIT" "$GUARD_UNIT"; do
  if [[ -e "$USER_UNITS/$unit" || -L "$USER_UNITS/$unit" ]]; then
    cp -a "$USER_UNITS/$unit" "$BACKUP/$unit"
    printf '%s|present\n' "$unit" >> "$BACKUP/manifest"
  else
    printf '%s|absent\n' "$unit" >> "$BACKUP/manifest"
  fi
done
trap 'status=$?; trap - ERR; echo "install failed; restoring $BACKUP" >&2; restore_backup "$BACKUP"; exit "$status"' ERR
rm -f "$USER_UNITS/$H3_UNIT" "$USER_UNITS/$GUARD_UNIT"
install -m 0644 "$ROOT/config/$H3_UNIT" "$USER_UNITS/$H3_UNIT"
install -m 0644 "$ROOT/config/$GUARD_UNIT" "$USER_UNITS/$GUARD_UNIT"
systemctl --user daemon-reload
systemctl --user enable --now "$GUARD_UNIT"
for _ in $(seq 1 10); do
  "$ROOT/.venv/bin/python" -m media_lab_core.solh3_control_guard --check-heartbeat && break
  sleep 1
done
"$ROOT/.venv/bin/python" -m media_lab_core.solh3_control_guard --check-heartbeat
load="$(systemctl --user show "$H3_UNIT" -p LoadState --value)"
active="$(systemctl --user show "$H3_UNIT" -p ActiveState --value)"
sub="$(systemctl --user show "$H3_UNIT" -p SubState --value)"
pid="$(systemctl --user show "$H3_UNIT" -p MainPID --value)"
[[ "$load" == "loaded" && "$active" == "inactive" && "$sub" == "dead" && "$pid" == "0" ]] || {
  echo "dormant H3 contract failed: $load $active $sub $pid" >&2; exit 4;
}
# Use trusted private host configuration only to name paths in the private receipt.
set -a
# shellcheck disable=SC1091
source "$ROOT/config/local.env"
set +a
SOL_ROOT="${SOL_ROOT/#\~/$HOME}"
DEPLOYED="$ROOT/deployed-source.json"
TAG="unknown"; COMMIT="unknown"
if [[ -f "$DEPLOYED" ]]; then
  TAG="$("$ROOT/.venv/bin/python" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("tag","unknown"))' "$DEPLOYED")"
  COMMIT="$("$ROOT/.venv/bin/python" -c 'import json,sys; print(json.load(open(sys.argv[1])).get("commit","unknown"))' "$DEPLOYED")"
fi
"$ROOT/.venv/bin/python" - "$BACKUP/release-receipt.json" "$TAG" "$COMMIT" "$BACKUP" "$SOL_ROOT" "$ROOT" <<'PY'
import json, os, sys
path, tag, commit, backup, sol_root, root = sys.argv[1:]
runtime = os.environ.get("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
receipt = {
    "guard_unit": "solh3-control-plane-guard.service",
    "h3_unit": "media-lab-sol-h3.service",
    "guard_heartbeat": f"{runtime}/solh3-control-plane-guard.json",
    "legacy_latch": f"{runtime}/flashnext-memwatch.latch",
    "safety_stop": f"{sol_root}/safety-stop.json",
    "incident_receipts": f"{sol_root}/control-plane-incidents",
    "boot_clearance": f"{sol_root}/boot-clearance.json",
    "boot_clearance_schema": {"approved": True, "boot_id": "exact /proc/sys/kernel/random/boot_id"},
    "release_tag": tag,
    "release_commit": commit,
    "rollback_artifact": backup,
    "rollback_command": f"{root}/tools/install-solh3-control-guard.sh --rollback {backup} --apply",
    "rollback_verify": "systemctl --user show media-lab-sol-h3.service solh3-control-plane-guard.service -p Id -p LoadState -p ActiveState -p SubState -p MainPID --no-pager",
}
with open(path, "w", encoding="utf-8") as out:
    json.dump(receipt, out, indent=2, sort_keys=True)
    out.write("\n")
os.chmod(path, 0o600)
PY
trap - ERR
echo "installed dormant H3 + guard; private release receipt: $BACKUP/release-receipt.json"
echo "rollback artifact: $BACKUP"
