#!/usr/bin/env python3
"""Delete residency receipts that recorded no change, once they are old.

A receipt is a no-op when its transaction committed and every planned action
was ``retain`` or ``commit``. Anything else (an eviction, a load, a rollback, a
failure) is evidence and is kept forever. The newest receipt is always kept,
because ``/api/residency`` reports it as ``last_receipt``.

    prune_residency_receipts.py <receipts dir> [--days 14] [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

NOOP_ACTIONS = {"retain", "commit"}


def is_noop(receipt: dict) -> bool:
    if receipt.get("status") != "committed" or receipt.get("rollback"):
        return False
    actions = (receipt.get("plan") or {}).get("actions")
    if not isinstance(actions, list):
        return False
    return all(isinstance(a, dict) and a.get("action") in NOOP_ACTIONS for a in actions)


def prune(directory: Path, *, days: float, now: float | None = None, dry_run: bool = False) -> int:
    now = time.time() if now is None else now
    files = sorted(Path(directory).glob("*.json"))
    if not files:
        return 0
    newest = max(files, key=lambda p: p.stat().st_mtime)
    removed = 0
    for path in files:
        if path == newest or now - path.stat().st_mtime < days * 86400:
            continue
        try:
            receipt = json.loads(path.read_text())
        except (OSError, ValueError):
            continue
        if isinstance(receipt, dict) and is_noop(receipt):
            removed += 1
            if not dry_run:
                path.unlink(missing_ok=True)
    return removed


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("directory", type=Path)
    ap.add_argument("--days", type=float, default=14)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args(argv)
    if not args.directory.is_dir():
        return 0
    n = prune(args.directory, days=args.days, dry_run=args.dry_run)
    if n:
        print(f"[statesnap] {'would prune' if args.dry_run else 'pruned'} {n} no-op residency receipts")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
