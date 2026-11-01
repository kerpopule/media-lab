"""``tools/director``: the director-school workflow from a terminal or an agent.

    director brief BRIEF.txt BOARD.json      plan a board from a brief; the planner fixes its own exam errors
    director exam BOARD.json                 grade a storyboard / shot list
    director plan BOARD.json [--real-long]   exam + engine per shot + estimate (spin-up included)
    director prompts BOARD.json              the H3 prompt and start-frame prompt for every shot
    director cut BOARD.json OUT.mp4 --clips a.mp4 b.mp4 ...   choose transitions and stitch
    director stitch PLAN.json OUT.mp4        stitch an explicit cut plan
    director critic CUT.mp4 RECEIPT.json --frames DIR [--board BOARD.json]
                    [--vision-url URL --vision-model NAME] [--verdicts FILE]
    director produce BOARD.json --out DIR [--studio URL] [--rounds 2] [--stills]

``produce`` drives a studio (on the studio machine it signs in with the local
token): start frames, H3 takes with one seed, the stitch, the critic, and up
to ``--rounds`` re-renders of the shots the critic rejects, with the critic's
reasons written into the next take. It never publishes anything.
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from . import director_school as ds
from . import seam_critic, stitch


def _load(path: str) -> dict[str, Any]:
    data = json.loads(Path(path).read_text(encoding="utf-8"))
    if isinstance(data, list):
        data = {"beats": data}
    return data


def _dump(data: Any) -> None:
    json.dump(data, sys.stdout, indent=2, ensure_ascii=False)
    sys.stdout.write("\n")


# ------------------------------------------------------------- studio client

class Studio:
    """Tiny client for the studio API (local token on the studio machine,
    or the family code from MEDIA_LAB_CODE elsewhere)."""

    def __init__(self, base: str, media_dir: str | None = None):
        from . import local_token
        self.base = base.rstrip("/")
        self.opener = local_token.studio_opener(self.base, os.environ.get("MEDIA_LAB_CODE") or None)
        self.media_dir = Path(media_dir) if media_dir else None

    def call(self, method: str, path: str, body: dict | None = None, timeout: int = 60) -> dict:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json"} if data else {})
        try:
            with self.opener.open(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode(errors="replace")[:300]
            raise RuntimeError(f"{method} {path} -> HTTP {exc.code}: {detail}") from None

    def submit(self, path: str, body: dict) -> str:
        out = self.call("POST", path, body)
        if not out.get("id"):
            raise RuntimeError(f"{path} refused: {json.dumps(out)[:300]}")
        return out["id"]

    def wait(self, job_id: str, *, poll: float = 15.0, limit: float = 5400.0, log=None) -> dict:
        start = time.time()
        last = None
        while True:
            j = self.call("GET", f"/api/jobs/{job_id}")
            state = (j.get("status"), j.get("stage"))
            if log and state != last:
                log(f"  job {job_id}: {j.get('status')} / {j.get('stage')}")
                last = state
            if j.get("status") in {"done", "error", "cancelled"}:
                return j
            if time.time() - start > limit:
                raise TimeoutError(f"job {job_id} still {j.get('status')} after {limit:.0f}s")
            time.sleep(poll)

    def run(self, path: str, body: dict, *, label: str = "", retries: int = 2, log=None) -> dict:
        """Submit, wait, and ride out a transient admission refusal.

        Studios before director-school refused a take queued right behind
        another one while memory settled ("requires 24.0 GiB ... only 22.7").
        Wait for memory to settle and resubmit, at most ``retries`` times."""
        for attempt in range(retries + 1):
            jid = self.submit(path, body)
            if log:
                log(f"{label or path}: job {jid}" + (f" (retry {attempt})" if attempt else ""))
            j = self.wait(jid, log=log)
            j["id"] = jid
            transient = j.get("status") == "error" and (
                "GiB" in str(j.get("detail") or "") or "stopped this job safely" in str(j.get("message") or ""))
            if j.get("status") == "done" or not transient or attempt == retries:
                return j
            settle_memory(log=log)
        return j

    def fetch(self, url: str, dest: Path) -> Path:
        name = Path(urllib.parse.urlsplit(url).path).name
        if self.media_dir and (self.media_dir / name).is_file():
            dest.write_bytes((self.media_dir / name).read_bytes())
            return dest
        with self.opener.open(self.base + url, timeout=600) as resp:
            dest.write_bytes(resp.read())
        return dest


# --------------------------------------------------------------------- brief

def text_chat(url: str, model: str, timeout: int = 900):
    """An OpenAI-compatible text chat returning the parsed JSON object."""
    import re

    def chat(system: str, user: str, max_tokens: int = 8000) -> dict:
        payload = {"model": model, "temperature": 0.6, "max_tokens": max_tokens,
                   "messages": [{"role": "system", "content": system + " /no_think"},
                                {"role": "user", "content": user + " /no_think"}]}
        req = urllib.request.Request(url, data=json.dumps(payload).encode(),
                                     headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            msg = json.loads(resp.read())["choices"][0]["message"]
        text = re.sub(r"<think>.*?</think>", "", msg.get("content") or msg.get("reasoning_content") or "",
                      flags=re.DOTALL)
        m = re.search(r"\{.*\}", text, re.DOTALL)
        return json.loads(m.group(0) if m else text)
    return chat


def plan_from_brief(brief: str, chat, *, revisions: int = 2, seed: int | None = None,
                    orientation: str = "landscape", log=print) -> dict[str, Any]:
    """Plan a board from a brief with the director's prompt, then make the
    planner sit the exam and correct its own errors (bounded)."""
    data = chat(ds.BOARD_SYS, f"Story idea: {brief}")
    board = {"title": str(data.get("title") or "Untitled"), "idea": brief, "orientation": orientation,
             "bible": data.get("bible") or {}, "beats": data.get("beats") or [], "seed": seed or 424242}
    exam = ds.lint_board(board, references_expected=False)
    history = [{"round": 0, "grade": exam["grade"], "errors": exam["errors"], "warnings": exam["warnings"]}]
    log(f"plan: {len(board['beats'])} shots, exam {exam['grade']} ({exam['errors']} errors)")
    for r in range(1, revisions + 1):
        problems = [f for f in exam["findings"] if f["severity"] == "error"
                    or f["code"] in {"JUMP_CUT", "UNMOTIVATED_DISSOLVE", "LEGIBLE_TEXT", "NO_SHOT_SIZE",
                                     "WARDROBE_UNDEFINED", "BIBLE_GAP", "NO_ESTABLISHING"}]
        if not problems:
            break
        notes = "\n".join(f"- shot {f['shot'] or '-'}: {f['code']}: {f['message']} Fix: {f['fix']}" for f in problems)
        data = chat(ds.BOARD_SYS, f"Story idea: {brief}\n\nYour previous plan was:\n"
                    f"{json.dumps({'title': board['title'], 'bible': board['bible'], 'beats': board['beats']})}"
                    f"\n\nIt failed the director's exam:\n{notes}\n\nReturn the corrected complete JSON object.")
        board.update({"title": str(data.get("title") or board["title"]), "bible": data.get("bible") or board["bible"],
                      "beats": data.get("beats") or board["beats"]})
        exam = ds.lint_board(board, references_expected=False)
        history.append({"round": r, "grade": exam["grade"], "errors": exam["errors"], "warnings": exam["warnings"]})
        log(f"revision {r}: exam {exam['grade']} ({exam['errors']} errors)")
    board["director_exam"] = {"final": exam, "history": history}
    return board


def settle_memory(*, need_gib: float = 24.5, limit_s: float = 90.0, log=None) -> None:
    """On the studio machine, wait until MemAvailable is back above ``need_gib``;
    elsewhere just give the studio a minute."""
    meminfo = Path("/proc/meminfo")
    if not meminfo.is_file():
        time.sleep(60)
        return
    start = time.time()
    while time.time() - start < limit_s:
        fields = dict(line.split(":", 1) for line in meminfo.read_text().splitlines() if ":" in line)
        available = int(fields.get("MemAvailable", "0 kB").split()[0]) / 1048576
        if available >= need_gib:
            break
        time.sleep(3)
    time.sleep(5)
    if log:
        log(f"  memory settled after {time.time() - start:.0f}s")


# ------------------------------------------------------------------- produce

def produce(board: dict, out_dir: Path, studio: Studio, *, rounds: int = 2, stills: bool = True,
            seed: int | None = None, vision=None, studio_wraps_h3: bool = False,
            takes_per_load: int | None = None, real_long: bool = False,
            log=print) -> dict[str, Any]:
    """Film a planned board on H3 with continuity, stitch it, critique it,
    re-render rejected shots (bounded), and leave every receipt in ``out_dir``.

    ``real_long``: the studio has the Real / Long engine, so identity-critical
    shots (a character with a reference picture) film there, from their start
    frame plus every visible character's reference picture, and all stills are
    made before the first take so the studio loads the video engine once."""
    out_dir.mkdir(parents=True, exist_ok=True)
    bible = board.get("bible") or {}
    beats = [ds.normalize_beat(b, i) for i, b in enumerate(board.get("beats") or [])]
    seed = int(seed or board.get("seed") or 424242)
    exam = ds.lint_board(board, references_expected=stills)
    (out_dir / "exam.json").write_text(json.dumps(exam, indent=2))
    log(f"exam: grade {exam['grade']} ({exam['errors']} errors, {exam['warnings']} warnings)")
    if exam["errors"]:
        for f in ds.iter_findings(exam, "error"):
            log(f"  error shot {f['shot']}: {f['code']} {f['message']}")
        raise SystemExit("the board fails the exam; fix the errors before spending GPU time")
    journal: dict[str, Any] = {"seed": seed, "shots": {}, "rounds": [], "stills": []}
    orientation = board.get("orientation") or "landscape"
    # 1) start frames: an establishing master, then every other still is an
    #    edit of the master (same set and light) anchored on the character's
    #    reference picture (same face)
    still_urls: dict[int, str] = {}
    characters = ds.normalize_bible(bible)["characters"]

    def image(body: dict, label: str) -> str:
        j = studio.run("/api/image", body, label=label, log=log)
        jid = j["id"]
        if j.get("status") != "done" or not j.get("url"):
            raise RuntimeError(f"{label} failed: {j.get('message')} {j.get('detail') or ''}")
        journal.setdefault("stills", []).append({"label": label, "job": jid, "url": j["url"],
                                                 "prompt": body["prompt"], "source": body.get("source"),
                                                 "reference_source": body.get("reference_source")})
        return j["url"]

    master_idx = next((b["index"] for b in beats if b["shot_size"] in ds.WIDE_SIZES), 0)

    def make_master() -> None:
        # the master: the establishing wide defines the room, the light and the
        # layout; each character's reference picture then fixes that person's
        # face in it; every other still is an edit of the master
        mb = beats[master_idx]
        master = image({"prompt": ds.still_prompt(bible, mb), "orientation": orientation, "seed": seed,
                        "engine": "auto"}, f"master still (shot {master_idx + 1})")
        for c in characters:
            if c["reference"] and c["name"] in mb["characters"]:
                master = image({"prompt": (f"Keep this picture exactly as it is: the room, the light, the framing, "
                                           f"the poses and every piece of clothing. Only make {c['name']} "
                                           f"({c['look'][:160]}) look exactly like the person in the second picture."),
                                "source": master, "reference_source": c["reference"], "orientation": orientation,
                                "seed": seed, "engine": "auto"}, f"master identity pass: {c['name']}")
        still_urls[master_idx] = master
        studio.fetch(master, out_dir / f"still-{master_idx + 1:02d}{Path(master).suffix}")

    def make_still(i: int, still_seed: int, note: str = "") -> None:
        if i == master_idx and i not in still_urls:
            make_master()
            return
        if master_idx not in still_urls:
            make_master()
        if i == master_idx:
            return
        b = beats[i]
        refs = [c["reference"] for c in characters if c["name"] in b["characters"] and c["reference"]]
        body = {"prompt": ("Same place, same light, same people and the same clothes as this picture. "
                           "New camera set-up: " + ds.still_prompt(bible, b) + (" " + note if note else "")),
                "source": still_urls[master_idx], "orientation": orientation, "seed": still_seed, "engine": "auto"}
        if len(refs) == 1:
            body["reference_source"] = refs[0]
        still_urls[i] = image(body, f"still for shot {i + 1}")
        studio.fetch(still_urls[i], out_dir / f"still-{i + 1:02d}-s{still_seed}{Path(still_urls[i]).suffix}")

    ref_b64: dict[str, str] = {}

    def reference_b64(url: str) -> str:
        if url not in ref_b64:
            tmp = out_dir / f".ref-{Path(urllib.parse.urlsplit(url).path).name}"
            studio.fetch(url, tmp)
            ref_b64[url] = base64.b64encode(tmp.read_bytes()).decode()
            tmp.unlink(missing_ok=True)
        return ref_b64[url]

    routes = {b["index"]: ds.route_engine(b, bible, real_long_available=real_long) for b in beats}
    journal["routes"] = {str(i + 1): {k: r[k] for k in ("variant", "why", "estimate_s")}
                         for i, r in routes.items()}

    # 2) takes
    patches: dict[int, str] = {}
    reasons_for: dict[int, list] = {}
    take_seed: dict[int, int] = {b["index"]: seed for b in beats}
    pending = [b["index"] for b in beats]
    clips: dict[int, Path] = {}
    report = None
    for round_no in range(rounds + 1):
        # Batches: each batch's stills are image jobs, which also make the
        # studio reload H3 before the batch's takes. takes_per_load bounds how
        # many different takes one H3 engine process films (Sol-H3 today hard-
        # fails on its third distinct prompt: docs/DIRECTOR-SCHOOL.md).
        batch = len(pending) if real_long else (takes_per_load or len(pending))
        order: list[tuple[str, int]] = []
        for k in range(0, len(pending), batch):
            group = pending[k:k + batch]
            if stills:
                order.extend(("still", i) for i in group)
            # Real / Long takes first, then any Sol takes: one engine switch at most
            order.extend(("take", i) for i in sorted(group, key=lambda n: routes[n]["variant"] != "real-long"))
        for step, i in order:
            if step == "still":
                # on Real / Long a re-take keeps its start frame (the references
                # hold the faces), so no image job evicts the loaded engine
                if real_long and round_no and i in still_urls:
                    continue
                if round_no or i not in still_urls:
                    make_still(i, take_seed[i], seam_critic.rerender_patch(reasons_for.get(i, [])) if round_no else "")
                continue
            b = beats[i]
            on_real_long = routes[i]["variant"] == "real-long"
            prompt = ds.compose_h3_prompt(bible, b, start_frame=i in still_urls and not on_real_long)
            if patches.get(i):
                prompt = prompt.replace("\n\noverall_soundscape:", " " + patches[i] + "\n\noverall_soundscape:", 1)
            if studio_wraps_h3:
                # a studio older than the director-school deploy wraps every H3
                # prompt itself: send only the description or it nests the schema
                prompt = prompt.split("\n\noverall_soundscape:")[0].replace(
                    "integrated_multimodal_description: [Shot 1] ", "", 1)
            body = {"prompt": prompt, "model": "h3", "duration": "5", "orientation": orientation,
                    "seed": take_seed[i], "style": "none"}
            if i in still_urls:
                body["source"] = still_urls[i]
            if on_real_long:
                # the start frame becomes the opening-frame picture; each visible
                # character's reference picture holds that face for the whole take
                by_name = {c["name"]: c for c in characters}
                body["h3_engine"] = "singularity"
                body["reference_detail"] = "match"
                body["references"] = [{"b64": reference_b64(by_name[n]["reference"]), "role": f"person called {n}"}
                                      for n in b["characters"] if n in by_name and by_name[n]["reference"]]
                if b["seconds"] > 5.5:
                    body["duration"] = str(int(min(15, round(b["seconds"]))))
            j = studio.run("/api/generate", body, label=f"round {round_no}: shot {i + 1} take", log=log)
            jid = j["id"]
            if j.get("status") != "done" or not j.get("url"):
                raise RuntimeError(f"shot {i + 1} failed: {j.get('message')} {j.get('detail') or ''}")
            dest = out_dir / f"shot-{i + 1:02d}-r{round_no}.mp4"
            studio.fetch(j["url"], dest)
            clips[i] = dest
            journal["shots"].setdefault(str(i + 1), []).append(
                {"round": round_no, "job": jid, "seed": take_seed[i], "prompt": prompt,
                 "engine": routes[i]["variant"],
                 "start_frame": still_urls.get(i), "file": dest.name})
        plan = ds.cut_plan(board, [str(clips[b["index"]]) for b in beats], width=1344, height=768,
                           quality="high")
        cut_path = out_dir / f"cut-r{round_no}.mp4"
        receipt = stitch.render(plan, cut_path)
        (out_dir / f"cut-r{round_no}.receipt.json").write_text(json.dumps(receipt, indent=2))
        report = seam_critic.review(cut_path, receipt, frames_dir=out_dir / f"seams-r{round_no}",
                                    bible=bible, chat=vision, syncnet=seam_critic.syncnet_runner())
        (out_dir / f"critic-r{round_no}.json").write_text(json.dumps(report, indent=2))
        (out_dir / f"critic-r{round_no}.md").write_text(seam_critic.markdown(report, f"Critic, round {round_no}"))
        journal["rounds"].append({"round": round_no, "cut": cut_path.name, "summary": report["summary"],
                                  "rerender": report["rerender"]})
        log(f"critic round {round_no}: {report['summary']}")
        if not report["rerender"] or round_no == rounds:
            break
        pending = [r["shot"] - 1 for r in report["rerender"]]
        for r in report["rerender"]:
            i = r["shot"] - 1
            reasons_for[i] = r["reasons"]
            patches[i] = seam_critic.rerender_patch(r["reasons"])
            take_seed[i] = take_seed[i] + 1000 * (round_no + 1)
    journal["final_cut"] = journal["rounds"][-1]["cut"]
    journal["open_rerenders"] = report["rerender"] if report else []
    (out_dir / "journal.json").write_text(json.dumps(journal, indent=2))
    return journal


# ---------------------------------------------------------------------- main

def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="director", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("brief"); p.add_argument("brief_file"); p.add_argument("out")
    p.add_argument("--text-url", default=os.environ.get("MEDIA_LAB_CHAT_URL", "http://127.0.0.1:8003/v1/chat/completions"))
    p.add_argument("--text-model", default=os.environ.get("QWEN_MODEL", "qwen3.8-27b-q4km"))
    p.add_argument("--revisions", type=int, default=2); p.add_argument("--seed", type=int)
    p = sub.add_parser("exam"); p.add_argument("board")
    p = sub.add_parser("plan"); p.add_argument("board"); p.add_argument("--real-long", action="store_true")
    p = sub.add_parser("prompts"); p.add_argument("board")
    p = sub.add_parser("cut"); p.add_argument("board"); p.add_argument("out")
    p.add_argument("--clips", nargs="+", required=True); p.add_argument("--quality", default="high")
    p.add_argument("--music"); p.add_argument("--width", type=int, default=1344); p.add_argument("--height", type=int, default=768)
    p = sub.add_parser("stitch"); p.add_argument("plan"); p.add_argument("out")
    p = sub.add_parser("critic"); p.add_argument("cut"); p.add_argument("receipt")
    p.add_argument("--frames", required=True); p.add_argument("--board")
    p.add_argument("--vision-url", default=os.environ.get("MEDIA_LAB_CRITIC_VISION_URL"))
    p.add_argument("--vision-model", default=os.environ.get("MEDIA_LAB_CRITIC_VISION_MODEL"))
    p.add_argument("--verdicts", help="JSON list of vision answers written by an agent that looked at the pair images")
    p = sub.add_parser("produce"); p.add_argument("board"); p.add_argument("--out", required=True)
    p.add_argument("--studio", default=os.environ.get("MEDIA_LAB_URL", "http://127.0.0.1:7863"))
    p.add_argument("--media-dir"); p.add_argument("--rounds", type=int, default=2)
    p.add_argument("--no-stills", action="store_true"); p.add_argument("--seed", type=int)
    p.add_argument("--takes-per-load", type=int,
                   help="film at most N different takes per H3 engine load (2 on today's Sol-H3)")
    p.add_argument("--real-long", action="store_true",
                   help="the studio has Real / Long: film identity-critical shots there, one engine load")
    p.add_argument("--studio-wraps-h3", action="store_true",
                   help="the studio predates director school and wraps H3 prompts itself")
    p.add_argument("--vision-url", default=os.environ.get("MEDIA_LAB_CRITIC_VISION_URL"))
    p.add_argument("--vision-model", default=os.environ.get("MEDIA_LAB_CRITIC_VISION_MODEL"))
    args = ap.parse_args(argv)

    if args.cmd == "brief":
        board = plan_from_brief(Path(args.brief_file).read_text(encoding="utf-8"),
                                text_chat(args.text_url, args.text_model), revisions=args.revisions, seed=args.seed,
                                log=lambda m: print(m, file=sys.stderr))
        Path(args.out).write_text(json.dumps(board, indent=2, ensure_ascii=False))
        _dump({"out": args.out, "exam": board["director_exam"]["history"]})
    elif args.cmd == "exam":
        _dump(ds.lint_board(_load(args.board)))
    elif args.cmd == "plan":
        _dump(ds.plan_summary(_load(args.board), real_long_available=args.real_long))
    elif args.cmd == "prompts":
        board = _load(args.board)
        rows = []
        for i, b in enumerate(board.get("beats") or []):
            nb = ds.normalize_beat(b, i)
            rows.append({"shot": i + 1, "shot_size": nb["shot_size"],
                         "h3_prompt": ds.compose_h3_prompt(board.get("bible"), nb, start_frame=True),
                         "still_prompt": ds.still_prompt(board.get("bible"), nb)})
        _dump(rows)
    elif args.cmd == "cut":
        plan = ds.cut_plan(_load(args.board), args.clips, width=args.width, height=args.height,
                           quality=args.quality, music=args.music)
        receipt = stitch.render(plan, args.out)
        Path(args.out).with_suffix(".receipt.json").write_text(json.dumps(receipt, indent=2))
        _dump({"output": args.out, "sha256": receipt["sha256"], "seconds": receipt["probe"]["duration"],
               "notes": receipt["plan"]["notes"]})
    elif args.cmd == "stitch":
        receipt = stitch.render(_load(args.plan), args.out)
        Path(args.out).with_suffix(".receipt.json").write_text(json.dumps(receipt, indent=2))
        _dump({"output": args.out, "sha256": receipt["sha256"], "seconds": receipt["probe"]["duration"],
               "notes": receipt["plan"]["notes"]})
    elif args.cmd == "critic":
        receipt = json.loads(Path(args.receipt).read_text())
        bible = _load(args.board).get("bible") if args.board else None
        chat = None
        if args.vision_url and args.vision_model:
            chat = seam_critic.default_vision_chat(args.vision_url, args.vision_model)
        if args.verdicts:
            measured = seam_critic.measure(args.cut, receipt, args.frames)
            vision = json.loads(Path(args.verdicts).read_text())
            report = seam_critic.verdicts(measured, vision, receipt.get("plan") or receipt)
            report["schema"] = seam_critic.REPORT_SCHEMA
        else:
            report = seam_critic.review(args.cut, receipt, frames_dir=args.frames, bible=bible, chat=chat,
                                        syncnet=seam_critic.syncnet_runner())
        Path(args.frames, "critic.json").write_text(json.dumps(report, indent=2))
        Path(args.frames, "critic.md").write_text(seam_critic.markdown(report))
        _dump({"summary": report["summary"], "rerender": report["rerender"],
               "seams": [{k: s[k] for k in ("seam", "verdict", "reasons")} for s in report["seams"]]})
    elif args.cmd == "produce":
        chat = None
        if args.vision_url and args.vision_model:
            chat = seam_critic.default_vision_chat(args.vision_url, args.vision_model)
        studio = Studio(args.studio, args.media_dir)
        journal = produce(_load(args.board), Path(args.out), studio, rounds=args.rounds,
                          stills=not args.no_stills, seed=args.seed, vision=chat,
                          studio_wraps_h3=args.studio_wraps_h3, takes_per_load=args.takes_per_load,
                          real_long=args.real_long)
        _dump({"final_cut": journal["final_cut"], "open_rerenders": journal["open_rerenders"]})
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
