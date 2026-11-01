"""config/local.env is the one per-host source: env > file > defaults, no literals."""
import importlib
import subprocess
import sys
from pathlib import Path

import pytest

from media_lab_core import local_config

ROOT = Path(__file__).resolve().parents[1]


def test_defaults_describe_a_private_single_machine_install(monkeypatch, tmp_path):
    monkeypatch.setenv("MEDIA_LAB_HOME", str(tmp_path))          # no local.env there
    for key in local_config.DEFAULTS:
        if key != "MEDIA_LAB_HOME":
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setattr(local_config, "SOURCE_ROOT", tmp_path)   # and none in the checkout
    assert local_config.home() == tmp_path
    assert local_config.bind_host() == "127.0.0.1"
    assert local_config.public_hosts() == set()
    assert local_config.own_addresses() == {"127.0.0.1", "localhost"}
    assert local_config.text_upstream() == "http://127.0.0.1:8004"
    assert local_config.studio_url() == "http://127.0.0.1:7863"
    assert local_config.models_root() == Path("~/.local/share/media-lab-p2-models").expanduser()
    assert local_config.runtime_root() == Path("~/runtime").expanduser()
    assert not local_config.sol_configured()


def test_file_then_environment_override(monkeypatch, tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "local.env").write_text(
        "# per-host\n"
        "export MEDIA_LAB_BIND_HOST='10.1.2.3'\n"
        "MEDIA_LAB_PUBLIC_HOSTS=studio.example.com, Second.Example.org\n"
        "MEDIA_LAB_TAILNET_HOST=10.1.2.3\n"
        "MEDIA_LAB_RUNTIME_ROOT=~/rt\n"
        "SOL_PKG=/opt/sol\n"
        "NOT_A_KEY=ignored\n")
    monkeypatch.setenv("MEDIA_LAB_HOME", str(tmp_path))
    monkeypatch.delenv("MEDIA_LAB_BIND_HOST", raising=False)
    monkeypatch.delenv("MEDIA_LAB_TEXT_UPSTREAM", raising=False)
    assert local_config.bind_host() == "10.1.2.3"
    assert local_config.public_hosts() == {"studio.example.com", "second.example.org"}
    assert local_config.own_addresses() == {"127.0.0.1", "localhost", "10.1.2.3"}
    assert local_config.studio_url() == "http://10.1.2.3:7863"
    assert local_config.runtime_root() == Path("~/rt").expanduser()
    assert local_config.sol_configured() and local_config.sol()["SOL_PKG"] == "/opt/sol"
    assert "NOT_A_KEY" not in local_config.load()
    monkeypatch.setenv("MEDIA_LAB_BIND_HOST", "0.0.0.0")
    assert local_config.bind_host() == "0.0.0.0"
    assert local_config.studio_url() == "http://127.0.0.1:7863"
    assert "0.0.0.0" not in local_config.own_addresses()
    env = local_config.subprocess_env({"PATH": "/usr/bin"})
    assert env["MEDIA_LAB_BIND_HOST"] == "0.0.0.0" and env["SOL_PKG"] == "/opt/sol"


def test_example_file_lists_every_key_and_no_real_host():
    example = ROOT / "config" / "local.env.example"
    parsed = local_config.parse_env_file(example)
    assert set(parsed) == set(local_config.DEFAULTS), set(local_config.DEFAULTS) ^ set(parsed)
    assert parsed["MEDIA_LAB_BIND_HOST"] == "127.0.0.1"
    assert parsed["MEDIA_LAB_PUBLIC_HOSTS"] == "" and parsed["SOL_PKG"] == ""


def test_shell_helper_agrees_with_python(monkeypatch, tmp_path):
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "local.env").write_text(
        "MEDIA_LAB_HOME=~/lab\nMEDIA_LAB_BIND_HOST=10.9.8.7\nMEDIA_LAB_RUNTIME_ROOT=~/rt\n")
    runner = tmp_path / "runner"
    runner.mkdir()
    (runner / "local_env.sh").write_bytes((ROOT / "runner" / "local_env.sh").read_bytes())
    script = ('. "$(dirname "$0")/local_env.sh"; echo "$MEDIA_LAB_HOME|$MEDIA_LAB_RUNTIME_ROOT|'
              '$MEDIA_LAB_STUDIO_URL|${MEDIA_LAB_PUBLISH_8004[*]}"')
    probe = runner / "probe.sh"
    probe.write_text("#!/usr/bin/env bash\nset -Eeuo pipefail\n" + script + "\n")
    env = {"HOME": str(tmp_path), "PATH": "/usr/bin:/bin"}
    out = subprocess.run(["bash", str(probe)], capture_output=True, text=True, env=env, check=True).stdout.strip()
    assert out == (f"{tmp_path}/lab|{tmp_path}/rt|http://10.9.8.7:7863|"
                   "-p 127.0.0.1:8004:8000 -p 10.9.8.7:8004:8000")


def test_cli_prints_resolved_values(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("MEDIA_LAB_HOME", str(tmp_path))
    monkeypatch.setenv("MEDIA_LAB_TAILNET_HOST", "10.0.0.5")
    assert local_config.main([]) == 0
    text = capsys.readouterr().out
    assert "MEDIA_LAB_TAILNET_HOST=10.0.0.5" in text and f"MEDIA_LAB_HOME={tmp_path}" in text
    assert local_config.main(["--sh"]) == 0
    assert "export MEDIA_LAB_TAILNET_HOST='10.0.0.5'" in capsys.readouterr().out


def test_identity_guard_passes_on_the_tracked_tree():
    guard = ROOT / "tools" / "identity_guard.py"
    result = subprocess.run([sys.executable, str(guard)], capture_output=True, text=True, cwd=ROOT)
    assert result.returncode == 0, result.stderr


def test_identity_guard_catches_a_private_host(tmp_path):
    guard = ROOT / "tools" / "identity_guard.py"
    leak = tmp_path / "leak.sh"
    leak.write_text("ssh someone@" + ".".join(["100", "66", "238", "97"]) + " ls\n")  # never the literal
    result = subprocess.run([sys.executable, str(guard), str(leak)], capture_output=True, text=True, cwd=ROOT)
    assert result.returncode == 1 and "leak.sh:1" in result.stderr
