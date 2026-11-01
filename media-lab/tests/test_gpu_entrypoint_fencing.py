import ast
import json
import os
import subprocess
import sys
import time
from pathlib import Path


ROOT = Path(__file__).parents[1]
APP = ROOT / "app.py"


def _function(name):
    tree = ast.parse(APP.read_text(encoding="utf-8"))
    return next(node for node in tree.body if isinstance(node, ast.FunctionDef) and node.name == name)


def _calls(node):
    return {call.func.id for call in ast.walk(node)
            if isinstance(call, ast.Call) and isinstance(call.func, ast.Name)}


def test_music3_and_tts_enter_canonical_gpu_operation():
    assert "gpu_operation" in _calls(_function("run_music"))
    assert "gpu_operation" in _calls(_function("vb_generate"))
    assert "gpu_render_ready" in _calls(_function("vb_generate"))


def test_maestro_runner_authorizes_before_model_api_import():
    source = (ROOT / "runner" / "maestro_queue_runner.py").read_text(encoding="utf-8")
    authorize = source.index('supplied["engine"] != "maestro"')
    model_import = source.index("from shared.api import init")
    assert authorize < model_import
    assert 'supplied["task"] != "generate"' in source


def test_maestro_runner_refuses_stale_delegation_before_model_import(tmp_path):
    settings = tmp_path / "settings.json"
    receipt = tmp_path / "receipt.json"
    delegation = tmp_path / "delegation.json"
    settings.write_text('{"model_type":"fixture"}', encoding="utf-8")
    delegated = {"fence": "8", "job_id": "job-8", "engine": "maestro",
                 "task": "generate", "issued_at": time.time() - 121}
    delegation.write_text(json.dumps(delegated), encoding="utf-8")
    env = {**os.environ, "MEDIA_LAB_GPU_FENCE": "8", "MEDIA_LAB_GPU_JOB_ID": "job-8",
           "MEDIA_LAB_GPU_ENGINE": "maestro", "MEDIA_LAB_GPU_TASK": "generate"}
    result = subprocess.run([sys.executable, str(ROOT / "runner" / "maestro_queue_runner.py"),
                             str(settings), str(receipt), str(delegation)],
                            capture_output=True, text=True, env=env)
    assert result.returncode != 0
    assert "exact Maestro GPU lease delegation is required" in result.stderr
    assert "No module named 'shared'" not in result.stderr
    assert delegation.exists(), "refused delegation must not be consumed"


def test_maestro_runner_refuses_missing_delegation_before_model_import(tmp_path):
    settings = tmp_path / "settings.json"
    settings.write_text('{"model_type":"fixture"}', encoding="utf-8")
    result = subprocess.run([sys.executable, str(ROOT / "runner" / "maestro_queue_runner.py"),
                             str(settings), str(tmp_path / "receipt.json"),
                             str(tmp_path / "missing-delegation.json")],
                            capture_output=True, text=True, env=os.environ.copy())
    assert result.returncode != 0
    assert "exact Maestro GPU lease delegation is required" in result.stderr
    assert "No module named 'shared'" not in result.stderr