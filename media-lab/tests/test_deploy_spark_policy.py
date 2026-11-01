from pathlib import Path


ROOT = Path(__file__).parents[1]
DEPLOY = ROOT / "tools" / "deploy-spark.sh"


def test_deploy_includes_gpu_capacity_receipts():
    text = DEPLOY.read_text()
    assert "+ /config/gpu-capacity-receipts.json" in text


def test_deploy_accepts_tagged_commits_without_remote_branch():
    text = DEPLOY.read_text()
    branch_line = next(line for line in text.splitlines() if line.startswith('BRANCH="$(git branch -r'))
    assert "|| true" in branch_line


def test_rollback_pointer_is_the_absolute_backup_path():
    """BACKUP is built from the remote's absolute $HOME; prefixing "~/" gave the
    malformed "~//home/..." pointer that deployed-source.json carried."""
    from pathlib import Path
    script = (Path(__file__).parents[1] / "tools" / "deploy-spark.sh").read_text()
    assert 'f"~/{backup}"' not in script
    assert '"rollback": backup,' in script
    assert 'BACKUP="$REMOTE_ABS_HOME/' in script


def test_every_repo_config_json_is_shipped_or_owned_by_the_box():
    """A config table the code reads must reach the box: render-eta.json shipped
    in #44 but was not allowlisted, so the model picker on the studio host
    showed no estimates at all."""
    text = DEPLOY.read_text()
    unshipped_by_design = {"qwen-image-21-weights.json"}
    for path in sorted((ROOT / "config").glob("*.json")):
        shipped = f"'+ /config/{path.name}'" in text
        owned = f"'/config/{path.name}'" in text
        assert shipped or owned or path.name in unshipped_by_design, path.name


def test_runtime_patches_are_shipped():
    """tools/sol-h3-runtime-patch.py installs patches/sol-h3-spark/geometry.py on
    the studio host; without the patches/ tree on the box it has nothing to install."""
    assert "'+ /patches/***'" in DEPLOY.read_text()
    assert (ROOT / "patches" / "sol-h3-spark" / "geometry.py").is_file()
