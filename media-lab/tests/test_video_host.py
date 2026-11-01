"""Wan2.2 video pack: request contract, MP4 gate, host verification and server routing."""
import json
import shutil
import subprocess
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from media_lab_core import video_host as host
from media_lab_core import video_jobs
from media_lab_core.video_artifact import inspect_mp4
from media_lab_core.video_request import decode_request
from media_lab_core.studio_cli import initialize, read_credentials, parser, application

FFMPEG = shutil.which('ffmpeg') or '/opt/homebrew/bin/ffmpeg'


@pytest.fixture(scope='module')
def clip(tmp_path_factory):
    if not Path(FFMPEG).exists():
        pytest.skip('ffmpeg needed to synthesise a test clip')
    path = tmp_path_factory.mktemp('clip') / 'clip.mp4'
    subprocess.run([FFMPEG, '-v', 'error', '-y', '-f', 'lavfi', '-i', 'testsrc2=size=704x1280:rate=24:duration=0.375',
                    '-c:v', 'libx264', '-pix_fmt', 'yuv420p', '-movflags', '+faststart', str(path)], check=True)
    return path.read_bytes()


def write_pack(tmp_path):
    runtime = tmp_path/'python'; runtime.write_bytes(b'#!/bin/sh\nexit 0\n'); runtime.chmod(0o700)
    source = tmp_path/'source'; (source/'wan').mkdir(parents=True); (source/'wan/__init__.py').write_text('')
    subprocess.run(['git', '-C', str(source), 'init', '-q'], check=True)
    subprocess.run(['git', '-C', str(source), '-c', 'user.email=t@t', '-c', 'user.name=t', 'commit', '-q', '--allow-empty', '-m', 'pin'], check=True)
    commit = subprocess.run(['git', '-C', str(source), 'rev-parse', 'HEAD'], capture_output=True, text=True, check=True).stdout.strip()
    ck = tmp_path/'checkpoints'; ck.mkdir(); (ck/'w.safetensors').write_bytes(b'w' * 50)
    manifest = tmp_path/'weights-manifest.json'
    manifest.write_text(json.dumps({'files': [{'path': 'w.safetensors', 'bytes': 50, 'sha256': host.sha256_file(ck/'w.safetensors')}]}))
    config = {'version': 1, 'runtime': str(runtime), 'runtime_sha256': host.sha256_file(runtime), 'source': str(source),
              'source_commit': commit, 'checkpoints': str(ck), 'weights_manifest': str(manifest),
              'inference_lock': str(tmp_path/'lock'), 'revision': 'wan22-test-rev', 'resident_idle_seconds': 0}
    path = tmp_path/'video.json'; path.write_text(json.dumps(config))
    return path, config, source


def test_request_bounds():
    good = {'prompt': 'a boat', 'frames': 25, 'size': '704*1280', 'steps': 20, 'seed': 7}
    assert decode_request(json.dumps(good).encode())['frames'] == 25
    for bad in ({**good, 'frames': 24}, {**good, 'frames': 125}, {**good, 'size': '512*512'}, {**good, 'steps': 5},
                {**good, 'seed': -1}, {**good, 'prompt': ' '}, {k: v for k, v in good.items() if k != 'seed'}):
        with pytest.raises(ValueError):
            decode_request(json.dumps(bad).encode())


def test_mp4_gate_accepts_a_real_h264_clip_and_rejects_others(clip):
    meta = inspect_mp4(clip)
    assert (meta['width'], meta['height'], meta['codec'], meta['mimeType']) == (704, 1280, 'avc1', 'video/mp4')
    assert 0.3 <= meta['durationSeconds'] <= 0.5
    with pytest.raises(ValueError):
        inspect_mp4(b'\x00' * 100)
    with pytest.raises(ValueError):
        inspect_mp4(clip[:-100])  # truncated: the last box escapes the file
    with pytest.raises(ValueError):
        inspect_mp4(clip.replace(b'avc1', b'hev1'))  # every brand and sample entry


def test_payload_validation_requires_exact_engine_and_settings():
    good = {'engineId': video_jobs.ENGINE, 'revision': 'r', 'kind': 'video', 'prompt': 'boat',
            'settings': {'operation': 'text-to-video', 'frames': 25, 'size': '704*1280', 'steps': 20, 'seed': 1}}
    video_jobs.validate_payload(good, 'r')
    for bad in ({**good, 'revision': 'x'}, {**good, 'kind': 'audio'}, {**good, 'settings': {**good['settings'], 'frames': 26}},
                {**good, 'settings': {**good['settings'], 'extra': 1}}, {**good, 'settings': {**good['settings'], 'operation': 'compose'}}):
        with pytest.raises(ValueError):
            video_jobs.validate_payload(bad, 'r')


def test_verify_pack_checks_runtime_commit_and_weights(tmp_path):
    path, config, source = write_pack(tmp_path)
    assert host.verify_pack(host.read_config(path)) == 1
    with pytest.raises(ValueError, match='pinned commit'):
        host.verify_pack({**config, 'source_commit': '0' * 40})
    (tmp_path/'checkpoints/w.safetensors').write_bytes(b'x' * 50)
    with pytest.raises(ValueError, match='manifest'):
        host.verify_pack(config)
    with pytest.raises(ValueError):
        host.read_config(tmp_path/'missing.json') if False else host.read_config(Path('relative.json'))


def test_server_routes_video_jobs_and_saves_to_library(tmp_path, monkeypatch, clip):
    path, config, _ = write_pack(tmp_path)
    root = tmp_path/'host'; initialize(root)
    monkeypatch.setattr(host.video_jobs, 'run_next', lambda *a, **k: None)
    app = application(parser().parse_args(['serve', str(root), '--video-config', str(path)]))
    with TestClient(app) as client:
        assert client.get('/manifest.json').json()['vibexStudio']['video'] is True
        paired = client.post('/api/gate', json={'code': read_credentials(root).code, 'studio_render': True, 'studio_library': True, 'studio_device': 'e'*32}).json()
        render = {'Authorization': 'Bearer ' + paired['renderToken']}
        for _ in range(50):
            engines = client.get('/api/studio/engines', headers=render).json()['engines']
            if engines: break
            threading.Event().wait(0.1)
        assert [e['id'] for e in engines] == [video_jobs.ENGINE] and engines[0]['operation'] == 'text-to-video'
        good = {'requestId': 'video-request-00001', 'engineId': video_jobs.ENGINE, 'revision': 'wan22-test-rev', 'kind': 'video',
                'prompt': 'a paper boat', 'settings': {'operation': 'text-to-video', 'frames': 9, 'size': '704*1280', 'steps': 20, 'seed': 7}}
        assert client.post('/api/studio/jobs', headers=render, json={**good, 'settings': {**good['settings'], 'frames': 10}}).status_code == 409
        job = client.post('/api/studio/jobs', headers=render, json=good).json()
        store = app.state.background_host.get_store()
        claimed = store.claim_cpu_job('w', video_jobs.ENGINE, kind='video'); assert claimed['id'] == job['id']
        meta = inspect_mp4(clip)
        (root/'artifacts'/job['id']).mkdir(parents=True); (root/'artifacts'/job['id']/'output.mp4').write_bytes(clip)
        store.transition(job['id'], 'w', 'succeeded', result={'artifact': {**meta, 'path': job['id'] + '/output.mp4', 'recovered': False}})
        content = client.get(f"/api/studio/jobs/{job['id']}/content", headers=render)
        assert content.status_code == 200 and content.headers['content-type'].startswith('video/mp4') and content.content == clip
        saved = client.post(f"/api/studio/jobs/{job['id']}/library", headers=render)
        assert saved.status_code == 200, saved.text
        rows = json.loads((root/'library.json').read_text())
        assert rows[-1]['url'].startswith('/media/Videos/Generated/') and rows[-1]['engine'] == 'Video (Wan2.2 TI2V-5B)' and rows[-1]['title'] == 'a paper boat'


def test_stop_ends_the_runtime_before_waiting_for_the_worker(tmp_path):
    """A supervisor stop must not wait out an in-flight render: the host ends its own runtime first."""
    import threading, time
    from media_lab_core.video_host import VideoHost
    host_instance = VideoHost(lambda: None, tmp_path)
    order = []
    started = threading.Event(); release = threading.Event()

    class Resident:
        def terminate(self):
            order.append('runtime stopped'); release.set()

    def worker():
        started.set(); release.wait(10); order.append('worker returned')
    host_instance.resident = Resident()
    host_instance.thread = threading.Thread(target=worker, daemon=True)
    host_instance.thread.start(); started.wait(5)
    began = time.monotonic()
    host_instance.stop()
    assert order == ['runtime stopped', 'worker returned']
    assert time.monotonic() - began < 5
    assert host_instance.stop_requested.is_set()
