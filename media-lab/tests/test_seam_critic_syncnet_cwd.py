"""The critic's SyncNet run happens inside the LatentSync checkout: S3FD opens
checkpoints/auxiliary/sfd_face.pth relative to it. On 2026-09-26 every lip-sync
row of the director proof said "not measured" (FileNotFoundError) because the
subprocess ran in the caller's directory."""
import json
import os
import stat
from pathlib import Path

from media_lab_core import seam_critic


def test_runner_runs_in_the_checkout_with_an_absolute_video(tmp_path, monkeypatch):
    root = tmp_path / "LatentSync"; (root / "checkpoints/auxiliary").mkdir(parents=True)
    (root / "checkpoints/auxiliary/sfd_face.pth").write_text("x")
    fake = tmp_path / "python"
    fake.write_text("#!/bin/sh\n"
                    "test -f checkpoints/auxiliary/sfd_face.pth || exit 3\n"
                    "case \"$2\" in /*) ;; *) exit 4;; esac\n"
                    "echo \'{\"face_track\": true, \"av_offset_frames\": 0, \"confidence\": 7.5}\'\n")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("MEDIA_LAB_SYNCNET_PYTHON", str(fake))
    monkeypatch.setenv("MEDIA_LAB_LATENTSYNC_ROOT", str(root))
    monkeypatch.chdir(tmp_path)
    (tmp_path / "shot.mp4").write_bytes(b"")
    run = seam_critic.syncnet_runner()
    assert run("shot.mp4") == {"face_track": True, "av_offset_frames": 0, "confidence": 7.5}
