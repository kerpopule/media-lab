"""Time the exact application's finalizer and Library gate without AI/GPU work.
The isolated source/Library fixtures are local; all media is produced by ffmpeg.
"""

import argparse
import ast
import json
import os
import time
from pathlib import Path
from typing import Any

from media_lab_core.sdr_delivery import digest


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("source", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=False)
    source = (Path(__file__).parents[1] / "app.py").read_text()
    functions: list[ast.stmt] = [
        n
        for n in ast.parse(source).body
        if isinstance(n, ast.FunctionDef) and n.name in ("_finish_video", "gallery_add")
    ]
    job: dict[str, Any] = {
        "id": "timed",
        "kind": "video",
        "prompt": "Private delivery benchmark",
        "status": "running",
    }
    before = digest(args.source)

    def fail(job, reason, detail=""):
        raise RuntimeError(reason + " " + detail)

    def save(path, value):
        Path(path).write_text(json.dumps(value, indent=2) + "\n")

    scope: dict[str, Any] = {
        "Path": Path,
        "os": os,
        "time": time,
        "ROOT": args.output,
        "MEDIA": args.output,
        "JOBS_DIR": args.output / "jobs",
        "jobs": {"timed": job},
        "save_state": lambda: None,
        "fail": fail,
        "_maybe_face_fix": lambda *a: None,
        "_save": save,
        "_load": lambda path, default: (
            json.loads(Path(path).read_text()) if Path(path).exists() else default
        ),
        "media_path": lambda url: args.output / Path(url).name if url else None,
    }
    exec(  # noqa: S102 - owned source's exact application hooks
        compile(ast.Module(body=functions, type_ignores=[]), "<exact-app-benchmark>", "exec"), scope
    )
    start = time.perf_counter()
    scope["_finish_video"](job, args.source)
    elapsed = time.perf_counter() - start
    gallery = json.loads((args.output / "gallery.json").read_text())
    assert job["status"] == "done" and len(gallery) == 1
    assert gallery[0]["variants"] == job["variants"]
    assert before == digest(args.source) == digest(args.output / "jobs/timed/engine-original.mp4")
    assert (args.output / "timed.jpg").stat().st_size > 0
    sdr = job["hdr_delivery"]["sdr_delivery"]
    result = {
        "scope": "Exact finalizer + Library hooks, original retention, encodes, repeated technical validation, poster and local Library JSON commit; no generation or face repair",
        "elapsed_seconds": round(elapsed, 3),
        "source_sha256": before,
        "source_unchanged": True,
        "status": job["status"],
        "variants": job["variants"],
        "sdr_encode_seconds": round(sdr["encode_seconds"], 3),
        "sdr_verify_seconds": round(sdr["verify_seconds"], 3),
        "hdr_sha256": digest(args.output / "timed.mp4"),
        "sdr_sha256": digest(args.output / "timed.sdr.mp4"),
    }
    (args.output / "application-benchmark.json").write_text(json.dumps(result, indent=2) + "\n")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
