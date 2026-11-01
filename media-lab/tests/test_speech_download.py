"""Owned speech downloads return the exact decoded and verified audio buffer."""
import hashlib
from fastapi import FastAPI
from fastapi.testclient import TestClient
import pytest
from media_lab_core.job_store import JobStore
from media_lab_core.studio_jobs import router
from media_lab_core.speech_jobs import ENGINE
from .test_speech_artifact import wav


def setup(tmp_path, data, *, suffix='.wav', terminal='succeeded'):
    store = JobStore(tmp_path / 'jobs.sqlite')
    jid = store.enqueue_once('a' * 32, 'speech-download-0001', 'audio', {'engineId': ENGINE})
    root = tmp_path / 'artifacts'
    folder = root / jid
    folder.mkdir(parents=True)
    output = folder / ('output' + suffix)
    output.write_bytes(data)
    artifact = {'path': f'{jid}/{output.name}', 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
    store.claim_next('test-worker')
    if terminal == 'cancelled':
        store.request_cancel(jid)
    store.transition(jid, 'test-worker', terminal, result={'artifact': artifact})
    app = FastAPI()
    app.include_router(router(lambda: store, lambda token: {'one': 'a' * 32, 'two': 'b' * 32}.get(token),
                              lambda: [], lambda _: None, root))
    return TestClient(app), f'/api/studio/jobs/{jid}/content', output


def test_owned_speech_download_and_same_size_tampering(tmp_path):
    data = wav()
    client, url, output = setup(tmp_path, data)
    assert client.get(url).status_code == 401
    assert client.get(url, headers={'Authorization': 'Bearer two'}).status_code == 404
    response = client.get(url, headers={'Authorization': 'Bearer one'})
    assert response.status_code == 200
    assert response.content == data
    assert response.headers['content-type'] == 'audio/wav'
    assert response.headers['x-content-sha256'] == hashlib.sha256(data).hexdigest()
    assert response.headers['cache-control'] == 'private, no-store'
    altered = bytearray(data)
    altered[-1] ^= 1
    output.write_bytes(altered)
    assert client.get(url, headers={'Authorization': 'Bearer one'}).status_code == 409


@pytest.mark.parametrize('data,suffix', [(wav(silent=True), '.wav'), (wav(rate=16000), '.wav'),
                                         (wav()[:-2], '.wav'), (wav(), '.mp3')])
def test_hash_alone_does_not_accept_invalid_speech(tmp_path, data, suffix):
    client, url, _ = setup(tmp_path, data, suffix=suffix)
    assert client.get(url, headers={'Authorization': 'Bearer one'}).status_code == 409


def test_cancelled_speech_is_never_downloadable(tmp_path):
    client, url, _ = setup(tmp_path, wav(), terminal='cancelled')
    assert client.get(url, headers={'Authorization': 'Bearer one'}).status_code == 404


def test_download_returns_checked_buffer_if_file_changes_after_validation(tmp_path, monkeypatch):
    from media_lab_core import speech_artifact
    data = wav()
    client, url, output = setup(tmp_path, data)
    inspect = speech_artifact.inspect_wav
    def inspect_then_change(buffer):
        result = inspect(buffer)
        output.write_bytes(b'changed after validation')
        return result
    monkeypatch.setattr(speech_artifact, 'inspect_wav', inspect_then_change)
    response = client.get(url, headers={'Authorization': 'Bearer one'})
    assert response.status_code == 200 and response.content == data
