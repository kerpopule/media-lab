#!/usr/bin/env bash
# Sourced by the runner shell scripts: loads config/local.env (per-host paths
# and addresses; see config/local.env.example) and fills in the same defaults
# media_lab_core/local_config.py uses, so a script and app.py never disagree.
#
#   . "$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)/local_env.sh"
#
# Safe under `set -Eeuo pipefail`. Values already in the environment win.
_media_lab_env_file="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/config/local.env"
if [[ -f "$_media_lab_env_file" ]]; then
  set -a
  # shellcheck disable=SC1090
  . "$_media_lab_env_file"
  set +a
fi
_media_lab_expand() { local v="$1"; printf '%s' "${v/#\~/$HOME}"; }
MEDIA_LAB_HOME="$(_media_lab_expand "${MEDIA_LAB_HOME:-$HOME/media-lab-simple}")"
MEDIA_LAB_MODELS_ROOT="$(_media_lab_expand "${MEDIA_LAB_MODELS_ROOT:-$HOME/.local/share/media-lab-p2-models}")"
MEDIA_LAB_RUNTIME_ROOT="$(_media_lab_expand "${MEDIA_LAB_RUNTIME_ROOT:-$HOME/runtime}")"
MEDIA_LAB_BIND_HOST="${MEDIA_LAB_BIND_HOST:-127.0.0.1}"
MEDIA_LAB_TAILNET_HOST="${MEDIA_LAB_TAILNET_HOST:-}"
MEDIA_LAB_TEXT_UPSTREAM="${MEDIA_LAB_TEXT_UPSTREAM:-http://127.0.0.1:8004}"
MEDIA_LAB_PUBLIC_HOSTS="${MEDIA_LAB_PUBLIC_HOSTS:-}"
export MEDIA_LAB_HOME MEDIA_LAB_MODELS_ROOT MEDIA_LAB_RUNTIME_ROOT MEDIA_LAB_BIND_HOST \
       MEDIA_LAB_TAILNET_HOST MEDIA_LAB_TEXT_UPSTREAM MEDIA_LAB_PUBLIC_HOSTS

# The studio's own base URL as seen from this machine (:7863).
case "$MEDIA_LAB_BIND_HOST" in
  0.0.0.0|::|"") MEDIA_LAB_STUDIO_URL="http://127.0.0.1:7863" ;;
  *)             MEDIA_LAB_STUDIO_URL="http://$MEDIA_LAB_BIND_HOST:7863" ;;
esac
export MEDIA_LAB_STUDIO_URL

# docker -p arguments for the :8004 text runtime: loopback always, plus the
# bind address when it is a specific non-loopback interface.
case "$MEDIA_LAB_BIND_HOST" in
  0.0.0.0|::)  MEDIA_LAB_PUBLISH_8004=(-p 8004:8000) ;;
  127.0.0.1|localhost|"") MEDIA_LAB_PUBLISH_8004=(-p 127.0.0.1:8004:8000) ;;
  *)           MEDIA_LAB_PUBLISH_8004=(-p 127.0.0.1:8004:8000 -p "$MEDIA_LAB_BIND_HOST:8004:8000") ;;
esac
