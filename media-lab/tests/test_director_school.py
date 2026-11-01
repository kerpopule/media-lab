"""Director school: the exam, transitions, routing and prompt composition."""
from media_lab_core import director_school as ds

BIBLE = {
    "style": "35mm film, soft grain, natural skin texture",
    "palette": "sodium orange, teal shadow, off-white tile",
    "location": "a 24-hour laundromat with rows of front-loading machines",
    "time_of_day": "2 a.m., rain on the windows",
    "lens": "35mm spherical lens, eye level, slow dolly moves",
    "characters": [
        {"name": "Maya", "look": "woman in her early 30s, dark curly hair in a bun, warm brown skin",
         "wardrobe": "teal scrubs under a grey cardigan", "reference": "/media/maya.png"},
        {"name": "Theo", "look": "lanky man in his late 20s, round glasses, short red hair",
         "wardrobe": "rain-soaked green parka"},
    ],
}


def board(beats, **extra):
    return {"bible": BIBLE, "seed": 7, "beats": beats, **extra}


def codes(exam):
    return [(f["code"], f["shot"]) for f in exam["findings"]]


def test_a_well_planned_board_passes():
    exam = ds.lint_board(board([
        {"shot_size": "WS", "characters": ["Maya", "Theo"], "screen_side": {"Maya": "left", "Theo": "right"},
         "video_prompt": "Theo crouches at a dryer while Maya folds towels at the counter.", "duration": 5},
        {"shot_size": "INSERT", "characters": [], "video_prompt": "Hands only: a single grey sock pulled from the drum.",
         "duration": 3},
        {"shot_size": "MCU", "characters": ["Maya"], "screen_side": {"Maya": "left"}, "speaker": "Maya",
         "references": ["/media/maya.png"],
         "video_prompt": 'Maya, not looking up: "It is always the left one."', "duration": 5},
    ]))
    assert exam["errors"] == 0, exam
    assert exam["grade"] in {"A", "B"}


def test_the_diner_mistakes_are_caught():
    exam = ds.lint_board({"beats": [
        {"characters": list("abcdef"), "shot_size": "MS", "video_prompt": "Six friends in a booth.", "duration": 5},
        {"characters": ["a", "b"], "shot_size": "MS", "transition": "dissolve", "duration": 5,
         "dialogue": [{"speaker": "a", "line": "This is amazing."}, {"speaker": "b", "line": "I know."}]},
        {"characters": ["c"], "shot_size": "WS", "duration": 5,
         "video_prompt": 'A neon WAFFLE HOUSE sign buzzes. She says: "Do not talk to me until I have had my coffee, seriously, not one word."'},
    ]})
    found = codes(exam)
    assert ("TOO_MANY_FACES", 1) in found
    assert ("TOO_MANY_SPEAKERS", 2) in found
    assert ("UNMOTIVATED_DISSOLVE", 2) in found
    assert ("DIALOGUE_TOO_LONG", 3) in found
    assert ("LEGIBLE_TEXT", 3) in found
    assert ("NO_SEED", None) in found
    assert exam["grade"] == "F"


def test_jump_cut_and_axis_break():
    exam = ds.lint_board(board([
        {"shot_size": "MS", "characters": ["Maya", "Theo"], "screen_side": {"Maya": "left", "Theo": "right"}},
        {"shot_size": "MCU", "characters": ["Maya", "Theo"], "screen_side": {"Maya": "right", "Theo": "left"}},
    ]))
    found = codes(exam)
    assert ("JUMP_CUT", 2) in found
    assert ("AXIS_BREAK", 2) in found


def test_identity_shot_without_anchor_is_flagged():
    exam = ds.lint_board(board([{"shot_size": "CU", "characters": ["Theo"], "video_prompt": "Theo frowns."}]))
    assert ("IDENTITY_UNANCHORED", 1) in codes(exam)


def test_shot_size_vocabulary():
    assert ds.shot_size("close-up") == "CU"
    assert ds.shot_size("Extreme wide establishing") == "EWS"
    assert ds.shot_size("over the shoulder on Maya") == "OTS"
    assert ds.shot_size("mcu") == "MCU"
    assert ds.shot_size("banana") is None


def test_transitions_are_motivated():
    a = {"speaker": "Theo", "video_prompt": 'Theo: "Where do they go?"', "shot_size": "MCU"}
    b = {"speaker": "Maya", "video_prompt": 'Maya: "Same place as mine."', "shot_size": "MCU"}
    reaction = {"video_prompt": "Theo stares at her, then laughs.", "shot_size": "CU"}
    assert ds.choose_transition(a, b)["kind"] == "j_cut"
    assert ds.choose_transition(b, reaction)["kind"] == "l_cut"
    assert ds.choose_transition(reaction, b, same_scene=False)["kind"] == "dissolve"
    assert ds.choose_transition(None, a)["kind"] == "cut"
    assert ds.choose_transition(a, {**b, "transition": "smash_cut"})["kind"] == "smash_cut"


def test_routing_and_estimates_include_spin_up():
    close = {"shot_size": "CU", "characters": ["Maya"], "duration": 5}
    wide = {"shot_size": "WS", "characters": [], "duration": 5}
    long_take = {"shot_size": "MS", "characters": ["Maya"], "duration": 12}
    assert ds.route_engine(wide, BIBLE)["variant"] == "sol-t2va"
    r = ds.route_engine(close, BIBLE)
    assert r["variant"] == "sol-fl2va" and r["estimate_s"] > ds.ENGINE_TIMINGS["sol-fl2va"]["warm_s"]
    rl = ds.route_engine(close, BIBLE, real_long_available=True)
    assert rl["variant"] == "real-long" and rl["references"] == ["/media/maya.png"]
    assert ds.route_engine(long_take, BIBLE)["notes"]
    cold = ds.estimate_seconds("real-long", 15, warm=False, references=1)
    assert cold > ds.ENGINE_TIMINGS["real-long"]["warm_ref_s"] + ds.ENGINE_TIMINGS["real-long"]["spin_up_s"]


def test_h3_prompt_carries_the_bible_and_one_schema():
    beat = {"shot_size": "MCU", "characters": ["Maya"], "screen_side": {"Maya": "left"}, "speaker": "Maya",
            "video_prompt": 'Maya folds a towel and says: "It is always the left one."'}
    prompt = ds.compose_h3_prompt(BIBLE, beat, start_frame=True)
    assert prompt.count("integrated_multimodal_description:") == 1
    assert prompt.count("overall_soundscape:") == 1 and prompt.count("non_diegetic_music:") == 1
    body = prompt.split("overall_soundscape:")[0]
    for must in ("Medium close-up", "teal scrubs", "2 a.m.", "sodium orange", "left of the frame",
                 "(S1) says <d>[English] It is always the left one.</d>", "never look into the camera",
                 "No on-screen text"):
        assert must in body, must
    assert '"' not in body


def test_still_prompt_builds_the_scene():
    text = ds.still_prompt(BIBLE, {"shot_size": "WS", "characters": ["Maya", "Theo"],
                                   "video_prompt": 'Theo says "hi" at the dryer.'})
    assert "Wide shot" in text and "green parka" in text and "no lettering" in text
    assert '"' not in text


def test_cut_plan_chooses_a_transition_for_every_cut(tmp_path):
    clips = []
    for i in range(3):
        p = tmp_path / f"{i}.mp4"
        p.write_bytes(b"x")
        clips.append(str(p))
    plan = ds.cut_plan(board([
        {"shot_size": "WS", "video_prompt": "Theo reaches into the dryer."},
        {"shot_size": "MCU", "speaker": "Theo", "video_prompt": 'Theo: "Where do they go?"'},
        {"shot_size": "MCU", "speaker": "Maya", "video_prompt": 'Maya: "Same place as mine."'},
    ]), clips, width=1344, height=768)
    kinds = [s["transition_in"]["kind"] for s in plan["shots"]]
    assert kinds == ["cut", "cut_on_action", "j_cut"]
    assert all(s["transition_in"]["why"] for s in plan["shots"])


def test_plan_summary_totals_the_estimate():
    summary = ds.plan_summary(board([{"shot_size": "WS", "duration": 5},
                                     {"shot_size": "CU", "characters": ["Maya"], "duration": 5}]))
    assert summary["estimate_s"] == sum(r["estimate_s"] for r in summary["routes"])
    assert summary["routes"][1]["variant"] == "sol-fl2va"


def test_the_planner_fixes_its_own_exam_errors():
    from media_lab_core import director_cli
    bad = {"title": "T", "bible": BIBLE, "beats": [
        {"shot_size": "MS", "characters": ["Maya", "Theo", "Ann"], "video_prompt": "Three friends talk."}]}
    good = {"title": "T", "bible": BIBLE, "beats": [
        {"shot_size": "WS", "characters": ["Maya", "Theo"], "video_prompt": "Maya and Theo at the dryers."}]}
    calls = []

    def chat(system, user, max_tokens=8000):
        calls.append(user)
        assert system == ds.BOARD_SYS
        return bad if len(calls) == 1 else good

    board = director_cli.plan_from_brief("two strangers in a laundromat", chat, log=lambda m: None)
    assert len(calls) == 2 and "TOO_MANY_FACES" in calls[1]
    assert board["director_exam"]["history"][0]["errors"] == 1
    assert board["director_exam"]["final"]["errors"] == 0


def test_cli_exam_prints_json(tmp_path, capsys):
    import json
    from media_lab_core import director_cli
    path = tmp_path / "board.json"
    path.write_text(json.dumps(board([{"shot_size": "WS", "video_prompt": "An empty laundromat."}])))
    assert director_cli.main(["exam", str(path)]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["grade"] and "findings" in out


def test_board_prompt_teaches_the_grammar():
    for must in ("shot_size", "screen_side", "180-degree", "j_cut", "ONE speaker per shot",
                 "Never dissolve inside one continuous moment", "No legible lettering"):
        assert must in ds.BOARD_SYS, must


def test_the_words_that_introduce_a_line_are_dropped_with_it():
    beat = {"shot_size": "MS", "characters": ["Maya"], "screen_side": {"Maya": "left"},
            "video_prompt": 'Maya folds towels. She says in a calm, weary voice: "It\'s always the left one." Static camera.',
            "dialogue": [{"speaker": "Maya", "line": "It\'s always the left one."}], "duration": 4}
    p = ds.compose_h3_prompt(BIBLE, beat)
    assert "weary voice:" not in p and "Maya folds towels." in p and "Static camera." in p
    assert "(S1) says <d>[English] It\'s always the left one.</d>" in p
