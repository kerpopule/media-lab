"""Library gate for a verified HDR + SDR pair, including legacy producers."""

import os
import tempfile
from pathlib import Path

from . import hdr_delivery, hdr_finish, sdr_delivery


def enforce(
    source, sibling, job_dir, *, expected_sdr=None, input_source=None, cancelled=lambda: False
):
    source, sibling, job_dir = Path(source), Path(sibling), Path(job_dir)
    if cancelled():
        raise RuntimeError("Cancelled before Library publication")
    if source.resolve() == sibling.resolve():
        raise ValueError("HDR and SDR paths must differ")
    if sibling.exists():
        # Only the finishing transaction may supply an existing pair. Never adopt
        # an unrelated/stale file simply because its name and color tags match.
        if not expected_sdr or sdr_delivery.digest(sibling) != expected_sdr["sha256"]:
            raise ValueError("Existing SDR sibling has no matching transaction receipt")
        hdr = hdr_finish.verify(source, "x264", cancelled=cancelled)
        hdr["sdr_delivery"] = sdr_delivery.verify(sibling, cancelled=cancelled)
        return hdr
    info = hdr_finish.probe(source)
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    if input_source and Path(input_source).is_file():
        prior = next(
            s for s in hdr_finish.probe(input_source)["streams"] if s["codec_type"] == "video"
        )
        if prior.get("color_transfer") in ("arib-std-b67", "smpte2084") and video.get(
            "color_transfer"
        ) not in ("arib-std-b67", "smpte2084"):
            raise ValueError("Finishing engine lost HDR source signaling")
    job_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="dual-library-", dir=job_dir) as temp:
        hdr_path, sdr_path = Path(temp) / "hdr.mp4", Path(temp) / "sdr.mp4"
        if video.get("color_transfer") == "arib-std-b67":
            hdr_delivery.retain_original(source, job_dir / "retained")
            hdr = hdr_finish.verify(source, "x264", cancelled=cancelled)
            hdr["sdr_delivery"] = sdr_delivery.encode(source, sdr_path, cancelled=cancelled)
        else:
            hdr = hdr_delivery.prepare(
                source, hdr_path, job_dir / "retained", sdr_target=sdr_path, cancelled=cancelled
            )
        if cancelled():
            raise RuntimeError("Cancelled before Library publication")
        # The Library row is the commit point. Both files are verified first.
        # os.replace is per-file atomic, not a multi-file transaction.
        os.replace(sdr_path, sibling)
        try:
            if hdr_path.exists():
                os.replace(hdr_path, source)
        except BaseException:
            sibling.unlink(missing_ok=True)
            raise
    return hdr
