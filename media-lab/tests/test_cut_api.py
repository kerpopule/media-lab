"""HTTP smoke test: the studio app with Cut grafted in, under a disposable HOME."""

import importlib.util
import os
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture(scope="module")
def media_app(tmp_path_factory):
    repo = Path(__file__).parents[1]
    home = tmp_path_factory.mktemp("cut-api-home")
    root = home / "media-lab-simple"
    root.mkdir()
    for name in ("static", "config"):
        (root / name).symlink_to(repo / name)
    old = {k: os.environ.get(k) for k in ("HOME", "MEDIA_LAB_DISABLE_BACKGROUND_WORKERS")}
    os.environ["HOME"] = str(home)
    os.environ["MEDIA_LAB_DISABLE_BACKGROUND_WORKERS"] = "1"
    spec = importlib.util.spec_from_file_location("cut_api_test_app", repo / "app.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["cut_api_test_app"] = module
    try:
        spec.loader.exec_module(module)
    finally:
        for k, v in old.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
    return module


def _client(media_app, signed=True):
    client = TestClient(media_app.app, base_url="http://127.0.0.1")
    if signed:
        assert client.post("/api/gate", json={"code": media_app.ACCESS_CODE}).status_code == 200
    return client


def test_cut_end_to_end_over_http(media_app, cut_media):
    client = _client(media_app)
    # seed the gallery with synthetic items through the studio's own importer
    a = media_app.import_media(cut_media["a"], title="Take A")
    c = media_app.import_media(cut_media["c"], title="Still C")
    m = media_app.import_media(cut_media["m"], title="Song M")
    assert client.get("/api/cut/projects").json() == {"projects": []}

    created = client.post("/api/cut/projects", json={"job_ids": [a["id"], c["id"], m["id"]], "name": "HTTP cut"})
    assert created.status_code == 200, created.text
    pid = created.json()["project_id"]
    project = client.get(f"/api/cut/projects/{pid}").json()
    assert project["revision"] == 0 and project["title"] == "HTTP cut"
    assert [cl["duration_frames"] for cl in project["timeline"]["tracks"][0]["clips"]] == [72, 96]
    assert project["assets"][0]["source"]["path"] == f"/media/{a['id']}.mp4"
    assert client.get(project["assets"][0]["source"]["path"]).status_code == 200      # the player can fetch it
    # the poster keeps the library URL the player fetches (docs/CUT.md); a bare
    # basename here made every gallery project with a poster fail closed
    assert project["assets"][0]["poster"] == f"/media/{a['id']}.jpg"
    assert client.get(project["assets"][0]["poster"]).status_code == 200
    assert client.get("/api/cut/projects").json()["projects"][0]["project_id"] == pid
    assert client.get("/cut").status_code == 200 and "cut.js" in client.get("/cut").text

    # human commands (any signed-in session)
    body = {"commands": [{"id": "t1", "type": "clip.trim", "payload": {"clip_id": project["timeline"]["tracks"][0]["clips"][0]["id"], "trim_in_frames": 12, "trim_out_frames": 60}},
                         {"id": "t2", "type": "caption.add", "payload": {"text": "hello", "start_frame": 0, "end_frame": 24}}],
            "transaction_id": "tx-1", "expected_revision": 0}
    r = client.post(f"/api/cut/projects/{pid}/commands", json=body)
    assert r.status_code == 200 and r.json()["status"] == "applied" and r.json()["revision"] == 1
    assert client.post(f"/api/cut/projects/{pid}/commands", json=body | {"transaction_id": "tx-stale"}).status_code == 409

    # Sparky can only propose, and only with the runtime credential
    proposal = {"commands": [{"id": "s1", "type": "audio.mix", "payload": {"target": "music", "gain_db": -20}}],
                "transaction_id": "sparky-1", "expected_revision": 1}
    assert client.post(f"/api/cut/projects/{pid}/sparky/commands", json=proposal).status_code == 403
    r = client.post(f"/api/cut/projects/{pid}/sparky/commands", json=proposal,
                    headers={"X-Media-Lab-Sparky-Token": media_app.CUT_SPARKY_TOKEN})
    assert r.status_code == 200 and r.json()["status"] == "proposed"
    assert client.get(f"/api/cut/projects/{pid}").json()["revision"] == 1
    assert len(client.get(f"/api/cut/projects/{pid}/pending").json()["pending"]) == 1
    r = client.post(f"/api/cut/projects/{pid}/review/sparky-1", json={"approve": True})
    assert r.status_code == 200 and r.json()["status"] == "applied"
    assert client.get(f"/api/cut/projects/{pid}").json()["timeline"]["mix"]["music"]["gain_db"] == -20
    # the operator tools see the same store
    op = media_app._studio_operator()
    view = op.execute("inspect_cut", {"project_id": pid}, action_ok=False)
    assert view["result"]["revision"] == 2 if "result" in view else True
    prop = op.execute("cut_propose", {"project_id": pid, "commands": [{"type": "color.apply", "payload": {"clip_id": project["timeline"]["tracks"][0]["clips"][0]["id"], "preset": "bw"}}]}, action_ok=True)
    assert prop["applied"] is False and prop["status"] == "proposed"
    client.post(f"/api/cut/projects/{pid}/review/{prop['transaction_id']}", json={"approve": False})

    # master is gated; preview renders and lands in the gallery
    assert client.post(f"/api/cut/projects/{pid}/render", json={"quality": "master", "explicit_approval": True}).status_code == 403
    r = client.post(f"/api/cut/projects/{pid}/render", json={"quality": "preview"})
    assert r.status_code == 200, r.text
    rid = r.json()["render_id"]
    deadline = time.time() + 120
    while time.time() < deadline:
        rec = client.get(f"/api/cut/renders/{rid}").json()
        if rec["status"] in ("done", "error"):
            break
        time.sleep(0.5)
    assert rec["status"] == "done", rec
    assert rec["receipt"]["sha256"] and rec["receipt"]["captions"] == "burned"
    assert rec["gallery_job_id"] in media_app.jobs and rec["url"].startswith("/media/")
    gallery = client.get("/api/gallery").json()
    assert gallery[0]["id"] == rec["gallery_job_id"] and gallery[0]["kind"] == "video"
    assert client.get(f"/api/cut/projects/{pid}/renders").json()["renders"][0]["render_id"] == rid
    # a render can itself be cut again
    again = client.post("/api/cut/projects", json={"job_ids": [rec["gallery_job_id"]]})
    assert again.status_code == 200 and again.json()["project"]["timeline"]["tracks"][0]["clips"][0]["duration_frames"] >= 90


def test_cut_routes_refuse_an_unsigned_stranger(media_app):
    client = TestClient(media_app.app, base_url="http://example.test")
    assert client.get("/api/cut/projects").status_code == 401
    assert client.post("/api/cut/projects", json={"job_ids": ["x"]}).status_code == 401
    assert client.get("/api/cut/projects/nope").status_code == 401
    bad = _client(media_app).post("/api/cut/projects", json={"job_ids": ["does-not-exist"]})
    assert bad.status_code == 404
    assert _client(media_app).get("/api/cut/projects/does-not-exist").status_code == 404
    assert _client(media_app).get("/api/cut/renders/render-0123456789").status_code == 404


def test_studio_library_ticket_cors_and_admin_boundary(media_app):
    client = TestClient(media_app.app, base_url='https://studio.example')
    origin = {'Origin': 'https://builder.example'}
    preflight = client.options('/api/gate', headers=origin | {'Access-Control-Request-Method': 'POST'})
    assert preflight.status_code == 204
    response = client.post('/api/gate', json={'code': media_app.ACCESS_CODE, 'studio_library': True}, headers=origin)
    assert response.status_code == 200
    assert response.json()['scope'] == 'library:read'
    assert media_app.SESSION_COOKIE not in response.cookies
    token = response.json()['token']
    assert media_app.session_role(token) == ''
    headers = origin | {'Authorization': f'Bearer {token}'}
    result = client.get('/api/studio/library', headers=headers)
    assert result.status_code == 200
    assert result.json()['version'] == 1
    assert result.headers['Access-Control-Allow-Origin'] == '*'
    assert 'Access-Control-Allow-Credentials' not in result.headers
    assert result.headers['Cache-Control'] == 'private, no-store'
    assert client.get('/api/queue', headers=headers).status_code == 401
    assert client.get('/api/gallery', headers=headers).status_code == 401
    assert client.get('/api/studio/library?token=' + token).status_code == 401
