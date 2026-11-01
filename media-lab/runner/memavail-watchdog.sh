#!/usr/bin/env bash
# memavail-watchdog.sh — stop the vLLM container when host MemAvailable stays below a floor.
# Designed to be run every INTERVAL seconds by a systemd user timer (flashnext-memwatch.timer); keeps its consecutive
# counter in $XDG_RUNTIME_DIR so each oneshot invocation is stateless. No root needed (docker group + user units).
#
#   FLOOR_GIB=12   CONSECUTIVE=3   CONTAINER=qwen38-flash-next   UNIT=flashnext.service
#
# Why MemAvailable: see WATCHDOG.md. The latch records the safety stop for
# operator inspection; it must not disable protection if a controller relaunches.
set -u
FLOOR_GIB="${FLOOR_GIB:-12}"
CONSECUTIVE="${CONSECUTIVE:-3}"
CONTAINER="${CONTAINER:-qwen38-flash-next}"
UNIT="${UNIT:-media-lab-sol-h3.service}"
RUN="${XDG_RUNTIME_DIR:-/tmp}"
COUNT_FILE="$RUN/flashnext-memwatch.count"
LATCH="$RUN/flashnext-memwatch.latch"
LOG="${LOG:-$HOME/logs/memwatch.log}"
TAG=flashnext-memwatch

read -r _ avail_kb _ < <(grep '^MemAvailable:' /proc/meminfo)
read -r _ free_kb  _ < <(grep '^MemFree:' /proc/meminfo)
read -r _ swapfree_kb _ < <(grep '^SwapFree:' /proc/meminfo)
floor_kb=$(( FLOOR_GIB * 1048576 ))
avail_mib=$(( avail_kb / 1024 )); free_mib=$(( free_kb / 1024 )); swapfree_mib=$(( swapfree_kb / 1024 ))
running=0; systemctl --user is-active --quiet "$UNIT" && running=1

count=0; [ -f "$COUNT_FILE" ] && count=$(cat "$COUNT_FILE" 2>/dev/null || echo 0)
if [ "$avail_kb" -lt "$floor_kb" ]; then count=$((count + 1)); else count=0; fi
echo "$count" > "$COUNT_FILE"
mkdir -p "$(dirname "$LOG")" 2>/dev/null
# One line every 5 s is ~15 MB a month: keep the current file under LOG_MAX_KB
# and one previous generation.
LOG_MAX_KB="${LOG_MAX_KB:-5120}"
if [ -f "$LOG" ] && [ "$(( $(stat -c %s "$LOG" 2>/dev/null || stat -f %z "$LOG" 2>/dev/null || echo 0) / 1024 ))" -ge "$LOG_MAX_KB" ]; then
  mv -f "$LOG" "$LOG.1" 2>/dev/null || true
fi
printf '%s avail=%dMiB free=%dMiB swapfree=%dMiB below_floor=%d/%d container=%s\n' \
  "$(date '+%F %T')" "$avail_mib" "$free_mib" "$swapfree_mib" "$count" "$CONSECUTIVE" "$running" >> "$LOG" 2>/dev/null

# A controller can restart the unit without clearing the marker. Keep the
# safety evidence, but never let an old marker disable protection of a new load.
[ -f "$LATCH" ] && [ "$running" != 1 ] && exit 0
[ "$count" -lt "$CONSECUTIVE" ] && exit 0
[ "$running" = 1 ] || { echo 0 > "$COUNT_FILE"; exit 0; }  # nothing of ours to kill; do not latch on someone else's pressure

logger -t "$TAG" -p user.crit "MemAvailable=${avail_mib}MiB < ${FLOOR_GIB}GiB for ${count} consecutive samples — killing $CONTAINER and stopping $UNIT"
date '+%F %T' > "$LATCH"
true
systemctl --user stop "$UNIT" >/dev/null 2>&1 || true      # explicit stop => Restart=always does not bring it back
echo 0 > "$COUNT_FILE"
logger -t "$TAG" -p user.crit "stop requested; latch retained. Inspect unit state and kernel NVRM errors; operator-approved recovery required before clearing the latch."
exit 2
