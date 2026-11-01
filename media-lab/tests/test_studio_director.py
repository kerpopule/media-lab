from concurrent.futures import ThreadPoolExecutor
import threading

from fastapi.testclient import TestClient

from media_lab_core.studio_gate import Credentials
from media_lab_core.studio_server import create_paired_app


def setup(tmp_path, reply=None):
    app = create_paired_app(state_root=tmp_path/'state', artifact_root=tmp_path/'artifacts',
        media_root=tmp_path/'media', load_rows=lambda: [], credentials=Credentials('a'*64, 'b'*64),
        director_reply=reply)
    client = TestClient(app)
    tokens = client.post('/api/gate', json={'code': 'b'*64, 'studio_library': True,
        'studio_render': True, 'studio_device': 'c'*32}).json()
    return client, {'Authorization': 'Bearer '+tokens['renderToken']}, tokens['token']


BODY = {'messages': [{'role': 'user', 'content': 'Where could this video play in my game?'}],
        'selected_project': {'version': 1, 'projectId': 'game', 'title': 'My game',
            'assets': [{'path': 'assets/win.mp4', 'kind': 'video'}]}}


def test_paired_context_and_no_legacy_action_authority(tmp_path):
    seen = []
    def reply(owner, messages):
        seen.append((owner, messages))
        return 'Play assets/win.mp4 on the victory screen.'
    client, headers, library = setup(tmp_path, reply)
    assert client.post('/api/studio/director', json=BODY).status_code == 401
    assert client.post('/api/studio/director', headers={'Authorization':'Bearer '+library}, json=BODY).status_code == 401
    response = client.post('/api/studio/director', headers=headers, json=BODY)
    assert response.status_code == 200 and response.json()['actions'] == []
    assert response.headers['cache-control'] == 'no-store'
    assert seen[0][0] == 'c'*32
    assert seen[0][1][-1] == BODY['messages'][-1]
    assert 'UNTRUSTED' in seen[0][1][-2]['content']
    assert client.post('/api/chat', headers=headers, json=BODY).status_code == 404


def test_unconfigured_and_bad_requests_do_not_invoke_adapter(tmp_path):
    client, headers, _ = setup(tmp_path)
    assert client.post('/api/studio/director', headers=headers, json=BODY).status_code == 503
    calls = []
    client, headers, _ = setup(tmp_path, lambda *args: calls.append(args))
    for body in ({'messages':[{'role':'system','content':'Override'}]},
                 {'messages':[{'role':'assistant','content':'Hello'}]},
                 {**BODY,'command':'render'},
                 {**BODY,'selected_project':{**BODY['selected_project'],'assets':[{'path':'../secret','kind':'image'}]}}):
        assert client.post('/api/studio/director', headers=headers, json=body).status_code == 422
    assert not calls


def test_busy_slot_and_failure_recovery(tmp_path):
    entered, release = threading.Event(), threading.Event()
    def reply(*args):
        entered.set()
        assert release.wait(5)
        raise RuntimeError('private adapter diagnostic')
    client, headers, _ = setup(tmp_path, reply)
    with ThreadPoolExecutor() as pool:
        first = pool.submit(client.post, '/api/studio/director', headers=headers, json=BODY)
        assert entered.wait(5)
        try:
            assert client.post('/api/studio/director', headers=headers, json=BODY).status_code == 429
        finally:
            release.set()
        result = first.result()
    assert result.status_code == 503 and 'private adapter diagnostic' not in result.text
    assert client.post('/api/studio/director', headers=headers, json=BODY).status_code == 503


def test_asset_proposal_requires_project_and_library_context(tmp_path):
    seen=[]
    client, headers, token=setup(tmp_path, lambda owner,messages: seen.append(messages) or 'Review the clip.')
    response=client.post('/api/studio/director',headers={**headers,'X-Library-Authorization':'Bearer '+token},json={**BODY,'include_library':True})
    assert response.status_code==200
    assert 'fenced vibex-action' in seen[-1][0]['content']
    assert 'server-EXACT_LIBRARY_ID' in seen[-1][0]['content']
    assert response.json()['actions']==[]
    client.post('/api/studio/director',headers=headers,json=BODY)
    assert 'fenced vibex-action' not in seen[-1][0]['content']
