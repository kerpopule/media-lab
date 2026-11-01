#!/usr/bin/env bash
# Media Lab pool lock manager. Usage: pool_lock.sh acquire|release|handoff
# One transient unit (media-lab-pool.service) holds the canonical GPU flock
# while ANY warm engine is resident, replacing the old reservation pattern.
# Uses the proven override-toggle recipe (reservation service is RefuseManualStart;
# the ONLY working restore is: move override aside, daemon-reload, reset-failed,
# systemd-run flock holder, move override back, daemon-reload).
#
# Since 2026-09-21 the controller owns the canonical flock itself for the
# duration of every fenced local GPU operation, so the legacy holders above are
# no longer the only legitimate owners. `controller_owns_lock` (read-only, below)
# names that owner so this shim stops reporting the controller as an "external
# holder" — that misreport is what answered BUSY, and every image job then died
# with 503 "gpu reserved elsewhere" on an idle box.
set -Eeuo pipefail
LOCK=/run/user/1000/spark-gpu.lock
OVERRIDE=$HOME/.config/systemd/user/media-lab-gpu-reservation.service.d/override.conf
CMD=${1:?acquire|release|handoff}
POOL_SH_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
LEASE_PROBE=$POOL_SH_DIR/lease_owner_probe.py

# Our own canonical owner, read-only; never takes, releases or kills. Any doubt
# (missing probe, foreign holder, dead holder, unreadable evidence) is false, so
# the legacy BUSY behaviour below is preserved exactly.
#
# controller_owns_lock  : may residency be granted? False while the controller
#                         has an unresolved GPU outcome (fail closed).
# controller_holds_lock : is our own live controller the current holder? Used by
#                         release, whose only question is whether starting an
#                         idle reservation would fight our own held lock.
controller_owns_lock() {
  [ -f "$LEASE_PROBE" ] || return 1
  /usr/bin/python3 "$LEASE_PROBE" --lock "$LOCK" >/dev/null 2>&1
}

controller_holds_lock() {
  [ -f "$LEASE_PROBE" ] || return 1
  /usr/bin/python3 "$LEASE_PROBE" --lock "$LOCK" --ignore-hold >/dev/null 2>&1
}

case "$CMD" in
  acquire)
    if systemctl --user is-active --quiet media-lab-pool.service; then echo OK; exit 0; fi
    if controller_owns_lock; then
      # The controller already holds canonical exclusion for a fenced local GPU
      # operation. Residency is therefore granted: do not start a second holder
      # (its flock could not be taken anyway) and do not call it "external".
      echo OK; exit 0
    fi
    if systemctl --user is-active --quiet media-lab-gpu-reservation.service; then
      # Reservation holds the lock (GPU idle-reserved): take over via override toggle.
      mv "$OVERRIDE" "$OVERRIDE.pool"; systemctl --user daemon-reload
      systemctl --user stop media-lab-gpu-reservation.service
      mv "$OVERRIDE.pool" "$OVERRIDE"; systemctl --user daemon-reload
    elif ! flock -n "$LOCK" -c true; then
      # The lock is held while NEITHER of our units is active. That is either an
      # outside production batch (leave it alone) or — the case that stalled
      # every H3 take on 2026-08-17 — a stale holder left behind by a transient
      # unit that died, which nothing will ever clean up. Reclaim only holders
      # that match our own "flock <lock> sleep infinity" pattern.
      for p in $(fuser "$LOCK" 2>/dev/null); do
        cmd=$(ps -o cmd= -p "$p" 2>/dev/null || true)
        case "$cmd" in
          *flock*"$LOCK"*sleep*)
            echo "reclaiming stale lock holder pid $p" >&2
            kill "$p" 2>/dev/null || true ;;
        esac
      done
      sleep 1
      if ! flock -n "$LOCK" -c true; then
        # still held -> genuinely someone else's GPU
        echo BUSY; exit 62
      fi
    fi
    systemctl --user reset-failed media-lab-pool.service >/dev/null 2>&1 || true
    systemd-run --user --unit=media-lab-pool.service --property=Restart=always \
      --property=RestartSec=2s /usr/bin/flock "$LOCK" /usr/bin/sleep infinity >/dev/null
    for i in $(seq 1 20); do
      flock -n "$LOCK" -c true || { echo OK; exit 0; }
      sleep 0.5
    done
    echo FAIL; exit 63
    ;;
  release)
    if controller_holds_lock; then
      # Our own live controller holds the canonical lock, so nothing else can
      # hold it: only stand the legacy holders down (the pool unit, as this
      # command always did, plus any reservation waiting on the lock) and never
      # start the idle reservation. A waiting flock is precisely the debris that
      # stalled every take on 2026-08-17. The controller releases its own fd when
      # the operation ends, and `handoff` takes over before the next acquire.
      systemctl --user stop media-lab-pool.service >/dev/null 2>&1 || true
      systemctl --user reset-failed media-lab-pool.service >/dev/null 2>&1 || true
      systemctl --user stop media-lab-gpu-reservation.service >/dev/null 2>&1 || true
      systemctl --user reset-failed media-lab-gpu-reservation.service >/dev/null 2>&1 || true
      echo OK; exit 0
    fi
    systemctl --user stop media-lab-pool.service >/dev/null 2>&1 || true
    systemctl --user reset-failed media-lab-pool.service >/dev/null 2>&1 || true
    if ! systemctl --user is-active --quiet media-lab-gpu-reservation.service; then
      mv "$OVERRIDE" "$OVERRIDE.pool"; systemctl --user daemon-reload
      systemctl --user reset-failed media-lab-gpu-reservation.service >/dev/null 2>&1 || true
      systemd-run --user --unit=media-lab-gpu-reservation.service --property=Restart=always \
        --property=RestartSec=2s /usr/bin/flock "$LOCK" /usr/bin/sleep infinity >/dev/null 2>&1 || true
      mv "$OVERRIDE.pool" "$OVERRIDE"; systemctl --user daemon-reload
    fi
    echo OK
    ;;
  handoff)
    # Transfer the canonical flock from either legacy idle holder to the
    # durable controller.  Do not start a replacement unit: the controller's
    # open lease fd becomes the owner immediately after this command returns.
    systemctl --user stop media-lab-pool.service >/dev/null 2>&1 || true
    systemctl --user reset-failed media-lab-pool.service >/dev/null 2>&1 || true
    if systemctl --user is-active --quiet media-lab-gpu-reservation.service; then
      restored=0
      restore_override() {
        if [[ $restored -eq 0 && -f "$OVERRIDE.pool" ]]; then
          mv "$OVERRIDE.pool" "$OVERRIDE"
          systemctl --user daemon-reload
          restored=1
        fi
      }
      trap restore_override EXIT
      mv "$OVERRIDE" "$OVERRIDE.pool"; systemctl --user daemon-reload
      systemctl --user stop media-lab-gpu-reservation.service
      restore_override
      trap - EXIT
    fi
    if ! flock -n "$LOCK" -c true; then
      # A non-legacy holder still owns the GPU. Never kill or bypass it.
      echo BUSY; exit 62
    fi
    echo OK
    ;;
  *) echo "unknown command $CMD"; exit 2;;
esac
