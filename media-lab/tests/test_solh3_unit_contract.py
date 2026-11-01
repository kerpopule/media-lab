import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def test_h3_is_a_dormant_loaded_static_unit_with_bounded_cgroup_policy():
    unit = (ROOT / "config/media-lab-sol-h3.service").read_text()
    assert "ExecStartPre=%h/media-lab-simple/.venv/bin/python -m media_lab_core.solh3_control_guard --check-heartbeat" in unit
    assert "Slice=media-lab-h3.slice" in unit
    assert "KillMode=control-group" in unit
    assert "TimeoutStopSec=10s" in unit
    assert "MemorySwapMax=2G" in unit
    assert "Restart=no" in unit
    assert "\n[Install]\n" not in unit


def test_guard_is_independent_and_stops_exact_h3_unit_if_observer_is_lost():
    unit = (ROOT / "config/solh3-control-plane-guard.service").read_text()
    assert "Slice=media-lab-control-plane.slice" in unit
    assert "MemoryLow=64M" in unit
    assert "MemoryMin=64M" in unit
    assert "CPUWeight=10000" in unit
    assert "ExecStopPost=-/usr/bin/systemctl --user --no-block stop media-lab-sol-h3.service" in unit
    assert "WantedBy=default.target" in unit


def test_installer_is_explicit_and_preserves_rollback_artifact():
    source = (ROOT / "tools/install-solh3-control-guard.sh").read_text()
    assert '--install --apply | --rollback BACKUP_DIR --apply' in source
    assert 'refusing install while $H3_UNIT is active' in source
    assert 'release-receipt.json' in source
    assert 'rollback_artifact' in source
    assert 'guard_heartbeat' in source
    assert 'boot_clearance_schema' in source
    assert 'source root must resolve to $EXPECTED_ROOT' in source
    assert 'install failed; restoring $BACKUP' in source
    assert 'cp -a "$backup/$unit" "$target"' in source
    assert 'enabled-runtime) systemctl --user enable --runtime' in source


def test_failed_install_transaction_restores_exact_prior_unit_files(tmp_path):
    home = tmp_path / "home"
    config = tmp_path / "config"
    state = tmp_path / "state"
    bins = tmp_path / "bin"
    units = config / "systemd/user"
    home.mkdir()
    units.mkdir(parents=True)
    state.mkdir()
    bins.mkdir()
    (home / "media-lab-simple").symlink_to(ROOT, target_is_directory=True)
    h3 = units / "media-lab-sol-h3.service"
    guard = units / "solh3-control-plane-guard.service"
    h3.write_text("prior h3\n")
    guard.write_text("prior guard\n")
    systemctl = bins / "systemctl"
    systemctl.write_text(
        "#!/bin/sh\n"
        "case \"$*\" in\n"
        "  '--user is-active --quiet media-lab-sol-h3.service') exit 1 ;;\n"
        "  '--user is-enabled solh3-control-plane-guard.service') echo enabled; exit 0 ;;\n"
        "  '--user is-active solh3-control-plane-guard.service') echo inactive; exit 3 ;;\n"
        "  '--user enable --now solh3-control-plane-guard.service') exit 42 ;;\n"
        "  *) exit 0 ;;\n"
        "esac\n"
    )
    systemctl.chmod(0o755)
    env = dict(
        os.environ,
        HOME=str(home),
        XDG_CONFIG_HOME=str(config),
        XDG_STATE_HOME=str(state),
        PATH=str(bins) + os.pathsep + os.environ["PATH"],
    )
    result = subprocess.run(
        ["bash", str(home / "media-lab-simple/tools/install-solh3-control-guard.sh"),
         "--install", "--apply"],
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 42
    assert "install failed; restoring" in result.stderr
    assert h3.read_text() == "prior h3\n"
    assert guard.read_text() == "prior guard\n"
