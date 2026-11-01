"""Gallery projects, the multi-project store and the whole-timeline render."""

import copy
import json
import subprocess

import pytest

from media_lab_core import cut


def _items(cut_media):
    rows = []
    for key, jid in (("a", "jobA"), ("c", "jobC"), ("b", "jobB"), ("m", "jobM")):
        item = cut.probe_gallery_file(cut_media[key])
        item.update({"job_id": jid, "title": f"Take {jid}", "prompt": f"prompt {jid}"})
        rows.append(item)
    return rows


def _cmd(kind, payload, cid):
    return {"id": cid, "type": kind, "payload": payload}


def test_probe_reports_real_durations_and_kinds(cut_media):
    a = cut.probe_gallery_file(cut_media["a"])
    assert a["kind"] == "video" and a["has_audio"] is True
    assert a["width"] == 640 and a["height"] == 360 and a["fps"] == 24.0
    assert 2.9 <= a["duration_seconds"] <= 3.1
    assert len(a["sha256"]) == 64
    c = cut.probe_gallery_file(cut_media["c"])
    assert c["kind"] == "image" and c["duration_seconds"] is None and c["has_audio"] is False
    m = cut.probe_gallery_file(cut_media["m"])
    assert m["kind"] == "music" and m["has_audio"] is True
    with pytest.raises(cut.CutError, match="missing"):
        cut.probe_gallery_file(cut_media["dir"] / "nope.mp4")


def test_gallery_project_puts_media_on_the_timeline_with_real_durations(cut_media):
    project = cut.build_gallery_project("cut-fixture", "Fixture", _items(cut_media))
    cut.validate_manifest(project)
    assert project["settings"]["fps"] == 24 and project["settings"]["width"] == 640
    v1 = project["timeline"]["tracks"][0]
    assert [(c["id"], c["start_frame"], c["duration_frames"]) for c in v1["clips"]] == [
        ("clip-jobA-main", 0, 72), ("clip-jobC-main", 72, 96), ("clip-jobB-main", 168, 48)]
    assert v1["clips"][1]["media_kind"] == "image" and v1["clips"][1]["source_duration_frames"] is None
    assert v1["clips"][0]["source_duration_frames"] == 72 and v1["clips"][0]["audio"]["linked"] is True
    music = project["timeline"]["tracks"][1]
    assert music["type"] == "music" and music["clips"][0]["id"] == "clip-jobM-music"
    assert project["duration_seconds"] == 9.0
    assert all(a["source"]["path"].startswith("/media/") for a in project["assets"])
    assert project["source"]["kind"] == "gallery_import" and project["source"]["job_ids"] == ["jobA", "jobC", "jobB", "jobM"]
    assert project["media_status"]["state"] == "ready"
    # same items → same manifest (deterministic import)
    assert project == cut.build_gallery_project("cut-fixture", "Fixture", _items(cut_media))


def test_gallery_project_is_honest_about_missing_media(cut_media):
    items = _items(cut_media)
    items[0]["exists"] = False
    project = cut.build_gallery_project("cut-missing", "Missing", items)
    assert project["media_status"]["state"] == "missing_media"
    assert project["media_status"]["missing_asset_count"] == 1
    assert project["candidates"][0]["media_state"] == "missing"


def test_multi_project_store_lists_creates_and_opens(tmp_path, cut_media):
    root = tmp_path / "home"
    assert cut.list_projects(root) == []
    items = _items(cut_media)
    resolver = lambda jid: next((i for i in items if i["job_id"] == jid), None)
    first = cut.create_project(root, cut.build_gallery_project("cut-one", "One", items[:1]), asset_resolver=resolver)
    second = cut.create_project(root, cut.build_gallery_project("cut-two", "Two", items[1:3]))
    assert (root / "cut/projects/cut-one/project.json").is_file()
    assert (root / "cut/projects/cut-one/journal.jsonl").is_file()
    rows = cut.list_projects(root)
    assert {r["project_id"] for r in rows} == {"cut-one", "cut-two"}
    two = next(r for r in rows if r["project_id"] == "cut-two")
    assert two["clip_count"] == 2 and two["project_source"] == "gallery" and two["poster"] == "/media/c.png"
    with pytest.raises(cut.CutError, match="already exists"):
        cut.create_project(root, cut.build_gallery_project("cut-one", "One", items[:1]))
    with pytest.raises(cut.CutError, match="unknown project"):
        cut.open_project(root, "cut-nine")
    with pytest.raises(cut.CutError):
        cut.project_dir(root, "../escape")
    # clip.add resolves a gallery job through the store's resolver and keeps originals immutable
    store = cut.open_project(root, "cut-one", asset_resolver=resolver)
    result = store.transact([_cmd("clip.add", {"job_id": "jobC"}, "add-c"),
                             _cmd("clip.add", {"job_id": "jobA"}, "add-a-again")],
                            actor="human", transaction_id="tx-add", expected_revision=0)
    assert result["status"] == "applied"
    project = store.load()
    ids = [c["id"] for c in project["timeline"]["tracks"][0]["clips"]]
    assert ids == ["clip-jobA-main", "clip-jobC-main", "clip-jobA-main-2"]
    assert [a["id"] for a in project["assets"]] == ["asset-jobA", "asset-jobC"]
    with pytest.raises(cut.CutError, match="unknown gallery item"):
        store.transact([_cmd("clip.add", {"job_id": "nope"}, "add-x")], actor="human",
                       transaction_id="tx-add-x", expected_revision=1)
    with pytest.raises(cut.CutError, match="resolver"):
        second.transact([_cmd("clip.add", {"job_id": "jobA"}, "add-y")], actor="human",
                        transaction_id="tx-add-y", expected_revision=0)
    # Sparky may not approve a master
    with pytest.raises(cut.CutError, match="only be applied by a person"):
        store.transact([_cmd("project.approve", {"master_render_approved": True}, "sp")], actor="sparky",
                       transaction_id="tx-sp", expected_revision=1, proposed=True)


def test_new_commands_edit_the_timeline_atomically(cut_media):
    project = cut.build_gallery_project("cut-cmd", "Commands", _items(cut_media))
    after, outputs = cut.apply_commands(project, [
        _cmd("clip.trim", {"clip_id": "clip-jobA-main", "trim_in_frames": 6, "trim_out_frames": 54}, "trim"),
        _cmd("clip.move", {"clip_id": "clip-jobC-main", "track_id": "track-video-main", "start_frame": 48}, "mv1"),
        _cmd("clip.move", {"clip_id": "clip-jobB-main", "track_id": "track-video-main", "start_frame": 144}, "mv2"),
        _cmd("transition.set", {"from_clip_id": "clip-jobA-main", "to_clip_id": "clip-jobC-main", "duration_frames": 12}, "tr"),
        _cmd("caption.add", {"text": "Hello", "start_frame": 10, "end_frame": 60}, "cap"),
        _cmd("diff", {}, "noop"),
        _cmd("color.apply", {"clip_id": "clip-jobB-main", "preset": "warm", "intensity": 0.5}, "col"),
        _cmd("audio.mix", {"target": "clip", "clip_id": "clip-jobA-main", "gain_db": -6, "muted": True}, "mixclip"),
        _cmd("clip.split", {"clip_id": "clip-jobB-main", "at_frame": 160}, "split"),
        _cmd("clip.remove", {"clip_id": "clip-jobM-music"}, "rm"),
    ], actor="human")
    v1 = after["timeline"]["tracks"][0]["clips"]
    assert [(c["id"], c["start_frame"], c["duration_frames"]) for c in v1] == [
        ("clip-jobA-main", 0, 48), ("clip-jobC-main", 48, 96), ("clip-jobB-main", 144, 16),
        ("clip-jobB-main-split-160", 160, 32)]
    assert after["timeline"]["transitions"][0]["kind"] == "dissolve"
    assert after["timeline"]["captions"]["items"][0]["text"] == "Hello"
    assert after["timeline"]["color"]["clip-jobB-main"] == {"preset": "warm", "intensity": 0.5}
    assert v1[0]["audio"] == {"linked": True, "gain_db": -6.0, "muted": True}
    assert after["timeline"]["tracks"][1]["clips"] == []
    assert after["duration_frames"] == 192 and len(outputs) == 10
    # guards
    with pytest.raises(cut.CutError, match="touching"):
        cut.apply_commands(project, [_cmd("transition.set", {"from_clip_id": "clip-jobA-main", "to_clip_id": "clip-jobB-main", "duration_frames": 6}, "t")], actor="human")
    with pytest.raises(cut.CutError, match="outside the source"):
        cut.apply_commands(project, [_cmd("clip.trim", {"clip_id": "clip-jobA-main", "trim_in_frames": 0, "trim_out_frames": 500}, "t")], actor="human")
    with pytest.raises(cut.CutError, match="unsupported color preset"):
        cut.apply_commands(project, [_cmd("color.apply", {"clip_id": "clip-jobA-main", "preset": "neon"}, "t")], actor="human")
    # a picture may be any length
    longer, _ = cut.apply_commands(project, [_cmd("clip.trim", {"clip_id": "clip-jobC-main", "trim_in_frames": 0, "trim_out_frames": 480}, "t")], actor="human")
    assert longer["timeline"]["tracks"][0]["clips"][1]["duration_frames"] == 480


def _edited(cut_media, tmp_path):
    items = _items(cut_media)
    project = cut.build_gallery_project("cut-render", "Render me", items)
    project, _ = cut.apply_commands(project, [
        _cmd("clip.trim", {"clip_id": "clip-jobA-main", "trim_in_frames": 6, "trim_out_frames": 54}, "trim"),
        _cmd("clip.move", {"clip_id": "clip-jobC-main", "track_id": "track-video-main", "start_frame": 48}, "mv1"),
        _cmd("clip.move", {"clip_id": "clip-jobB-main", "track_id": "track-video-main", "start_frame": 168}, "mv2"),
        _cmd("transition.set", {"from_clip_id": "clip-jobA-main", "to_clip_id": "clip-jobC-main", "duration_frames": 12}, "tr"),
        _cmd("caption.add", {"text": "Hello from Cut", "start_frame": 10, "end_frame": 60}, "cap"),
        _cmd("color.apply", {"clip_id": "clip-jobB-main", "preset": "warm", "intensity": 0.8}, "col"),
        _cmd("audio.mix", {"target": "music", "gain_db": -10}, "mix"),
    ], actor="human")
    return project


def test_timeline_plan_covers_trims_gaps_dissolves_stills_captions_color_and_music(cut_media, tmp_path):
    project = _edited(cut_media, tmp_path)
    plan = cut.plan_timeline_render(project, media_dir=cut_media["dir"], output=tmp_path / "o.mp4",
                                    export_request=cut.validate_export_request(project, {"quality": "preview"}),
                                    work_dir=tmp_path / "work")
    cmd = plan["command"]
    graph = cmd[cmd.index("-filter_complex") + 1]
    assert cmd[:6] == ["ffmpeg", "-nostdin", "-v", "error", "-y", "-fflags"]
    assert "-ss" in cmd and cmd[cmd.index("-ss") + 1] == "0.250000"       # trim in of clip A
    assert "-loop" in cmd                                                  # the still
    assert "xfade=transition=fade:duration=0.500000" in graph and "acrossfade" in graph
    assert "color=c=black" in graph                                        # the gap 144→168
    assert "eq=" in graph and "gamma_r" in graph                           # warm look
    assert "overlay=" in graph and plan["captions"] == "burned"            # caption card
    assert "amix=inputs=2" in graph and "adelay=0|0" in graph              # music bed
    assert "volume=-10.000dB" in graph
    assert "-map_metadata" in cmd and "-threads" in cmd and "creation_time=1970-01-01T00:00:00Z" in cmd
    assert plan["sources"] == ["/media/a.mp4", "/media/b.mp4", "/media/c.png", "/media/m.mp3"]
    assert plan["expected_seconds"] == pytest.approx(8.5)                  # 9 s timeline − 0.5 s dissolve
    assert cut.render_duration_frames(project) == 204
    assert (tmp_path / "work/captions.srt").read_text().startswith("1\n00:00:00,417 --> 00:00:02,000\nHello from Cut")
    with pytest.raises(cut.CutError, match="missing media"):
        cut.plan_timeline_render(project, media_dir=tmp_path, output=tmp_path / "x.mp4",
                                 export_request=cut.validate_export_request(project, {}), work_dir=tmp_path / "w2")


def test_timeline_render_twice_is_byte_identical_and_probes_clean(cut_media, tmp_path):
    project = _edited(cut_media, tmp_path)
    request = cut.validate_export_request(project, {"quality": "preview", "format": "mp4"})
    seen = []
    receipts = [cut.render_timeline(project, media_dir=cut_media["dir"], output=tmp_path / f"r{i}.mp4",
                                    export_request=request, work_dir=tmp_path / f"w{i}",
                                    progress=seen.append) for i in (1, 2)]
    assert receipts[0]["sha256"] == receipts[1]["sha256"]
    assert (tmp_path / "r1.mp4").read_bytes() == (tmp_path / "r2.mp4").read_bytes()
    assert receipts[0]["ffprobe"] == receipts[1]["ffprobe"]
    assert receipts[0]["exit_code"] == 0 and receipts[0]["captions"] == "burned"
    assert receipts[0]["schema"] == "media_lab.render_receipt.v1" and receipts[0]["candidate_not_final"] is True
    probe = receipts[0]["ffprobe"]
    assert {s["codec_type"] for s in probe["streams"]} == {"video", "audio"}
    video = next(s for s in probe["streams"] if s["codec_type"] == "video")
    assert (video["width"], video["height"]) == (640, 360)
    assert 8.4 <= float(probe["format"]["duration"]) <= 8.6
    assert seen and seen[-1] == 1.0
    assert not list(tmp_path.glob(".r1.*partial*"))
    # a range export and a webm without audio also plan
    webm = cut.plan_timeline_render(project, media_dir=cut_media["dir"], output=tmp_path / "o.webm",
                                    export_request=cut.validate_export_request(project, {"format": "webm", "include_audio": False,
                                                                                         "range_mode": "selection", "range_start_seconds": 1, "range_end_seconds": 4}),
                                    work_dir=tmp_path / "w3")
    assert "libvpx-vp9" in webm["command"] and "-an" in webm["command"] and webm["expected_seconds"] == 3.0


@pytest.mark.parametrize("burn_requested", [False, True])
@pytest.mark.parametrize("include_audio", [False, True])
def test_embedded_captions_render_with_or_without_pillow(
    cut_media, tmp_path, monkeypatch, burn_requested, include_audio
):
    # Exercise both an explicit soft-caption export and the fallback used when
    # Pillow cannot create caption cards. Verify the actual encoded artifact.
    monkeypatch.setattr(cut, "_caption_image", lambda *args: False)
    project = _edited(cut_media, tmp_path)
    output = tmp_path / "subtitled.mp4"
    receipt = cut.render_timeline(
        project,
        media_dir=cut_media["dir"],
        output=output,
        export_request=cut.validate_export_request(project, {
            "quality": "preview", "format": "mp4",
            "burn_captions": burn_requested, "include_audio": include_audio,
        }),
        work_dir=tmp_path / "work",
    )
    streams = receipt["ffprobe"]["streams"]
    assert receipt["captions"] == "embedded"
    assert {s["codec_type"] for s in streams} == (
        {"video", "audio", "subtitle"} if include_audio else {"video", "subtitle"}
    )
    assert next(s for s in streams if s["codec_type"] == "subtitle")["codec_name"] == "mov_text"
    subtitles = subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-i", str(output),
         "-map", "0:s:0", "-f", "srt", "pipe:1"],
        capture_output=True, text=True, timeout=30, check=True,
    ).stdout
    assert "Hello from Cut" in subtitles
