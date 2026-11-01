"""The code prompt's lockout (owner's rule, 2026-09-26).

3 wrong codes within 10 minutes from one client (an IPv4 address or an IPv6
/64) lock the code prompt for that client for 1 hour; every later lockout
doubles (2 h, 4 h, 8 h ... capped at 7 days); a clean day steps the level back
down. Already-signed-in devices keep working. The admin code has its own
counter. The owner lifts a lockout with `media-lab code --unlock`, and sets a
family code of their own choosing with `media-lab code --set-family`.

Covered on three levels: the pure rule (media_lab_core/door_lockout.py, with
a fake clock), the studio's door (app.py on a disposable data root, clients
told apart by CF-Connecting-IP on a public host), and the CLI.
"""
import contextlib
import importlib.util
import io
import json
import os
import stat
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from media_lab_core import cli, door_lockout, family_code, secret_files

REPO = Path(__file__).parents[1]
HOUR = 3600
T0 = 1_800_000_000.0             # a fixed "now" for the pure-rule cases


# ---------------------------------------------------------------- the pure rule

def _wrong(table, key, now, n=1):
    out = None
    for i in range(n):
        out = door_lockout.strike(table, key, now + i)
    return out


def test_three_wrong_codes_in_ten_minutes_lock_for_an_hour():
    table = {}
    first = door_lockout.strike(table, "ip:203.0.113.9", T0)
    second = door_lockout.strike(table, "ip:203.0.113.9", T0 + 60)
    assert (first["locked"], first["tries_left"]) == (False, 2)
    assert (second["locked"], second["tries_left"]) == (False, 1)
    assert second["next_lock"] == HOUR
    third = door_lockout.strike(table, "ip:203.0.113.9", T0 + 120)
    assert third["locked"] and third["retry_after"] == HOUR and third["level"] == 1
    assert door_lockout.remaining(table, "ip:203.0.113.9", T0 + 121) == HOUR - 1
    assert door_lockout.remaining(table, "ip:203.0.113.9", T0 + 120 + HOUR) == 0
    # another client is untouched
    assert door_lockout.remaining(table, "ip:203.0.113.10", T0 + 121) == 0


def test_strikes_older_than_ten_minutes_do_not_count():
    table = {}
    door_lockout.strike(table, "k", T0)
    door_lockout.strike(table, "k", T0 + 100)
    late = door_lockout.strike(table, "k", T0 + 600)        # T0 has aged out: 2 in the window
    assert not late["locked"] and late["tries_left"] == 1
    assert door_lockout.strike(table, "k", T0 + 601)["locked"]  # T0+100, +600, +601: 3 in 10 min


def test_each_later_lockout_doubles_up_to_a_week():
    table, now, got = {}, T0, []
    for _ in range(10):
        verdict = _wrong(table, "k", now, 3)
        got.append(verdict["retry_after"])
        now += verdict["retry_after"] + 10           # wait it out, then offend again at once
    assert got == [HOUR, 2 * HOUR, 4 * HOUR, 8 * HOUR, 16 * HOUR, 32 * HOUR, 64 * HOUR,
                   128 * HOUR, 7 * 24 * HOUR, 7 * 24 * HOUR]
    assert door_lockout.lock_seconds(0) == 0 and door_lockout.lock_seconds(99) == 7 * 24 * HOUR


def test_a_clean_day_steps_the_level_back_down_one_at_a_time():
    table = {}
    assert _wrong(table, "k", T0, 3)["retry_after"] == HOUR    # lockout 1 ends at T0+2+1h
    t = T0 + 2 + HOUR + 1
    assert _wrong(table, "k", t, 3)["retry_after"] == 2 * HOUR  # lockout 2
    end = t + 2 + 2 * HOUR
    # less than a clean day after the lockout ended: the next one is 4 h
    probe = {"k": json.loads(json.dumps(table["k"]))}
    assert _wrong(probe, "k", end + 23 * HOUR, 3)["retry_after"] == 4 * HOUR
    # one clean day: back to level 1, so the next one is 2 h (not 4 h)
    probe = {"k": json.loads(json.dumps(table["k"]))}
    assert _wrong(probe, "k", end + 24 * HOUR + 5, 3)["retry_after"] == 2 * HOUR
    # two clean days: forgotten entirely, 1 h again, and the table can drop it
    probe = {"k": json.loads(json.dumps(table["k"]))}
    door_lockout.prune(probe, end + 48 * HOUR + 5)
    assert probe == {}
    assert _wrong(probe, "k", end + 48 * HOUR + 5, 3)["retry_after"] == HOUR


def test_a_wrong_code_during_a_lockout_is_not_a_strike_and_changes_nothing():
    table = {}
    _wrong(table, "k", T0, 3)
    before = json.dumps(table)
    again = door_lockout.strike(table, "k", T0 + 100)
    assert again["locked"] and again["retry_after"] == HOUR - 98
    assert json.dumps(table) == before


def test_a_right_code_forgets_the_recent_wrong_ones_but_not_the_level():
    table = {}
    _wrong(table, "k", T0, 2)
    assert door_lockout.succeed(table, "k", T0 + 5) is True
    assert "k" not in table                                    # nothing left to remember
    assert not _wrong(table, "k", T0 + 10, 2)["locked"]         # two fresh tries again
    _wrong(table, "k", T0 + 20, 1)                              # ... the third locks (level 1)
    door_lockout.succeed(table, "k", T0 + 20 + HOUR + 1)        # e.g. the owner, later
    assert table["k"]["level"] == 1                             # a shared network gets no pardon


def test_prune_keeps_running_lockouts_when_the_table_overflows(monkeypatch):
    monkeypatch.setattr(door_lockout, "KEY_MAX", 5)
    table = {}
    _wrong(table, "locked", T0, 3)
    for i in range(20):
        door_lockout.strike(table, f"noise{i}", T0 + 10 + i)
    door_lockout.prune(table, T0 + 40)
    assert len(table) == 5 and "locked" in table


def test_malformed_saved_entries_never_crash_or_unlock():
    table = {"a": "junk", "b": {"strikes": "x", "level": "nan", "until": None},
             "c": {"level": 2, "until": T0 + 50, "strikes": [], "clean": T0}}
    assert door_lockout.remaining(table, "a", T0) == 0
    assert door_lockout.remaining(table, "c", T0) == 50
    assert door_lockout.strike(table, "b", T0)["tries_left"] == 2
    door_lockout.prune(table, T0)
    assert "c" in table


def test_keys_are_per_network_and_owner_targets_parse():
    tk = door_lockout.throttle_key
    assert tk("203.0.113.9") == "203.0.113.9"
    assert tk("2a01:4f8:c0c:1a2b::1") == tk("2a01:4f8:c0c:1a2b:dead:beef:1:2") == "2a01:4f8:c0c:1a2b::/64"
    assert tk("::ffff:203.0.113.9") == "203.0.113.9"
    assert tk("fd7a:115c:a1e0::1") == "fd7a:115c:a1e0::1"
    assert door_lockout.client_key("") == "anon"
    assert door_lockout.target_key(None) == "all"
    assert door_lockout.target_key("ALL") == "all" and door_lockout.target_key("anon") == "anon"
    assert door_lockout.target_key("203.0.113.9") == "ip:203.0.113.9"
    assert door_lockout.target_key("2a01:4f8:c0c:1a2b::77") == "ip:2a01:4f8:c0c:1a2b::/64"
    assert door_lockout.target_key("ip:2a01:4f8:c0c:1a2b::/64") == "ip:2a01:4f8:c0c:1a2b::/64"
    with pytest.raises(ValueError):
        door_lockout.target_key("drop table")


def test_unlock_all_or_one_client_in_both_counters():
    tables = {"gate": {}, "admin": {}}
    for ns in tables:
        _wrong(tables[ns], "ip:203.0.113.9", T0, 3)
        _wrong(tables[ns], "ip:198.51.100.4", T0, 3)
    assert door_lockout.unlock(tables, "ip:203.0.113.9") == 2
    assert set(tables["gate"]) == set(tables["admin"]) == {"ip:198.51.100.4"}
    assert door_lockout.unlock(tables, "all") == 2
    assert tables == {"gate": {}, "admin": {}}


def test_unlock_request_file_is_private_and_consumed_once(tmp_path):
    path = door_lockout.request_unlock(tmp_path, "ip:203.0.113.9")
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    door_lockout.request_unlock(tmp_path, "anon")               # a second, different one widens
    assert door_lockout.take_unlock_request(tmp_path) == "all"
    assert not path.exists() and door_lockout.take_unlock_request(tmp_path) is None
    path.write_text('{"target": "../../etc"}')
    assert door_lockout.take_unlock_request(tmp_path) is None and not path.exists()


def test_human_durations():
    h = door_lockout.human
    assert (h(59), h(3599), h(3600), h(2 * 3600 + 60 * 5), h(8 * 3600)) == (
        "1 min", "60 min", "1 h", "2 h 5 min", "8 h")
    assert h(7 * 24 * 3600) == "7 d" and h(86400 + 4 * 3600) == "1 d 4 h"


# ---------------------------------------------------------------- the studio's door

PUBLIC = "studio.example.com"


def _load_app(tmp_path_factory, name, seed=None):
    home = tmp_path_factory.mktemp(name)
    root = home / "media-lab-simple"
    root.mkdir()
    for item in ("static", "config"):
        (root / item).symlink_to(REPO / item)
    if seed:
        seed(root)
    old = {k: os.environ.get(k) for k in ("HOME", "MEDIA_LAB_DISABLE_BACKGROUND_WORKERS",
                                          "MEDIA_LAB_HOME", "MEDIA_LAB_PUBLIC_HOSTS")}
    os.environ["HOME"] = str(home)
    os.environ["MEDIA_LAB_DISABLE_BACKGROUND_WORKERS"] = "1"
    os.environ["MEDIA_LAB_PUBLIC_HOSTS"] = PUBLIC
    os.environ.pop("MEDIA_LAB_HOME", None)
    module_name = f"door_lockout_app_{name}"
    spec = importlib.util.spec_from_file_location(module_name, REPO / "app.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    out = io.StringIO()
    try:
        with contextlib.redirect_stdout(out):
            spec.loader.exec_module(module)
    finally:
        for key, value in old.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
    return module, out.getvalue()


@pytest.fixture(scope="module")
def door(tmp_path_factory):
    return _load_app(tmp_path_factory, "door")


@pytest.fixture(autouse=True)
def _clean_door(request):
    """Every case starts with an open door and the real clock."""
    yield
    if "door" in request.fixturenames:
        app, _ = request.getfixturevalue("door")
        with app._attempt_lock:
            for table in app._lockouts.values():
                table.clear()
            app._attempts["global"] = {}


class Clock:
    def __init__(self):
        self.now = time.time()

    def __call__(self):
        return self.now


def _visitor(app, ip):
    """A browser on the public hostname, as the Cloudflare edge presents it."""
    client = TestClient(app.app, base_url=f"https://{PUBLIC}")
    client.headers["cf-connecting-ip"] = ip
    return client


def _family(app):
    return app.ACCESS_CODE_FILE.read_text().strip()


def _admin(app):
    return app.PIN_FILE.read_text().strip()


def _guess(client, code="wrong-code"):
    return client.post("/api/gate", json={"code": code})


def test_the_door_locks_a_network_for_an_hour_after_three_wrong_codes(door):
    app, _ = door
    guesser = _visitor(app, "203.0.113.50")
    first, second, third = (_guess(guesser, f"nope-{i}") for i in range(3))
    assert (first.status_code, first.json()["tries_left"]) == (403, 2)
    assert (second.status_code, second.json()["tries_left"]) == (403, 1)
    assert second.json()["next_lock"] == HOUR
    assert third.status_code == 429
    body = third.json()
    assert body["locked"] and body["wrong"] and body["retry_after"] == HOUR
    assert third.headers["Retry-After"] == str(HOUR)
    assert "already signed in keep working" in body["message"]
    # the right family code is not even looked at now
    refused = _guess(guesser, _family(app))
    assert refused.status_code == 429 and 0 < refused.json()["retry_after"] <= HOUR
    assert "mlab_access" not in refused.cookies
    # a different network is not affected
    assert _guess(_visitor(app, "198.51.100.7"), _family(app)).status_code == 200


def test_the_whole_ipv6_64_shares_one_lockout(door):
    app, _ = door
    codes = [_guess(_visitor(app, f"2a01:4f8:c0c:5555::{i + 1:x}"), f"v6-{i}").status_code
             for i in range(4)]
    assert codes == [403, 403, 429, 429]
    assert _guess(_visitor(app, "2a01:4f8:c0c:5556::1"), _family(app)).status_code == 200


def test_devices_already_signed_in_keep_working_during_a_lockout(door):
    app, _ = door
    family_phone = _visitor(app, "203.0.113.60")
    assert _guess(family_phone, _family(app)).status_code == 200
    assert family_phone.get("/api/queue").status_code == 200
    kid = _visitor(app, "203.0.113.60")                        # same house, same address
    assert [_guess(kid, f"typo-{i}").status_code for i in range(3)] == [403, 403, 429]
    assert _guess(_visitor(app, "203.0.113.60"), _family(app)).status_code == 429
    assert family_phone.get("/api/queue").status_code == 200     # still signed in
    assert family_phone.get("/api/me").json()["role"] == "admin"   # (every session manages)


def test_the_admin_code_has_its_own_counter(door):
    app, _ = door
    house = "203.0.113.70"
    assert [_guess(_visitor(app, house), f"typo-{i}").status_code for i in range(3)] == [403, 403, 429]
    # the family door is shut for this network, but the owner's admin code still opens it
    owner = _visitor(app, house)
    ok = _guess(owner, _admin(app))
    assert ok.status_code == 200 and ok.json()["role"] == "admin"
    assert owner.get("/api/me").json()["owner"] is True
    # while shut, wrong codes count against the admin counter; three of them shut that too
    assert [_guess(_visitor(app, house), f"admin-guess-{i}").status_code for i in range(3)] == [429] * 3
    assert _guess(_visitor(app, house), _admin(app)).status_code == 429
    assert owner.get("/api/me").json()["owner"] is True           # the owner's session is untouched


def test_owner_scripts_x_lab_pin_counter_is_separate_from_the_prompt(door):
    app, _ = door
    script = _visitor(app, "203.0.113.80")
    wrong = [script.get("/api/queue", headers={"X-Lab-Pin": f"bad-{i}"}).status_code for i in range(3)]
    assert wrong == [401, 401, 429]
    locked = script.get("/api/queue", headers={"X-Lab-Pin": _admin(app)})
    assert locked.status_code == 429 and locked.json()["locked"] is True
    # the code prompt on the same network is still open (separate counter)
    assert _guess(_visitor(app, "203.0.113.80"), _family(app)).status_code == 200
    # and the other way round: a locked prompt does not stop an owner script
    for i in range(3):
        _guess(_visitor(app, "203.0.113.81"), f"typo-{i}")
    assert _visitor(app, "203.0.113.81").get(
        "/api/queue", headers={"X-Lab-Pin": _admin(app)}).status_code == 200


def test_lockouts_escalate_and_decay_on_the_studios_clock(door, monkeypatch):
    app, _ = door
    clock = Clock()
    monkeypatch.setattr(app, "_door_now", clock)
    net = "203.0.113.90"
    waits = []
    for _ in range(4):
        answers = [_guess(_visitor(app, net), f"x-{i}") for i in range(3)]
        waits.append(answers[-1].json()["retry_after"])
        clock.now += waits[-1] + 1
    assert waits == [HOUR, 2 * HOUR, 4 * HOUR, 8 * HOUR]
    # the lock is over: the right code works again
    assert _guess(_visitor(app, net), _family(app)).status_code == 200
    # a clean day later the next lockout is one step lower (8 h, not 16 h)
    clock.now += 24 * HOUR + 5
    assert [_guess(_visitor(app, net), f"y-{i}").json().get("retry_after") for i in range(3)][-1] == 8 * HOUR


def test_lockouts_survive_a_restart_and_the_owner_can_lift_them(tmp_path_factory, capsys):
    now = time.time()
    locked = {"lockouts": {"gate": {"ip:203.0.113.99": {"strikes": [], "level": 3,
                                                        "until": now + 4 * HOUR, "clean": now}},
                           "admin": {}},
              "devices": {"gate": {"old-browser-key": {"fails": 9, "last": now}}}}

    def seed(root):
        (root / "auth-attempts.json").write_text(json.dumps(locked))
    app, _ = _load_app(tmp_path_factory, "restarted", seed)
    assert "devices" not in app._attempts                       # the old per-browser backoff is gone
    refused = _guess(_visitor(app, "203.0.113.99"), _family(app))
    assert refused.status_code == 429 and refused.json()["retry_after"] > 3 * HOUR
    # `media-lab code --unlock 203.0.113.99` leaves a request; the studio's own
    # writer thread applies it within a few seconds, with nothing else poking it
    capsys.readouterr()
    req = door_lockout.request_unlock(app.ROOT, door_lockout.target_key("203.0.113.99"))
    deadline = time.monotonic() + 10
    while req.exists() and time.monotonic() < deadline:
        time.sleep(0.1)
    assert not req.exists()
    log = capsys.readouterr().out
    # both counters: the family code sent while the prompt was shut was an admin-counter strike
    assert "lifted 2 code-prompt lockout record(s) (one client)" in log and "203.0.113" not in log
    assert _guess(_visitor(app, "203.0.113.99"), _family(app)).status_code == 200


def test_a_lockout_is_logged_without_the_address_or_any_code(door, capsys):
    app, _ = door
    for i in range(3):
        _guess(_visitor(app, "203.0.113.111"), f"guess-{i}")
    log = capsys.readouterr().out
    assert "code prompt locked for one client" in log and "1 h" in log
    assert "203.0.113.111" not in log and _family(app) not in log


def test_the_gate_page_explains_the_lockout(door):
    app, _ = door
    page = TestClient(app.app, base_url=f"https://{PUBLIC}").get("/", headers={"accept": "text/html"})
    assert page.status_code == 401
    assert "Too many wrong codes from this network" in page.text
    assert "Devices that are already signed in keep working" in page.text
    assert "Studio owner? Enter the admin code" in page.text
    assert "d.tries_left===1" in page.text


# ---------------------------------------------------------------- the CLI

def _run_cli(monkeypatch, root, argv, stdin_text=None):
    """`media-lab <argv>` against the data root `root`; returns (rc, stdout, stderr)."""
    monkeypatch.setenv("MEDIA_LAB_HOME", str(root))
    out, err = io.StringIO(), io.StringIO()
    if stdin_text is not None:
        stream = io.StringIO(stdin_text)
        stream.isatty = lambda: False
        monkeypatch.setattr(sys, "stdin", stream)
    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
        rc = cli.main(argv)
    return rc, out.getvalue(), err.getvalue()


@pytest.fixture
def cli_root(tmp_path):
    root = tmp_path / "data"
    root.mkdir()
    secret_files.write_private(root / "access-code.txt", "maple-otter-lantern-comet\n")
    secret_files.write_private(root / "admin-pin.txt", family_code.mint_admin() + "\n")
    return root


def test_set_family_reads_stdin_writes_0600_and_never_prints_the_code(cli_root, monkeypatch):
    chosen = "4924"
    rc, out, err = _run_cli(monkeypatch, cli_root, ["code", "--set-family"], chosen + "\n")
    assert rc == 0
    path = cli_root / "access-code.txt"
    assert path.read_text() == chosen + "\n"
    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert chosen not in out and chosen not in err
    assert "short and guessable" in err and "lockout" in err
    assert "signed out" in err
    # the same code again changes nothing (nobody is signed out)
    rc, out, _err = _run_cli(monkeypatch, cli_root, ["code", "--set-family"], chosen)
    assert rc == 0 and "nothing changed" in out


def test_set_family_from_a_file_and_the_refusals(cli_root, tmp_path, monkeypatch):
    src = tmp_path / "code.txt"
    src.write_text("Harbour Kite 77\n")
    rc, out, err = _run_cli(monkeypatch, cli_root, ["code", "--set-family", "--from", str(src)])
    assert rc == 0 and "Harbour" not in out + err and "short" not in err
    assert (cli_root / "access-code.txt").read_text() == "Harbour Kite 77\n"
    admin = (cli_root / "admin-pin.txt").read_text().strip()
    for bad, why in (("", "empty"), (" - . ", "empty"), ("x" * 81, "longer"),
                     ("one\ntwo", "one line"), ("tab\there", "control"),
                     (admin.upper().replace("-", " "), "admin code")):
        rc, out, err = _run_cli(monkeypatch, cli_root, ["code", "--set-family"], bad)
        assert rc == 2 and why in err, (bad, err)
    assert (cli_root / "access-code.txt").read_text() == "Harbour Kite 77\n"   # untouched


def test_the_new_family_code_is_live_at_the_door_without_a_restart(door, tmp_path, monkeypatch):
    app, _ = door
    signed_in = _visitor(app, "198.51.100.20")
    assert _guess(signed_in, _family(app)).status_code == 200
    old = _family(app)
    rc, _out, _err = _run_cli(monkeypatch, app.ROOT, ["code", "--set-family"], "4924")
    assert rc == 0
    app._codes_seen["at"] = 0.0          # skip the one-second throttle
    assert signed_in.get("/api/queue").status_code == 401          # rotation signs devices out
    assert _guess(_visitor(app, "198.51.100.21"), "49-24").status_code == 200   # forgiving typing
    assert _guess(_visitor(app, "198.51.100.22"), old).status_code == 403
    secret_files.write_private(app.ACCESS_CODE_FILE, old + "\n")
    app._codes_seen["at"] = 0.0


def test_unlock_and_locks_from_the_cli(tmp_path, monkeypatch):
    root = tmp_path / "data"
    root.mkdir()
    now = time.time()
    state = {"lockouts": {"gate": {"ip:203.0.113.5": {"strikes": [], "level": 1, "until": now + HOUR, "clean": now},
                                   "anon": {"strikes": [now - 5], "level": 0, "until": 0, "clean": now}},
                          "admin": {"ip:203.0.113.5": {"strikes": [], "level": 2, "until": now + 60, "clean": now}}}}
    secret_files.write_private(root / "auth-attempts.json", json.dumps(state))
    rc, out, _err = _run_cli(monkeypatch, root, ["code", "--locks"])
    assert rc == 0 and "203.0.113.5" in out and "anon" in out
    assert "LOCKED for 1 min" in out and ("LOCKED for 1 h" in out or "LOCKED for 60 min" in out)
    # nobody consumes the request (no studio running): applied to the saved file directly
    monkeypatch.setattr(cli, "UNLOCK_WAIT_S", 0.0)
    rc, out, _err = _run_cli(monkeypatch, root, ["code", "--unlock", "203.0.113.5"])
    assert rc == 0 and "cleared 2 saved lockout record(s)" in out
    saved = json.loads((root / "auth-attempts.json").read_text())
    assert "ip:203.0.113.5" not in saved["lockouts"]["gate"] and "anon" in saved["lockouts"]["gate"]
    assert stat.S_IMODE((root / "auth-attempts.json").stat().st_mode) == 0o600
    assert (root / door_lockout.UNLOCK_REQUEST).exists()             # waits for the next start
    rc, _out, err = _run_cli(monkeypatch, root, ["code", "--unlock", "not-an-address"])
    assert rc == 2 and "not an address" in err
    rc, out, _err = _run_cli(monkeypatch, root, ["code", "--unlock"])
    assert rc == 0
    rc, out, _err = _run_cli(monkeypatch, root, ["code", "--locks"])
    assert "no lockouts" in out


def test_unlock_reaches_a_running_studio(door, monkeypatch):
    app, _ = door
    for i in range(3):
        _guess(_visitor(app, "203.0.113.120"), f"z-{i}")
    assert _guess(_visitor(app, "203.0.113.120"), _family(app)).status_code == 429
    # the studio's writer thread picks the request up within 3 s; don't wait for it
    monkeypatch.setattr(cli, "_pause", lambda _s: app._door_take_unlock())
    rc, out, _err = _run_cli(monkeypatch, app.ROOT, ["code", "--unlock", "203.0.113.120"])
    assert rc == 0 and "applied it" in out
    assert _guess(_visitor(app, "203.0.113.120"), _family(app)).status_code == 200


def test_client_identity_is_the_network_not_a_cookie(door):
    app, _ = door
    req = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"),
                          headers={"host": PUBLIC, "cf-connecting-ip": "2a01:4f8:c0c:1a2b::9"},
                          state=SimpleNamespace())
    assert app._door_key(req) == "ip:2a01:4f8:c0c:1a2b::/64"
    local = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), headers={"host": "localhost"},
                            state=SimpleNamespace())
    assert app._door_key(local) == "anon"
    # a fresh cookie jar does not buy a fresh set of tries
    for i in range(3):
        _guess(_visitor(app, "203.0.113.130"), f"c-{i}")
    fresh_jar = _visitor(app, "203.0.113.130")
    fresh_jar.cookies.set("mlab_device", "0" * 32 + "." + "0" * 12)
    assert _guess(fresh_jar, _family(app)).status_code == 429
