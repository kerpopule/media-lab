import json
from pathlib import Path

import pytest

from media_lab_core import cli, family_code, pairing


@pytest.fixture
def root(tmp_path, monkeypatch):
    monkeypatch.setenv("MEDIA_LAB_HOME", str(tmp_path))
    return tmp_path


def test_app_root_env_override(root):
    assert cli.app_root() == root


def test_config_round_trip(root):
    assert cli.load_config(root) == {}
    rc = cli.main(["config", "--port", "7891", "--bind", "127.0.0.1", "--service", "no",
                   "--venv", str(root / "venv")])
    assert rc == 0
    cfg = cli.load_config(root)
    assert cfg["port"] == 7891 and cfg["bind"] == "127.0.0.1" and cfg["service"] is False
    assert cfg["repo"] == str(cli.REPO) and cfg["data_root"] == str(root)
    assert cli.venv_python(cfg) == root / "venv" / "bin" / "python"


def test_codes_mint_and_read(root):
    access, admin = cli.code_paths(root)
    a, b = cli.mint_access_code(), cli.mint_admin_code()
    assert len(a.split("-")) == 4 and all(w in family_code.WORDS for w in a.split("-"))
    assert len(b.split("-")) == 6 and not b.isdigit()           # never a 4-digit PIN
    cli.write_code(access, "maple-otter-lantern-comet")
    assert cli.read_code(access) == "maple-otter-lantern-comet"
    assert (access.stat().st_mode & 0o777) == 0o600
    assert cli.read_code(admin) is None


def test_code_command(root, capsys):
    assert cli.main(["code"]) == 1                   # nothing minted yet
    assert cli.main(["code", "--rotate"]) == 0
    new = capsys.readouterr().out.strip()
    assert len(new.split("-")) == 4
    assert cli.main(["code"]) == 0
    assert capsys.readouterr().out.strip() == new
    assert cli.main(["code", "--rotate", "--admin"]) == 0
    assert len(capsys.readouterr().out.strip().split("-")) == 6
    for name in ("access-code.txt", "admin-pin.txt"):
        assert ((root / name).stat().st_mode & 0o777) == 0o600


def test_code_quiet_never_prints_a_code(root, capsys):
    assert cli.main(["code", "--rotate", "--quiet"]) == 0
    out = capsys.readouterr().out
    code = (root / "access-code.txt").read_text().strip()
    assert code not in out and "access-code.txt" in out
    assert cli.main(["code", "--quiet"]) == 0
    assert code not in capsys.readouterr().out


def test_code_ensure_creates_missing_codes_and_keeps_existing(root, capsys):
    cli.write_code(root / "access-code.txt", "ABCD2345")        # a legacy family code
    (root / "access-code.txt").chmod(0o664)
    assert cli.main(["code", "--ensure"]) == 0
    assert (root / "access-code.txt").read_text().strip() == "ABCD2345"
    admin = (root / "admin-pin.txt").read_text().strip()
    assert len(admin.split("-")) == 6
    for name in ("access-code.txt", "admin-pin.txt"):
        assert ((root / name).stat().st_mode & 0o777) == 0o600
    assert admin not in capsys.readouterr().out
    assert cli.main(["code"]) == 0
    assert "short and guessable" in capsys.readouterr().err


def test_render_template_fills_everything_and_refuses_leftovers():
    out = cli.render_template("a=@@A@@ b=@@B@@", {"A": 1, "B": "x"})
    assert out == "a=1 b=x"
    with pytest.raises(ValueError):
        cli.render_template("a=@@A@@ @@MISSING@@", {"A": 1})


@pytest.mark.parametrize("rel", ["systemd/media-lab.service", "launchd/com.medialab.server.plist"])
def test_shipped_templates_render(root, rel):
    cfg = {"port": 7863, "bind": "0.0.0.0", "venv": str(root / ".venv")}
    text = cli.render_template((cli.REPO / rel).read_text(), cli.template_values(cfg, root))
    assert "@@" not in text
    assert cli.MANAGED_MARKER in text
    assert "--port 7863" in text or "<string>7863</string>" in text
    assert str(root / ".venv" / "bin" / "python") in text
    assert f"MEDIA_LAB_HOME={root}" in text or f"<string>{root}</string>" in text


def test_probe_host():
    assert cli.probe_host("0.0.0.0") == "127.0.0.1"
    assert cli.probe_host("") == "127.0.0.1"
    assert cli.probe_host("10.1.2.3") == "10.1.2.3"


def test_status_json_offline(root, monkeypatch, capsys):
    """No server, no service manager: the JSON still carries every key an agent
    reads, and the exit code says DOWN."""
    cli.save_config({"port": 7891, "bind": "0.0.0.0"}, root)
    monkeypatch.setattr(cli, "manifest_up", lambda *a, **k: False)
    monkeypatch.setattr(cli, "service_state", lambda: {"kind": "none", "name": "", "state": "absent", "managed": False})
    monkeypatch.setattr(cli, "gpu_info", lambda: {"present": False, "names": [], "note": "n"})
    assert cli.main(["status", "--json"]) == 1
    st = json.loads(capsys.readouterr().out)
    assert st["ok"] is False and st["port"] == 7891
    assert st["url"] == "http://127.0.0.1:7891"
    assert st["engines"] is None and st["service"]["mode"] == "none"
    assert st["codes"]["access_code_file"] == str(root / "access-code.txt")
    assert st["data_root"] == str(root)


def test_status_reads_engines_when_up(root, monkeypatch, capsys):
    cli.write_code(root / "access-code.txt", "ABCD2345")
    monkeypatch.setattr(cli, "manifest_up", lambda *a, **k: True)
    monkeypatch.setattr(cli, "service_state", lambda: {"kind": "systemd", "name": "media-lab.service", "state": "running", "managed": True})
    monkeypatch.setattr(cli, "gpu_info", lambda: {"present": True, "names": ["GB10"], "note": "n"})
    seen = {}

    def fake_setup_status(port, bind, code, timeout=8):
        seen.update(port=port, code=code)
        return {"engines": {"ltx": {"state": "ready", "detail": "running"}}, "gpu": True,
                "fal_configured": False, "first_run": False}

    monkeypatch.setattr(cli, "setup_status", fake_setup_status)
    assert cli.main(["status"]) == 0
    out = capsys.readouterr().out
    assert "UP" in out and "ltx" in out and "media-lab.service" in out and "GB10" in out
    assert seen == {"port": pairing.DEFAULT_PORT, "code": "ABCD2345"}


def test_pair_json_and_text(root, monkeypatch, capsys):
    cli.save_config({"port": 7891, "bind": "0.0.0.0"}, root)
    cli.write_code(root / "access-code.txt", "ABCD2345")
    cli.write_code(root / "admin-pin.txt", "0042")
    monkeypatch.setattr(cli, "manifest_up", lambda *a, **k: True)
    monkeypatch.setattr(pairing, "list_ipv4_addresses",
                        lambda runner=None: [pairing.make_address("127.0.0.1", "lo"),
                                             pairing.make_address("192.168.1.71", "en0")])
    assert cli.main(["pair", "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["link"] == "vibex://pair?medialab=http%3A%2F%2F192.168.1.71%3A7891"
    assert data["access_code"] == "ABCD2345" and data["admin_code"] == "0042" and data["up"] is True
    assert data["best"]["kind"] == "lan"
    pytest.importorskip("qrcode")
    assert cli.main(["pair"]) == 0
    text = capsys.readouterr().out
    assert "Media Lab is up." in text and data["link"] in text and "█" in text


def test_wait_returns_nonzero_when_process_dies(root, monkeypatch):
    monkeypatch.setattr(cli, "manifest_up", lambda *a, **k: False)
    assert cli.main(["wait", "--port", "1", "--pid", "999999999", "--timeout", "5"]) == 1


def test_uninstall_keeps_data_root(root, monkeypatch, capsys):
    venv = root / "fakevenv"
    (venv / "bin").mkdir(parents=True)
    cli.save_config({"port": 7891, "venv": str(venv)}, root)
    (root / "media").mkdir()
    (root / "media" / "keep.mp4").write_text("x")
    monkeypatch.setattr(cli, "remove_service", lambda out=print: None)
    monkeypatch.setattr(cli, "stop_pid", lambda root: False)
    assert cli.main(["uninstall", "--yes"]) == 0
    assert not venv.exists()
    assert not cli.config_path(root).exists()
    assert (root / "media" / "keep.mp4").exists()


def test_wrapper_script_points_at_cli():
    text = (cli.REPO / "tools" / "media-lab").read_text()
    assert "media_lab_core.cli" in text and text.startswith("#!/bin/sh")


def test_no_private_addresses_in_owned_files():
    """Public-safety: the installer files may not carry a tailnet address, a
    /home/<user> path, or a machine hostname from the box this code came from."""
    import re
    owned = ["install.sh", "requirements.txt", "media_lab_core/cli.py", "media_lab_core/pairing.py",
             "tools/media-lab", "systemd/media-lab.service", "launchd/com.medialab.server.plist",
             "docs/INSTALL.md"]
    tailnet_ip = re.compile(r"\b100\.(6[4-9]|[7-9]\d|1[01]\d|12[0-7])\.\d{1,3}\.\d{1,3}\b(?!/)")  # not the CGNAT CIDR
    home_path = re.compile(r"/home/[a-z][a-z0-9_-]+")
    hostname = re.compile(r"[a-z]+s-macbook-pro|_spark\d", re.I)
    for rel in owned:
        p = Path(cli.REPO) / rel
        if not p.exists():
            continue
        text = p.read_text()
        for pat in (tailnet_ip, home_path, hostname):
            assert not pat.search(text), f"{rel} matches {pat.pattern}: {pat.search(text).group(0)}"


def test_setup_json_forwards_options_without_legacy_config_or_network_probe(monkeypatch,capsys):
    monkeypatch.setattr(cli,"app_root",lambda: pytest.fail("planner read legacy root"))
    monkeypatch.setattr(pairing,"list_ipv4_addresses",lambda: pytest.fail("planner probed network"))
    assert cli.main(["setup","--list","--json"])==0
    result=json.loads(capsys.readouterr().out)
    assert result["models"]
    assert cli.main(["setup","--json","--select","missing-model"])==2
    result=capsys.readouterr()
    assert not result.out
    assert "error" in json.loads(result.err)


def test_setup_json_accepts_optional_separator_and_other_commands_reject_unknown_flags(capsys):
    assert cli.main(["setup","--","--json"])==0
    assert json.loads(capsys.readouterr().out)["selected"]==[]
    with pytest.raises(SystemExit) as exc:cli.main(["status","--not-a-real-option"])
    assert exc.value.code==2
