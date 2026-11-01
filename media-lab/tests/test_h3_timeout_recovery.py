import ast
from pathlib import Path


ROOT = Path(__file__).parents[1]
APP = ROOT / "app.py"
ENGINE = ROOT / "runner" / "engine_server.py"


def _load_function(path: Path, name: str):
    tree = ast.parse(path.read_text())
    node = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == name)
    namespace = {}
    exec(compile(ast.Module(body=[node], type_ignores=[]), str(path), "exec"), namespace)
    return namespace[name]


def test_video_page_model_field_identifies_h3_for_exact_stop():
    job_engine = _load_function(APP, "job_engine")
    assert job_engine({"kind": "video", "request": {"model": "h3"}}) == "h3"
    assert job_engine({"kind": "video", "request": {"model": "ltx25"}}) == "ltx"
    assert job_engine({"kind": "say", "request": {"engine": "h3"}}) == "h3"


def test_h3_client_deadline_and_request_id_are_wired():
    source = APP.read_text()
    assert "H3_ENGINE_HTTP_TIMEOUT_S = 24 * 60 * 60" in source
    assert 'body.setdefault("request_id", str(j["id"]))' in source
    assert 'timeout = max(timeout, H3_ENGINE_HTTP_TIMEOUT_S)' in source


def test_engine_reconnect_is_idempotent_and_atomically_published():
    source = ENGINE.read_text()
    assert "idempotent_out = OUT / f'job-{request_id}.mp4'" in source
    assert "'cached': True" in source
    assert ".partial.mp4" in source
    assert "os.replace(staged_out, out)" in source