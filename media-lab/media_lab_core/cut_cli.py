"""Cut CLI — drive the Media Lab timeline editor from a shell or an agent.

    python -m media_lab_core.cut_cli projects
    python -m media_lab_core.cut_cli new --job 1a2b3c --job 4d5e6f --name "Two takes"
    python -m media_lab_core.cut_cli trim <project> <clip> --in 12 --out 96
    python -m media_lab_core.cut_cli render <project> --quality preview --wait

Talks to the local API (``--url``, default http://127.0.0.1:7863).  The session
cookie comes from ``POST /api/gate`` with the family or admin code (``--code`` or
``MEDIA_LAB_CODE``).  On the studio machine itself no code is needed: the CLI
sends the local tool token (local-token.txt) to the studio's own addresses — the
studio no longer trusts a tailnet or localhost Host header.  ``--json`` prints
the raw API answer for agents.  Every edit is one transaction with a fresh id and the
project's current revision, so a stale view fails closed instead of clobbering.
"""

from __future__ import annotations

import argparse
import http.cookiejar
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from typing import Any

from . import local_token

DEFAULT_URL = "http://127.0.0.1:7863"


class CliError(SystemExit):
    def __init__(self, message: str, code: int = 2):
        super().__init__(code)
        self.message = message


class Client:
    def __init__(self, url: str, code: str | None = None, timeout: float = 60.0):
        self.url = url.rstrip("/")
        self.timeout = timeout
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar),
                                                  local_token.StudioAuthHandler())
        self.code = code

    def request(self, method: str, path: str, body: Any | None = None) -> Any:
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(self.url + path, data=data, method=method, headers=headers)
        try:
            with self.opener.open(req, timeout=self.timeout) as resp:
                text = resp.read().decode("utf-8")
                return json.loads(text) if text else {}
        except urllib.error.HTTPError as exc:
            text = exc.read().decode("utf-8", "replace")
            try:
                payload = json.loads(text)
            except json.JSONDecodeError:
                payload = {"error": text[:300] or exc.reason}
            if exc.code == 401 and self.code and not getattr(self, "_signed_in", False):
                self.sign_in()
                return self.request(method, path, body)
            raise CliError(f"{method} {path} -> {exc.code}: {payload.get('error') or payload}")
        except urllib.error.URLError as exc:
            raise CliError(f"cannot reach {self.url}: {exc.reason}")

    def sign_in(self) -> None:
        if not self.code:
            raise CliError("this studio needs a code: pass --code or set MEDIA_LAB_CODE")
        self._signed_in = True
        result = self.request("POST", "/api/gate", {"code": self.code})
        if not result.get("ok"):
            raise CliError("the studio refused that code")

    def get(self, path: str) -> Any:
        return self.request("GET", path)

    def post(self, path: str, body: Any) -> Any:
        return self.request("POST", path, body)


# ---------- argument → command mapping (pure, tested) ----------

def _cmd(kind: str, payload: dict[str, Any]) -> dict[str, Any]:
    return {"id": f"cli-{kind.replace('.', '-')}-{uuid.uuid4().hex[:8]}", "type": kind,
            "payload": {k: v for k, v in payload.items() if v is not None}}


def build_commands(args: argparse.Namespace) -> list[dict[str, Any]]:
    """Map a parsed CLI invocation to the Cut command list it will send."""

    verb = args.command
    if verb == "trim":
        return [_cmd("clip.trim", {"clip_id": args.clip, "trim_in_frames": args.trim_in,
                                   "trim_out_frames": args.trim_out})]
    if verb == "split":
        return [_cmd("clip.split", {"clip_id": args.clip, "at_frame": args.at,
                                    "new_clip_id": args.new_id})]
    if verb == "move":
        return [_cmd("clip.move", {"clip_id": args.clip, "track_id": args.track,
                                   "start_frame": args.start})]
    if verb == "add":
        return [_cmd("clip.add", {"job_id": args.job, "track_id": args.track, "start_frame": args.start,
                                  "duration_seconds": args.seconds})]
    if verb == "remove":
        return [_cmd("clip.remove", {"clip_id": args.clip})]
    if verb == "transition":
        if args.clear:
            return [_cmd("transition.remove", {"from_clip_id": args.from_clip, "to_clip_id": args.to_clip})]
        return [_cmd("transition.set", {"from_clip_id": args.from_clip, "to_clip_id": args.to_clip,
                                        "kind": args.kind, "duration_frames": args.frames})]
    if verb == "caption":
        sub = args.caption_command
        if sub == "add":
            return [_cmd("caption.add", {"text": args.text, "start_frame": args.start,
                                         "end_frame": args.end, "caption_id": args.id})]
        if sub == "edit":
            return [_cmd("caption.edit", {"caption_id": args.id, "text": args.text,
                                          "start_frame": args.start, "end_frame": args.end})]
        if sub == "generate":
            return [_cmd("caption.generate", {"scene_id": args.scene, "text": args.text})]
        if sub == "remove":
            return [_cmd("caption.remove", {"caption_id": args.id})]
    if verb == "mix":
        payload: dict[str, Any] = {"target": args.target, "gain_db": args.gain}
        if args.target == "clip":
            payload["clip_id"] = args.clip
            payload["muted"] = args.mute if args.mute is not None else None
        else:
            payload["normalize"] = args.normalize if args.normalize is not None else None
            payload["target_lufs"] = args.lufs
        return [_cmd("audio.mix", payload)]
    if verb == "color":
        return [_cmd("color.apply", {"clip_id": args.clip, "preset": args.preset, "intensity": args.intensity})]
    if verb == "approve-master":
        return [_cmd("project.approve", {"master_render_approved": not args.revoke})]
    if verb in {"undo", "redo"}:
        return [_cmd(verb, {})]
    if verb == "restore":
        return [_cmd("restore", {"revision": args.revision})]
    if verb == "diff":
        return [_cmd("diff", {})]
    if verb == "apply":
        raw = json.loads(args.json_commands)
        if isinstance(raw, dict):
            raw = [raw]
        if not isinstance(raw, list) or not raw:
            raise CliError("--json must be a command object or a non-empty list of them")
        out = []
        for index, item in enumerate(raw):
            if not isinstance(item, dict) or "type" not in item:
                raise CliError(f"command {index + 1} needs a type")
            out.append({"id": str(item.get("id") or f"cli-{index + 1}-{uuid.uuid4().hex[:6]}"),
                        "type": item["type"], "payload": item.get("payload") or {}})
        return out
    raise CliError(f"{verb} does not map to commands")


# ---------- printing ----------

def _fmt_time(frames: int, fps: int) -> str:
    seconds = frames / max(1, fps)
    return f"{int(seconds // 60):02d}:{seconds % 60:05.2f}"


def print_project(project: dict[str, Any]) -> None:
    fps = int(project["settings"]["fps"])
    print(f"{project['title']}  [{project['project_id']}]  rev {project['revision']}  "
          f"{project['settings']['width']}x{project['settings']['height']} @ {fps} fps  "
          f"{project['duration_seconds']}s")
    for track in project["timeline"]["tracks"]:
        print(f"  {track['name']} ({track['type']})")
        for clip in track["clips"]:
            end = clip["start_frame"] + clip["duration_frames"]
            print(f"    {clip['id']:<34} {_fmt_time(clip['start_frame'], fps)} → {_fmt_time(end, fps)}"
                  f"  src {clip['trim_in_frame']}-{clip['trim_out_frame']}f  {clip.get('label', '')[:40]}")
    for t in project["timeline"]["transitions"]:
        print(f"  ⤳ {t['kind']} {t['duration_frames']}f  {t['from_clip_id']} → {t['to_clip_id']}")
    for c in project["timeline"]["captions"]["items"]:
        print(f"  💬 {c['id']}  {_fmt_time(c['start_frame'], fps)}–{_fmt_time(c['end_frame'], fps)}  {c['text'][:60]}")
    for clip_id, spec in (project["timeline"].get("color") or {}).items():
        print(f"  🎨 {clip_id}: {spec['preset']} @ {spec['intensity']}")
    print(f"  🔊 mix: {json.dumps(project['timeline']['mix'])}")
    print(f"  master approved: {project['approval'].get('master_render_approved')}")


def emit(args: argparse.Namespace, payload: Any, human: Any = None) -> None:
    if args.json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    elif human is not None:
        print(human)
    else:
        print(json.dumps(payload, indent=2, sort_keys=True))


# ---------- verbs ----------

def transact(client: Client, args: argparse.Namespace, commands: list[dict[str, Any]], proposed: bool = False) -> Any:
    project = client.get(f"/api/cut/projects/{args.project}")
    body = {"commands": commands, "transaction_id": f"cli-{uuid.uuid4().hex[:12]}",
            "expected_revision": int(project["revision"])}
    return client.post(f"/api/cut/projects/{args.project}/commands", body)


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2
    try:
        return run(args)
    except CliError as exc:
        print(f"cut: {exc.message}", file=sys.stderr)
        return exc.code


def run(args: argparse.Namespace) -> int:
    client = Client(args.url, args.code or os.getenv("MEDIA_LAB_CODE") or None)
    verb = args.command
    if verb == "projects":
        data = client.get("/api/cut/projects")
        rows = data.get("projects", [])
        emit(args, data, "\n".join(
            f"{r.get('project_id'):<20} rev {r.get('revision', '?'):<3} {str(r.get('duration_seconds', '')):>7}s  "
            f"{r.get('clip_count', 0)} clips  {r.get('title', '')}" for r in rows) or "no projects yet")
        return 0
    if verb == "new":
        body = {"job_ids": args.job or [], "name": args.name or "", "still_seconds": args.still}
        if args.storyboard:
            body["storyboard"] = args.storyboard
        data = client.post("/api/cut/projects", body)
        emit(args, data, f"created {data.get('project_id')}  →  {client.url}/cut?project={data.get('project_id')}")
        if not args.json:
            print_project(data["project"])
        return 0
    if verb == "show":
        project = client.get(f"/api/cut/projects/{args.project}")
        if args.json:
            emit(args, project)
        else:
            print_project(project)
        return 0
    if verb == "pending":
        data = client.get(f"/api/cut/projects/{args.project}/pending")
        emit(args, data, "\n".join(
            f"{p['transaction_id']}  base rev {p['base_revision']}  {len(p.get('diff') or [])} change(s): "
            + ", ".join(c.get('type') for c in p.get('commands', []))
            for p in data.get("pending", [])) or "nothing pending")
        return 0
    if verb in {"approve", "reject"}:
        data = client.post(f"/api/cut/projects/{args.project}/review/{args.transaction}", {"approve": verb == "approve"})
        emit(args, data, f"{data.get('status')}  rev {data.get('revision')}")
        return 0
    if verb == "render":
        body = {"quality": args.quality, "format": args.format, "include_audio": not args.no_audio,
                "burn_captions": not args.no_captions, "explicit_approval": args.explicit_approval,
                "import_to_gallery": not args.no_import}
        if args.range:
            start, end = (float(x) for x in args.range.split(":", 1))
            body.update({"range_mode": "selection", "range_start_seconds": start, "range_end_seconds": end})
        data = client.post(f"/api/cut/projects/{args.project}/render", body)
        render_id = data.get("render_id")
        if not args.wait:
            emit(args, data, f"render {render_id} queued — poll: cut status {render_id}")
            return 0
        last = None
        while True:
            rec = client.get(f"/api/cut/renders/{render_id}")
            if not args.json and rec.get("progress") != last:
                last = rec.get("progress")
                print(f"\r  {rec.get('stage'):<10} {int((last or 0) * 100):3d}%", end="", flush=True)
            if rec.get("status") in {"done", "error"}:
                if not args.json:
                    print()
                emit(args, rec, f"{rec.get('status')}: {rec.get('url') or rec.get('message')}  "
                                f"sha256 {((rec.get('receipt') or {}).get('sha256') or '')[:16]}")
                return 0 if rec.get("status") == "done" else 1
            time.sleep(1.0)
    if verb == "status":
        rec = client.get(f"/api/cut/renders/{args.render}")
        emit(args, rec, f"{rec.get('status')} {rec.get('stage')} {int((rec.get('progress') or 0) * 100)}%  {rec.get('url') or rec.get('message') or ''}")
        return 0
    commands = build_commands(args)
    result = transact(client, args, commands)
    summary = f"{result.get('status')}  rev {result.get('revision')}  {len(result.get('diff') or [])} change(s)"
    if verb == "diff" and not args.json:
        print(json.dumps(result.get("diff"), indent=2))
    emit(args, result, summary)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cut", description="Media Lab Cut — timeline editor CLI")
    parser.add_argument("--url", default=os.getenv("MEDIA_LAB_URL", DEFAULT_URL))
    parser.add_argument("--code", default=None, help="family or admin code (or MEDIA_LAB_CODE)")
    parser.add_argument("--json", action="store_true", help="print raw JSON")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("projects", help="list projects")
    new = sub.add_parser("new", help="create a project from gallery job ids")
    new.add_argument("--job", action="append", help="gallery job id (repeatable)")
    new.add_argument("--name", default="")
    new.add_argument("--still", type=float, default=4.0, help="seconds a picture stays on screen")
    new.add_argument("--storyboard", default="", help="optional storyboard.json inside the storyboard folder")

    def project_parser(name: str, help_text: str) -> argparse.ArgumentParser:
        p = sub.add_parser(name, help=help_text)
        p.add_argument("project")
        return p

    project_parser("show", "print the timeline")
    p = project_parser("trim", "set a clip's source in/out (frames)")
    p.add_argument("clip"); p.add_argument("--in", dest="trim_in", type=int, required=True)
    p.add_argument("--out", dest="trim_out", type=int, required=True)
    p = project_parser("split", "split a clip at an absolute timeline frame")
    p.add_argument("clip"); p.add_argument("--at", type=int, required=True); p.add_argument("--new-id", dest="new_id")
    p = project_parser("move", "move a clip to a track/frame")
    p.add_argument("clip"); p.add_argument("--track", default="track-video-main"); p.add_argument("--start", type=int, required=True)
    p = project_parser("add", "add a gallery item as a clip")
    p.add_argument("--job", required=True); p.add_argument("--track"); p.add_argument("--start", type=int)
    p.add_argument("--seconds", type=float, help="length for a picture")
    p = project_parser("remove", "remove a clip")
    p.add_argument("clip")
    p = project_parser("transition", "set (or --clear) a transition between two touching clips")
    p.add_argument("from_clip"); p.add_argument("to_clip"); p.add_argument("--kind", default="dissolve")
    p.add_argument("--frames", type=int, default=12); p.add_argument("--clear", action="store_true")
    cap = project_parser("caption", "captions")
    caps = cap.add_subparsers(dest="caption_command", required=True)
    c = caps.add_parser("add"); c.add_argument("--text", required=True); c.add_argument("--start", type=int, required=True)
    c.add_argument("--end", type=int, required=True); c.add_argument("--id")
    c = caps.add_parser("edit"); c.add_argument("id"); c.add_argument("--text", required=True)
    c.add_argument("--start", type=int); c.add_argument("--end", type=int)
    c = caps.add_parser("generate"); c.add_argument("--scene", required=True); c.add_argument("--text")
    c = caps.add_parser("remove"); c.add_argument("id")
    p = project_parser("mix", "audio mix: --target dialogue|music|master|clip")
    p.add_argument("--target", default="dialogue"); p.add_argument("--gain", type=float)
    p.add_argument("--normalize", type=lambda v: v.lower() in {"1", "true", "yes", "on"}, default=None)
    p.add_argument("--lufs", type=float); p.add_argument("--clip")
    p.add_argument("--mute", type=lambda v: v.lower() in {"1", "true", "yes", "on"}, default=None)
    p = project_parser("color", "colour look on a clip")
    p.add_argument("clip"); p.add_argument("--preset", default="neutral"); p.add_argument("--intensity", type=float, default=1.0)
    p = project_parser("approve-master", "mark the project approved for a master render")
    p.add_argument("--revoke", action="store_true")
    p = project_parser("apply", "apply raw JSON commands")
    p.add_argument("--json", dest="json_commands", required=True, help="a command or list of commands")
    project_parser("undo", "undo the last transaction")
    project_parser("redo", "redo")
    project_parser("diff", "record a no-op transaction and print the diff (empty)")
    p = project_parser("restore", "restore a revision")
    p.add_argument("--revision", type=int, required=True)
    project_parser("pending", "list Sparky proposals awaiting review")
    p = project_parser("approve", "approve a proposal"); p.add_argument("transaction")
    p = project_parser("reject", "reject a proposal"); p.add_argument("transaction")
    p = project_parser("render", "render the timeline")
    p.add_argument("--quality", default="preview", choices=["preview", "medium", "high", "master"])
    p.add_argument("--format", default="mp4", choices=["mp4", "webm"])
    p.add_argument("--no-audio", action="store_true"); p.add_argument("--no-captions", action="store_true")
    p.add_argument("--no-import", action="store_true", help="do not add the render to the gallery")
    p.add_argument("--range", help="start:end seconds")
    p.add_argument("--explicit-approval", action="store_true", help="required for master")
    p.add_argument("--wait", action="store_true", help="poll until finished")
    p = sub.add_parser("status", help="render status"); p.add_argument("render")
    return parser


if __name__ == "__main__":
    sys.exit(main())
