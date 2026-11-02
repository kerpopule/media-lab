#!/usr/bin/env python3
"""Hash-bound selective tagged release; private compositions stay off public git.

Called by deploy-spark.sh TAG --scoped-plan PLAN [--dry-run]. A plan binds every
selected file to its exact live preimage and tagged source, and optionally a
private composed file. It cannot delete files or replace configuration/state.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import subprocess
import sys  # noqa: F401 - referenced by the appended remote entrypoint


def digest(data):
    return hashlib.sha256(data).hexdigest()


def safe_name(name):
    p = PurePosixPath(name)
    if (
        not isinstance(name, str)
        or str(p) != name
        or p.is_absolute()
        or any(part in {"..", ".", ""} for part in p.parts)
    ):
        raise ValueError("unsafe release path")
    permitted = name in {"app.py", "web_only_policy.py"} or (
        len(p.parts) > 1
        and p.parts[0] in {"media_lab_core", "static", "tests", "docs", "tools"}
        and p.suffix in {".py", ".html", ".js", ".css", ".md", ".json"}
    )
    if not permitted or any(part.startswith(".") for part in p.parts):
        raise ValueError("path outside scoped code surface")
    return name


def build_bundle(repo, tag, plan_path):
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", tag):
        raise ValueError("invalid tag")
    commit = subprocess.check_output(
        ["git", "-C", str(repo), "rev-parse", "--verify", f"refs/tags/{tag}^{{commit}}"], text=True
    ).strip()
    plan = json.loads(plan_path.read_text())
    if plan.get("schema") != 1 or plan.get("commit") != commit:
        raise ValueError("plan/tag mismatch")
    entries = plan.get("files")
    if not isinstance(entries, list) or not entries or len(entries) > 100:
        raise ValueError("empty or oversized scoped release")
    files = []
    seen = set()
    for entry in entries:
        name = safe_name(entry["path"])
        if name in seen:
            raise ValueError("duplicate release path")
        seen.add(name)
        before = entry["before"]
        if before is not None and not re.fullmatch(r"[0-9a-f]{64}", before):
            raise ValueError("invalid preimage hash")
        tagged = subprocess.check_output(
            ["git", "-C", str(repo), "show", f"{commit}:media-lab/{name}"]
        )
        if digest(tagged) != entry["tag_sha256"]:
            raise ValueError("tagged source mismatch")
        source = entry.get("composed")
        data = (plan_path.parent / source).read_bytes() if source else tagged
        if digest(data) != entry["after"]:
            raise ValueError("payload hash mismatch")
        if name.endswith(".py"):
            compile(data, name, "exec")
        files.append(
            {
                "path": name,
                "before": before,
                "after": digest(data),
                "tag_sha256": digest(tagged),
                "composed": bool(source),
                "data": base64.b64encode(data).decode("ascii"),
            }
        )
    repository = subprocess.check_output(
        ["git", "-C", str(repo), "remote", "get-url", "origin"], text=True
    ).strip()
    return {"schema": 1, "tag": tag, "commit": commit, "repository": repository, "files": files}


# Remote entry point uses only stdlib; it never imports the application or GPU code.
def apply_bundle(bundle, root, dry_run=False, queue=None, restart=None):
    import datetime
    import fcntl
    import tempfile
    import urllib.request

    root = Path(root).absolute()
    if root.is_symlink() or not root.is_dir():
        raise ValueError("invalid deployment root")
    if bundle.get("schema") != 1 or not bundle.get("files"):
        raise ValueError("invalid bundle")
    backup_root = root / ".backups"
    if backup_root.is_symlink():
        raise ValueError("symlink backup root")
    backup_root.mkdir(exist_ok=True)
    lock_path = backup_root / "scoped-release.lock"
    if lock_path.is_symlink():
        raise ValueError("symlink deployment lock")
    with lock_path.open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        rows = bundle["files"]
        names = [safe_name(e["path"]) for e in rows]
        if len(names) != len(set(names)) or len(rows) > 100:
            raise ValueError("duplicate or oversized release")
        payloads = {}
        for entry in rows:
            data = base64.b64decode(entry["data"], validate=True)
            if digest(data) != entry["after"]:
                raise ValueError("payload corruption")
            if entry["path"].endswith(".py"):
                compile(data, entry["path"], "exec")
            payloads[entry["path"]] = data
        receipt_path = root / "deployed-source.json"
        if receipt_path.is_symlink():
            raise ValueError("symlink deployment receipt")
        old_receipt_bytes = receipt_path.read_bytes()
        old_receipt = json.loads(old_receipt_bytes)
        reservation = root.parent / ".local/state/spark-pair-control/reservation.json"
        reserved_before = reservation.read_bytes() if reservation.exists() else None
        service = "media-lab-web.service" if reserved_before else "media-lab-simple.service"

        def preimages():
            for entry in rows:
                target = root / entry["path"]
                for component in [target, *target.parents]:
                    if component == root:
                        break
                    if component.is_symlink():
                        raise ValueError("symlink release target")
                actual = digest(target.read_bytes()) if target.exists() else None
                if actual != entry["before"]:
                    raise ValueError("live preimage changed: " + entry["path"])
            if receipt_path.read_bytes() != old_receipt_bytes:
                raise ValueError("deployment receipt changed")
            now_reserved = reservation.read_bytes() if reservation.exists() else None
            if now_reserved != reserved_before:
                raise ValueError("pair ownership changed during staging")

        def actual_queue():
            # Read only the required bind value; never log the token/config.
            host = "127.0.0.1"
            for line in (root / "config/local.env").read_text().splitlines():
                if line.startswith("MEDIA_LAB_BIND_HOST="):
                    host = line.split("=", 1)[1].strip().strip("\"'")
            token = (root / "local-token.txt").read_text().strip()
            if not token:
                raise ValueError("missing local authentication")
            req = urllib.request.Request(
                f"http://{host}:7863/api/queue?hist=0", headers={"X-Media-Lab-Local": token}
            )
            with urllib.request.urlopen(req, timeout=5) as response:
                return json.load(response)

        def known_queue():
            doc = (queue or actual_queue)()
            if not isinstance(doc, dict) or not (
                "active_total" in doc or isinstance(doc.get("active"), list)
            ):
                raise ValueError("queue state unknown")
            active = doc.get("active_total", len(doc.get("active", [])))
            if type(active) is not int or active < 0:
                raise ValueError("queue state unknown")
            return doc, active

        def idle():
            doc, active = known_queue()
            if active != 0 or doc.get("active"):
                raise ValueError("queue is active")

        def real_restart(unit):
            subprocess.run(["systemctl", "--user", "restart", unit], check=True, timeout=90)

        preimages()
        idle()
        summary = {
            "tag": bundle["tag"],
            "commit": bundle["commit"],
            "service": service,
            "deployed_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
            "files": [{k: v for k, v in e.items() if k != "data"} for e in rows],
        }
        if dry_run:
            return dict(summary, status="dry-run", live_changed=False)
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S")
        backup = Path(tempfile.mkdtemp(prefix=f"scoped-{stamp}-", dir=backup_root))
        stage = backup / "staged"
        stage.mkdir()
        originals = {}
        modes = {}
        for entry in rows:
            name = entry["path"]
            target = root / name
            originals[name] = target.read_bytes() if target.exists() else None
            modes[name] = target.stat().st_mode & 0o777 if target.exists() else 0o644
            if originals[name] is not None:
                p = backup / "preimage" / name
                p.parent.mkdir(parents=True, exist_ok=True)
                p.write_bytes(originals[name])
            p = stage / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(payloads[name])
            p.chmod(modes[name])
        summary["backup"] = str(backup)
        (backup / "manifest.json").write_text(json.dumps(summary, indent=2) + "\n")
        (backup / "deployed-source.before.json").write_bytes(old_receipt_bytes)
        preimages()
        idle()
        preimages()  # Queue probing must not conceal a concurrent owner transition.
        changed = []
        try:
            for name in names:
                target = root / name
                target.parent.mkdir(parents=True, exist_ok=True)
                os.replace(stage / name, target)
                changed.append(name)
            current_reservation = reservation.read_bytes() if reservation.exists() else None
            if current_reservation != reserved_before:
                raise ValueError("pair ownership changed before restart")
            (restart or real_restart)(service)
            for entry in rows:
                if digest((root / entry["path"]).read_bytes()) != entry["after"]:
                    raise ValueError("installed file readback failed: " + entry["path"])
            # Readiness is known authenticated JSON, not idleness: a healthy
            # new job after restart must never trigger a rollback/restart.
            import time

            deadline = time.monotonic() + (0 if queue else 90)
            while True:
                try:
                    known_queue()
                    break
                except Exception:
                    if time.monotonic() >= deadline:
                        raise
                    time.sleep(2)
            updated = dict(old_receipt)
            updated["scoped_releases"] = [*old_receipt.get("scoped_releases", []), summary]
            tmp = backup / "deployed-source.next.json"
            tmp.write_text(json.dumps(updated, indent=2) + "\n")
            os.replace(tmp, receipt_path)
            if json.loads(receipt_path.read_text()) != updated:
                raise ValueError("receipt readback failed")
        except BaseException:
            # Restore only this release's known files, in reverse order. Do not
            # erase an intervening writer's bytes. Keep all backup evidence.
            rollback_conflicts = []
            for name in reversed(changed):
                target = root / name
                if (
                    target.is_symlink()
                    or not target.exists()
                    or digest(target.read_bytes()) != digest(payloads[name])
                ):
                    rollback_conflicts.append(name)
                    continue
                if originals[name] is None:
                    target.unlink()
                else:
                    tmp = backup / "restore.tmp"
                    tmp.write_bytes(originals[name])
                    tmp.chmod(modes[name])
                    os.replace(tmp, target)
            if receipt_path.read_bytes() != old_receipt_bytes:
                # Do not clobber another deployment's receipt.
                if json.loads(receipt_path.read_text()).get("scoped_releases", [])[-1:] == [
                    summary
                ]:
                    tmp = backup / "receipt-restore.tmp"
                    tmp.write_bytes(old_receipt_bytes)
                    os.replace(tmp, receipt_path)
            owner_unchanged = (
                reservation.read_bytes() if reservation.exists() else None
            ) == reserved_before
            if not rollback_conflicts and owner_unchanged:
                (restart or real_restart)(service)
            elif rollback_conflicts:
                raise RuntimeError(
                    "rollback refused foreign writes: " + ",".join(rollback_conflicts)
                )
            else:
                raise RuntimeError(
                    "code rolled back; restart withheld because pair ownership changed"
                )
            raise
        return dict(summary, status="installed", live_changed=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("tag")
    ap.add_argument("--scoped-plan", type=Path, required=True)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    repo = Path(subprocess.check_output(["git", "rev-parse", "--show-toplevel"], text=True).strip())
    bundle = build_bundle(repo, args.tag, args.scoped_plan.resolve())
    spark = os.environ.get("SPARK", "")
    if not re.fullmatch(r"[A-Za-z0-9_.-]+@[A-Za-z0-9_.-]+", spark):
        raise ValueError("set explicit SPARK=user@host")
    # Execute the exact tagged helper, not an unrelated installed host copy.
    code = subprocess.check_output(
        ["git", "-C", str(repo), "show", f"{bundle['commit']}:media-lab/tools/scoped_release.py"],
        text=True,
    ).rsplit('\nif __name__ == "__main__":', 1)[0]
    code += (
        "\nprint(json.dumps(apply_bundle(json.load(sys.stdin), Path.home()/'media-lab-simple', dry_run="
        + repr(args.dry_run)
        + "), indent=2))\n"
    )
    result = subprocess.run(
        [
            "ssh",
            "-o",
            "BatchMode=yes",
            "-o",
            "ConnectTimeout=10",
            spark,
            "python3 -c " + shlex.quote(code),
        ],
        input=json.dumps(bundle),
        text=True,
        timeout=300,
    )
    if result.returncode:
        raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
