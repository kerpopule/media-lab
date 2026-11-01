import copy
import hashlib
import json
import wave

from fastapi.testclient import TestClient
from PIL import Image

from media_lab_core.studio_gate import Credentials
from media_lab_core.studio_server import create_paired_app


def test_storyboard_import_scope_retry_restart_and_original_preservation(tmp_path):
    media = tmp_path/'media'
    media.mkdir()
    Image.new('RGB', (64, 48), 'purple').save(media/'scene.png')
    before = (media/'scene.png').read_bytes()
    with wave.open(str(media/'song.wav'), 'wb') as song:
        song.setnchannels(1); song.setsampwidth(2); song.setframerate(8000); song.writeframes(b'\0\0'*8000)
    board = {'id': 'old-board', 'title': 'Original', 'song_url': '/media/song.wav', 'beats': [{'title': f'Scene {i}', 'duration': '1'} for i in range(20)]}
    original = copy.deepcopy(board)
    args = dict(state_root=tmp_path/'state', artifact_root=tmp_path/'artifacts',
                credentials=Credentials('1'*64, '2'*64), media_root=media,
                load_rows=lambda: [{'id': 'image', 'url': '/media/scene.png'}, {'id': 'song', 'url': '/media/song.wav'}],
                load_collections=lambda: {'storyboards': [board]})
    with TestClient(create_paired_app(**args)) as client:
        grant = client.post('/api/gate', json={'code': '2'*64, 'studio_library': True, 'studio_edit': True, 'studio_device': 'a'*32}).json()
        library = {'Authorization': 'Bearer '+grant['token']}
        headers = {'Authorization': 'Bearer '+grant['editToken'], 'X-Library-Authorization': 'Bearer '+grant['token']}
        record = client.get('/api/studio/collections/storyboards', headers=library).json()['records'][0]
        assert record['soundtrackAsset']['id'] == 'song'
        body = {'requestId': 'storyboard-import-review-01', 'storyboardId': 'old-board', 'sourceSha256': record['sourceSha256'],
                'title': 'Editing copy', 'fps': 24, 'scenes': [{'assetId': 'image', 'seconds': 1.0} for _ in range(20)]}
        assert client.post('/api/studio/editing/storyboards', json=body).status_code == 401
        assert client.post('/api/studio/editing/storyboards', headers={'Authorization': headers['Authorization']}, json=body).status_code == 403
        assert client.post('/api/studio/editing/storyboards', headers=headers, json={**body, 'sourceSha256': '0'*64}).status_code == 409
        assert client.post('/api/studio/editing/storyboards', headers=headers, json={**body, 'scenes': body['scenes'][:-1]}).status_code == 422
        created = client.post('/api/studio/editing/storyboards', headers=headers, json=body)
        assert created.status_code == 200, created.text
        project = created.json()['project']
        # No soundtrack retains the fingerprint used before musicAssetId was
        # added to the API, so an older uncertain request remains resumable.
        assert project['provenance']['studio_request_sha256'] == hashlib.sha256(json.dumps(body, sort_keys=True).encode()).hexdigest()
        assert len(project['timeline']['tracks'][0]['clips']) == 20
        assert project['duration_seconds'] == 20
        assert client.post('/api/studio/editing/storyboards', headers=headers, json=body).json() == created.json()
        assert client.post('/api/studio/editing/storyboards', headers=headers, json={**body, 'title': 'Different'}).status_code == 409
        music = client.post('/api/studio/editing/storyboards', headers=headers, json={**body, 'requestId': 'storyboard-with-music-01', 'musicAssetId': 'song'})
        assert music.status_code == 200, music.text
        music_project = music.json()['project']
        assert music_project['duration_seconds'] == 20
        assert music_project['timeline']['tracks'][1]['clips'][0]['duration_frames'] == 24
        assert client.post('/api/studio/editing/storyboards', headers=headers, json={**body, 'requestId': 'storyboard-with-music-01', 'musicAssetId': 'missing'}).status_code == 409
        other = client.post('/api/gate', json={'code': '2'*64, 'studio_edit': True, 'studio_device': 'b'*32}).json()
        assert client.get('/api/studio/editing/projects/'+project['project_id'], headers={'Authorization': 'Bearer '+other['editToken']}).status_code == 404
    with TestClient(create_paired_app(**args)) as client:
        assert client.post('/api/studio/editing/storyboards', headers=headers, json=body).json()['project'] == project
    assert board == original
    assert (media/'scene.png').read_bytes() == before
