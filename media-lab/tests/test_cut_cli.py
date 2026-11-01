"""The CLI maps arguments to Cut commands exactly; no server needed."""

import json

import pytest

from media_lab_core import cut_cli


def _commands(argv):
    args = cut_cli.build_parser().parse_args(argv)
    return cut_cli.build_commands(args)


def test_cli_maps_every_edit_verb_to_the_right_command():
    (trim,) = _commands(["trim", "p1", "clip-a", "--in", "12", "--out", "96"])
    assert trim["type"] == "clip.trim" and trim["payload"] == {"clip_id": "clip-a", "trim_in_frames": 12, "trim_out_frames": 96}
    assert trim["id"].startswith("cli-clip-trim-")
    (split,) = _commands(["split", "p1", "clip-a", "--at", "40"])
    assert split["type"] == "clip.split" and split["payload"] == {"clip_id": "clip-a", "at_frame": 40}
    (move,) = _commands(["move", "p1", "clip-a", "--start", "0"])
    assert move["payload"] == {"clip_id": "clip-a", "track_id": "track-video-main", "start_frame": 0}
    (add,) = _commands(["add", "p1", "--job", "abc", "--seconds", "2.5"])
    assert add["type"] == "clip.add" and add["payload"] == {"job_id": "abc", "duration_seconds": 2.5}
    (tr,) = _commands(["transition", "p1", "clip-a", "clip-b", "--kind", "fadeblack", "--frames", "6"])
    assert tr["payload"] == {"from_clip_id": "clip-a", "to_clip_id": "clip-b", "kind": "fadeblack", "duration_frames": 6}
    (trx,) = _commands(["transition", "p1", "clip-a", "clip-b", "--clear"])
    assert trx["type"] == "transition.remove"
    (cap,) = _commands(["caption", "p1", "add", "--text", "Hi", "--start", "0", "--end", "48"])
    assert cap["type"] == "caption.add" and cap["payload"] == {"text": "Hi", "start_frame": 0, "end_frame": 48}
    (cape,) = _commands(["caption", "p1", "edit", "caption-x", "--text", "Yo"])
    assert cape["payload"] == {"caption_id": "caption-x", "text": "Yo"}
    (capg,) = _commands(["caption", "p1", "generate", "--scene", "scene-1"])
    assert capg["type"] == "caption.generate" and capg["payload"] == {"scene_id": "scene-1"}
    (mix,) = _commands(["mix", "p1", "--target", "music", "--gain", "-8"])
    assert mix["payload"] == {"target": "music", "gain_db": -8.0}
    (mixn,) = _commands(["mix", "p1", "--normalize", "true", "--lufs", "-14"])
    assert mixn["payload"] == {"target": "dialogue", "normalize": True, "target_lufs": -14.0}
    (mixc,) = _commands(["mix", "p1", "--target", "clip", "--clip", "clip-a", "--mute", "yes"])
    assert mixc["payload"] == {"target": "clip", "clip_id": "clip-a", "muted": True}
    (col,) = _commands(["color", "p1", "clip-a", "--preset", "warm", "--intensity", "0.4"])
    assert col["payload"] == {"clip_id": "clip-a", "preset": "warm", "intensity": 0.4}
    (ap,) = _commands(["approve-master", "p1"])
    assert ap["type"] == "project.approve" and ap["payload"] == {"master_render_approved": True}
    assert _commands(["undo", "p1"])[0]["type"] == "undo"
    assert _commands(["redo", "p1"])[0]["type"] == "redo"
    assert _commands(["restore", "p1", "--revision", "2"])[0]["payload"] == {"revision": 2}


def test_cli_apply_accepts_raw_json_and_fails_closed():
    raw = json.dumps([{"type": "clip.trim", "payload": {"clip_id": "c", "trim_in_frames": 0, "trim_out_frames": 5}},
                      {"id": "keep-me", "type": "diff"}])
    cmds = _commands(["apply", "p1", "--json", raw])
    assert [c["type"] for c in cmds] == ["clip.trim", "diff"] and cmds[1]["id"] == "keep-me"
    with pytest.raises(SystemExit):
        _commands(["apply", "p1", "--json", "[]"])
    with pytest.raises(SystemExit):
        _commands(["apply", "p1", "--json", json.dumps([{"payload": {}}])])
