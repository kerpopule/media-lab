#!/usr/bin/env python3
"""Nightly pull backup of the Sparks' irreplaceable data to this machine.

Runs on the backup machine (a LaunchAgent or a timer), pulling over ssh with
rsync ``--link-dest`` so an unchanged file costs nothing in the next snapshot.
Stdlib only, Python 3.9+.

    spark_backup.py run  [SET ...]      # back up (all sets when none named)
    spark_backup.py prune [SET ...]     # apply retention only
    spark_backup.py verify SET [--restore-to DIR]   # hash-check, optionally restore a sample
    spark_backup.py plan                # print what would run, change nothing

Per set (config: $SPARK_BACKUP_CONFIG or ~/.config/spark-backup/config.json):
  1. a space guard: skip (and record why) unless the disk has min_free_gb free
     and would keep min_free_after_gb after a full-size copy;
  2. on the host, at low priority, stage the live files that must be read
     whole: JSON state is copied, SQLite goes through the online-backup API,
     and listed commands (crontab -l ...) are captured. Nothing live is written;
  3. pull each listed path with ``rsync -a --numeric-ids --delete
     --link-dest=<previous snapshot>`` into <root>/<set>/<YYYY-MM-DD>.partial,
     then rename it into place, so a half-finished run never looks complete;
  4. write manifest.json (file count, bytes, sha256 of every staged/JSON file,
     and 20 random media files checked against the host), last-success.json
     (read by the health watch) and a size sanity flag (< 90% of the previous);
  5. prune only this tool's own dated snapshots past retention
     (daily / weekly on Sundays / monthly on the 1st).

Secrets are never pulled: sets are include-lists, and every rsync also gets an
exclude list of the studio's secret file names as a second guard.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import hashlib
import json
import os
import random
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path

DEFAULT_CONFIG = Path(os.path.expanduser("~/.config/spark-backup/config.json"))
NEVER = ["admin-pin.txt", "access-code.txt", "access-secret.txt", "local-token.txt",
         "vapid_private.pem", "local.env", "auth-attempts.json", "push-subs.json",
         "*.sock", "*.pid", ".env", "auth.json", "*.key", "id_*"]
# The host-side rsync runs in its own memory-capped scope, so the page cache
# it fills while reading the library is reclaimed inside that scope and never
# squeezes a warm (or loading) video engine; plus the lowest CPU/IO priority.
REMOTE_RSYNC = ("systemd-run --user --scope --quiet -p MemoryHigh=1G -p MemoryMax=3G "
                "nice -n 19 ionice -c3 rsync")
STAGE_DIR = ".cache/spark-backup-stage"

STAGE_SCRIPT = r'''
import fnmatch, json, os, shutil, sqlite3, subprocess, sys
from pathlib import Path
spec = PAYLOAD
home = Path.home()
stage = home / spec["stage"]
if stage.exists():
    shutil.rmtree(stage)
stage.mkdir(parents=True)
os.chmod(stage, 0o700)
out = {"copied": [], "sqlite": [], "commands": [], "missing": []}
for pattern in spec.get("files", []):
    matches = sorted(home.glob(pattern)) if any(c in pattern for c in "*?[") else [home / pattern]
    matches = [m for m in matches if m.is_file() and m.stat().st_size <= 64 * 1024 * 1024
               and not any(fnmatch.fnmatch(m.name, pat) for pat in spec.get("never", []))]
    if not matches:
        out["missing"].append(pattern); continue
    for src in matches:
        rel = str(src.relative_to(home))
        dst = stage / "files" / rel
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(src, dst)
        out["copied"].append(rel)
for rel in spec.get("sqlite", []):
    src = home / rel
    if not src.is_file():
        out["missing"].append(rel); continue
    dst = stage / "files" / rel
    dst.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(f"file:{src}?mode=ro", uri=True, timeout=30)
    try:
        bak = sqlite3.connect(dst)
        con.backup(bak)
        bak.close()
    finally:
        con.close()
    out["sqlite"].append(rel)
for name, cmd in spec.get("commands", {}).items():
    dst = stage / "commands" / name
    dst.parent.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=120)
    dst.write_text(r.stdout)
    out["commands"].append(name)
print(json.dumps(out))
'''

HASH_SCRIPT = r'''
import hashlib, json, sys
from pathlib import Path
paths = PAYLOAD
home = Path.home()
out = {}
for rel in paths:
    h = hashlib.sha256()
    try:
        with open(home / rel, "rb") as fh:
            for block in iter(lambda: fh.read(1 << 20), b""):
                h.update(block)
        out[rel] = h.hexdigest()
    except OSError:
        out[rel] = None
print(json.dumps(out))
'''


def log(msg: str) -> None:
    print(f"[spark-backup {_dt.datetime.now().strftime('%F %T')}] {msg}", flush=True)


def load_config(path=None) -> dict:
    return json.loads(Path(path or os.environ.get("SPARK_BACKUP_CONFIG") or DEFAULT_CONFIG).read_text())


def ssh_cmd(spec: dict) -> list:
    return ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15", *spec.get("ssh_options", []),
            spec["ssh"]]


def remote_python(spec: dict, script: str, payload, timeout=900) -> dict:
    """Run ``script`` on the host at low priority; the script travels on stdin."""
    source = f"PAYLOAD = json.loads({json.dumps(json.dumps(payload))})\n"
    body = "import json\n" + source + script
    r = subprocess.run(ssh_cmd(spec) + ["nice", "-n", "19", "python3", "-"],
                       input=body.encode(), capture_output=True, timeout=timeout)
    if r.returncode != 0:
        raise RuntimeError(f"remote step failed: {r.stderr.decode(errors='replace')[-400:]}")
    return json.loads(r.stdout.decode().strip().splitlines()[-1])


def snapshots(set_dir: Path) -> list:
    out = []
    for p in set_dir.iterdir() if set_dir.exists() else []:
        try:
            _dt.date.fromisoformat(p.name)
        except ValueError:
            continue
        if p.is_dir():
            out.append(p)
    return sorted(out, key=lambda p: p.name)


def dir_bytes(path: Path) -> tuple:
    files = total = 0
    for root, _dirs, names in os.walk(path):
        for n in names:
            try:
                st = os.lstat(os.path.join(root, n))
            except OSError:
                continue
            files += 1
            total += st.st_size
    return files, total


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def keep_set(names: list, keep: dict) -> set:
    """Which dated snapshot names retention keeps."""
    dates = sorted((_dt.date.fromisoformat(n) for n in names), reverse=True)
    kept = set(dates[: int(keep.get("daily", 14))])
    weekly = [d for d in dates if d.weekday() == 6][: int(keep.get("weekly", 0))]
    monthly = [d for d in dates if d.day == 1][: int(keep.get("monthly", 0))]
    kept.update(weekly)
    kept.update(monthly)
    if dates:
        kept.add(dates[0])
    return {d.isoformat() for d in kept}


def prune(set_dir: Path, keep: dict, dry: bool = False) -> list:
    snaps = snapshots(set_dir)
    wanted = keep_set([p.name for p in snaps], keep)
    removed = []
    for p in snaps:
        if p.name not in wanted:
            removed.append(p.name)
            if not dry:
                shutil.rmtree(p)
    for p in set_dir.glob("*.partial") if set_dir.exists() else []:
        if time.time() - p.stat().st_mtime > 2 * 86400:
            removed.append(p.name)
            if not dry:
                shutil.rmtree(p, ignore_errors=True)
    return removed


def space_ok(root: Path, prev_bytes: int, cfg: dict) -> tuple:
    free = shutil.disk_usage(root).free
    min_free = float(cfg.get("min_free_gb", 60)) * 1e9
    after = free - prev_bytes
    if free < min_free:
        return False, f"only {free / 1e9:.0f} GB free (need {min_free / 1e9:.0f})"
    if after < float(cfg.get("min_free_after_gb", 50)) * 1e9:
        return False, f"a full copy would leave {after / 1e9:.0f} GB"
    return True, f"{free / 1e9:.0f} GB free"


def rsync_pull(spec: dict, rel: str, dest: Path, link: Path | None, *, runner=subprocess.run):
    """Pull one directory (``rel`` ends with /) relative to the remote home."""
    if not rel.endswith("/"):
        raise ValueError(f"paths are directories ending in '/': {rel!r} (files go in stage_files)")
    src_is_dir = True
    rel_clean = rel.rstrip("/")
    target = dest / rel_clean
    target.mkdir(parents=True, exist_ok=True)
    cmd = ["rsync", "-a", "--numeric-ids", "--delete", "--timeout=600",
           f"--rsync-path={spec.get('rsync_path') or REMOTE_RSYNC}", "-e", " ".join(shlex.quote(x) for x in ssh_cmd(spec)[:-1])]
    for pattern in NEVER + list(spec.get("exclude", [])):
        cmd.append(f"--exclude={pattern}")
    if link is not None and (link / rel_clean).is_dir():
        cmd.append(f"--link-dest={link / rel_clean}")
    remote = f"{spec['ssh']}:{rel_clean}/"
    cmd += [remote, f"{target}/"]
    del src_is_dir
    r = runner(cmd, capture_output=True, text=True, timeout=6 * 3600)
    # 24 = some files vanished during transfer (a job finished): not an error.
    if r.returncode not in (0, 24):
        raise RuntimeError(f"rsync {rel} exited {r.returncode}: {(r.stderr or '')[-400:]}")


def wait_for_precheck(spec: dict, *, runner=subprocess.run, sleep=time.sleep) -> None:
    """Run the set's host-side precheck until it passes (e.g. "no engine is
    loading and memory pressure is calm"), or give up after precheck_wait_s."""
    check = spec.get("precheck")
    if not check:
        return
    deadline = time.time() + float(spec.get("precheck_wait_s", 1800))
    while True:
        r = runner(ssh_cmd(spec) + [check], capture_output=True, text=True, timeout=60)
        if r.returncode == 0:
            return
        if time.time() >= deadline:
            raise RuntimeError("precheck never passed (host busy); skipped this run")
        log(f"{spec['ssh']}: precheck not passed yet; waiting 2 min")
        sleep(120)


def back_up(name: str, spec: dict, cfg: dict, *, today: str | None = None) -> dict:
    root = Path(os.path.expanduser(cfg["root"]))
    set_dir = root / name
    set_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    os.chmod(set_dir, 0o700)
    today = today or _dt.date.today().isoformat()
    prev = [p for p in snapshots(set_dir) if p.name != today]
    link = prev[-1] if prev else None
    last = json.loads((set_dir / "last-success.json").read_text()) if (set_dir / "last-success.json").exists() else {}
    ok, why = space_ok(root, int(last.get("bytes") or 0), cfg)
    if not ok:
        raise RuntimeError(f"space guard: {why}")
    wait_for_precheck(spec)
    started = time.time()
    partial = set_dir / f"{today}.partial"
    if partial.exists():
        shutil.rmtree(partial)
    partial.mkdir()
    staged = None
    stage_rel = f"{STAGE_DIR}/{name}"
    if spec.get("stage_files") or spec.get("stage_sqlite") or spec.get("commands"):
        staged = remote_python(spec, STAGE_SCRIPT, {
            "stage": stage_rel, "files": spec.get("stage_files", []), "never": NEVER,
            "sqlite": spec.get("stage_sqlite", []), "commands": spec.get("commands", {})})
        rsync_pull(spec, stage_rel + "/", partial / "_staged", None)
        # --link-dest for the staged tree too
    for rel in spec.get("paths", []):
        rsync_pull(spec, rel, partial, link)
    files, total = dir_bytes(partial)
    manifest = {"set": name, "date": today, "files": files, "bytes": total,
                "staged": staged, "json_sha256": {}, "media_check": {}}
    for p in sorted(partial.rglob("*.json")):
        if p.is_file() and p.stat().st_size < 64 * 1024 * 1024:
            manifest["json_sha256"][str(p.relative_to(partial))] = sha256(p)
    media = [p for p in partial.rglob("*") if p.is_file() and not p.name.endswith(".json")
             and not str(p.relative_to(partial)).startswith("_staged")]
    sample = random.sample(media, min(20, len(media)))
    if sample:
        rels = [str(p.relative_to(partial)) for p in sample]
        remote = remote_python(spec, HASH_SCRIPT, rels, timeout=900)
        for rel, p in zip(rels, sample):
            local = sha256(p)
            manifest["media_check"][rel] = {"match": remote.get(rel) == local,
                                            "vanished_on_host": remote.get(rel) is None}
    mismatched = [r for r, v in manifest["media_check"].items()
                  if not v["match"] and not v["vanished_on_host"]]
    if mismatched:
        raise RuntimeError(f"{len(mismatched)} sampled files differ from the host: {mismatched[:3]}")
    (partial / "manifest.json").write_text(json.dumps(manifest, indent=1, sort_keys=True))
    final = set_dir / today
    if final.exists():
        shutil.rmtree(final)
    partial.rename(final)
    shrunk = bool(last.get("bytes")) and total < 0.9 * float(last["bytes"])
    success = {"set": name, "snapshot": str(final), "finished": time.time(),
               "seconds": round(time.time() - started, 1), "files": files, "bytes": total,
               "previous_bytes": last.get("bytes"), "shrunk": shrunk,
               "media_checked": len(manifest["media_check"])}
    (set_dir / "last-success.json").write_text(json.dumps(success, indent=1, sort_keys=True))
    removed = prune(set_dir, spec.get("keep", cfg.get("keep", {"daily": 14})))
    success["pruned"] = removed
    return success


def verify(name: str, cfg: dict, restore_to: Path | None = None, sample_n: int = 5) -> dict:
    """Hash-check the newest snapshot; optionally restore a sample into a scratch dir."""
    root = Path(os.path.expanduser(cfg["root"]))
    snaps = snapshots(root / name)
    if not snaps:
        raise RuntimeError("no snapshot to verify")
    snap = snaps[-1]
    manifest = json.loads((snap / "manifest.json").read_text())
    bad = [rel for rel, digest in manifest["json_sha256"].items() if sha256(snap / rel) != digest]
    out = {"snapshot": str(snap), "json_checked": len(manifest["json_sha256"]), "json_bad": bad}
    if restore_to is not None:
        restore_to = Path(restore_to)
        if restore_to.exists() and any(restore_to.iterdir()):
            raise RuntimeError(f"{restore_to} is not empty; restore only into a fresh scratch dir")
        restore_to.mkdir(parents=True, exist_ok=True)
        files = [p for p in snap.rglob("*") if p.is_file()]
        picks = [snap / r for r in list(manifest["json_sha256"])[:sample_n]] + \
            random.sample(files, min(sample_n, len(files)))
        restored = {}
        for src in picks:
            rel = src.relative_to(snap)
            dst = restore_to / rel
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst)
            restored[str(rel)] = sha256(dst) == sha256(src)
        out["restored"] = restored
        out["restore_dir"] = str(restore_to)
    return out


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description="Spark nightly pull backup")
    ap.add_argument("action", choices=["run", "prune", "verify", "plan"])
    ap.add_argument("sets", nargs="*")
    ap.add_argument("--config", type=Path)
    ap.add_argument("--restore-to", type=Path)
    args = ap.parse_args(argv)
    cfg = load_config(args.config)
    names = args.sets or list(cfg["sets"])
    rc = 0
    for name in names:
        spec = cfg["sets"][name]
        if args.action == "plan":
            print(json.dumps({name: {k: spec.get(k) for k in ("ssh", "paths", "stage_files",
                                                               "stage_sqlite", "keep")}}))
        elif args.action == "prune":
            print(json.dumps({name: prune(Path(os.path.expanduser(cfg["root"])) / name,
                                          spec.get("keep", cfg.get("keep", {"daily": 14})))}))
        elif args.action == "verify":
            print(json.dumps(verify(name, cfg, args.restore_to), indent=1))
        else:
            try:
                log(f"{name}: starting")
                done = back_up(name, spec, cfg)
                log(f"{name}: ok {done['files']} files {done['bytes'] / 1e9:.2f} GB "
                    f"in {done['seconds']} s" + (" (SHRUNK)" if done["shrunk"] else ""))
            except Exception as exc:  # recorded for the health watch, then next set
                rc = 1
                log(f"{name}: FAILED {exc}")
                set_dir = Path(os.path.expanduser(cfg["root"])) / name
                set_dir.mkdir(parents=True, exist_ok=True)
                (set_dir / "last-failure.json").write_text(json.dumps(
                    {"ts": time.time(), "error": str(exc)[:500]}))
    return rc


if __name__ == "__main__":
    sys.exit(main())
