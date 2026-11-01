#!/usr/bin/env python3
"""Queue an externally assembled film and block until Storyboard readback passes.

This is the only supported handoff for a bespoke/out-of-process stitch. The
sidecar is installed before the media file, then the media rename makes the pair
visible atomically to the Spark inbox watcher. Successful return proves:
  1. an assembly_import job ran through the Media Lab queue;
  2. the exact SHA-256 is bound to the requested storyboard;
  3. the job and board readbacks agree on URL, hash, and board id.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import re
import shlex
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from media_lab_core import local_config   # config/local.env, stdlib only
from media_lab_core import local_token    # local-token.txt / MEDIA_LAB_CODE sign-in

# The studio host's ssh target (MEDIA_LAB_SSH) and API; pass --remote/--api to
# override. The inbox path is relative to the remote user's home.
DEFAULT_REMOTE = local_config.get("MEDIA_LAB_SSH")
DEFAULT_API = local_config.studio_url()
REMOTE_INBOX = "media-lab-simple/inbox"


def _candidate_until_approved(board: dict) -> bool:
    """Same rule as media_lab_core.cut.candidate_until_approved (older boards
    carry the flag under an owner-named key)."""
    return any(isinstance(k, str) and k.startswith("candidate_not_final_until_")
               and k.endswith("_approves") and v is True for k, v in board.items())


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


# Set in main(): the local token on the studio box, else a session from the
# family code in MEDIA_LAB_CODE (the studio trusts no Host header or network).
OPENER = urllib.request.build_opener(local_token.StudioAuthHandler())


def get_json(url: str) -> Any:
    with OPENER.open(url, timeout=20) as response:
        return json.load(response)


def run(command: list[str]) -> None:
    subprocess.run(command, check=True)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("--board-id", required=True)
    parser.add_argument("--title", default="")
    parser.add_argument("--remote", default=DEFAULT_REMOTE)
    parser.add_argument("--api", default=DEFAULT_API)
    parser.add_argument("--timeout", type=int, default=240)
    args = parser.parse_args()
    global OPENER
    OPENER = local_token.studio_opener(args.api, os.environ.get("MEDIA_LAB_CODE") or None)

    source = args.source.expanduser().resolve()
    if not source.is_file() or source.suffix.lower() not in {".mp4", ".mov", ".m4v", ".webm"}:
        raise SystemExit(f"Not a supported assembled video: {source}")
    board_id = args.board_id.strip()
    boards = get_json(args.api.rstrip("/") + "/api/storyboards")
    matches = [board for board in boards if board.get("id") == board_id]
    if len(matches) != 1:
        raise SystemExit(f"Expected exactly one live storyboard {board_id!r}; found {len(matches)}")
    board = matches[0]
    digest = sha256_file(source)
    safe_board = re.sub(r"[^A-Za-z0-9_.-]+", "-", board_id).strip("-.") or "storyboard"
    remote_name = f"assembly-{safe_board}-{digest[:12]}.mp4"
    sidecar = {
        "assembly": True,
        "board_id": board_id,
        "sha256": digest,
        "title": (args.title.strip() or str(board.get("title") or "Storyboard film"))[:120],
        "private_internal_only": True,
        "candidate_not_final_until_owner_approves": True,
        "publication_authorized": False,
        "external_sharing_authorized": False,
    }

    with tempfile.TemporaryDirectory() as temp_dir:
        side_path = Path(temp_dir) / f"{remote_name}.json"
        side_path.write_text(json.dumps(sidecar, indent=2) + "\n")
        run(["ssh", "-o", "BatchMode=yes", args.remote, "mkdir", "-p", REMOTE_INBOX])
        # Sidecar first. The .part media suffix is not watched; the final rename is
        # the single event that exposes a complete, hash-bound assembly pair.
        run(["scp", "-q", str(side_path), f"{args.remote}:{REMOTE_INBOX}/{remote_name}.json.part"])
        run(["ssh", "-o", "BatchMode=yes", args.remote,
             f"mv {shlex.quote(REMOTE_INBOX + '/' + remote_name + '.json.part')} "
             f"{shlex.quote(REMOTE_INBOX + '/' + remote_name + '.json')}"])
        run(["scp", "-q", str(source), f"{args.remote}:{REMOTE_INBOX}/{remote_name}.part"])
        run(["ssh", "-o", "BatchMode=yes", args.remote,
             f"mv {shlex.quote(REMOTE_INBOX + '/' + remote_name + '.part')} "
             f"{shlex.quote(REMOTE_INBOX + '/' + remote_name)}"])

    deadline = time.monotonic() + max(20, args.timeout)
    last = {}
    while time.monotonic() < deadline:
        time.sleep(2)
        boards = get_json(args.api.rstrip("/") + "/api/storyboards")
        live = next((item for item in boards if item.get("id") == board_id), {})
        job_id = str(live.get("last_assembly_job_id") or "")
        last = {"board": live, "job_id": job_id}
        if live.get("final_sha256") != digest or not job_id:
            continue
        try:
            job = get_json(args.api.rstrip("/") + f"/api/jobs/{job_id}")
        except urllib.error.HTTPError:
            continue
        last["job"] = job
        checks = {
            "job_kind": job.get("kind") == "assembly_import",
            "job_done": job.get("status") == "done",
            "job_board": job.get("board_id") == board_id,
            "job_hash": job.get("sha256") == digest,
            "job_storyboard_registered": job.get("storyboard_registered") is True,
            "job_url_matches_board": job.get("url") == live.get("final_url"),
            "board_private": live.get("private_internal_only") is True,
            "board_not_final": _candidate_until_approved(live),
            "board_not_public": live.get("publication_authorized") is False,
        }
        if all(checks.values()):
            print(json.dumps({
                "ok": True, "board_id": board_id, "job_id": job_id,
                "final_url": live.get("final_url"), "sha256": digest,
                "checks": checks,
            }, indent=2))
            return
        if job.get("status") in {"failed", "cancelled"}:
            raise SystemExit(json.dumps({"error": "assembly queue job failed", "state": last}, indent=2))
    raise SystemExit(json.dumps({"error": "timed out waiting for queue/storyboard receipt", "state": last}, indent=2))


if __name__ == "__main__":
    main()
