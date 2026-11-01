"""Warm music renderer: one owned process serves many jobs, is budgeted, cancellable and reaped when idle."""
import json
import os
import struct
import math
import wave
import io
import time
from pathlib import Path

import pytest

from media_lab_core import music_host as host
from media_lab_core.cpu_worker import WorkerStopped
from media_lab_core.music_worker import ResidentRenderer, run_music_job


def wav_bytes(seconds=12):
    buffer = io.BytesIO()
    with wave.open(buffer, 'wb') as out:
        out.setnchannels(2); out.setsampwidth(2); out.setframerate(48000)
        out.writeframes(b''.join(struct.pack('<h', int(9000 * math.sin(i / 17))) * 2 for i in range(seconds * 48000)))
    return buffer.getvalue()


FAKE = r'''
import hashlib, json, os, sys, time, shutil
from pathlib import Path
channel = os.fdopen(os.dup(1), 'w', buffering=1)
os.dup2(2, 1); sys.stdout = sys.stderr
args = sys.argv
revision = args[args.index('--revision') + 1]
sample = Path(os.environ['FAKE_WAV'])
for line in sys.stdin:
    job = json.loads(line)
    print('rendering', job, 'pid', os.getpid())  # goes to stderr, must never reach the channel
    data = Path(job['input']).read_bytes()
    if b'"hang"' in data:
        time.sleep(30)
    out = Path(job['output'])
    shutil.copy(sample, out / 'output.wav')
    meta = json.loads(os.environ['FAKE_META'])
    (out / 'receipt.json').write_text(json.dumps({'version': 1, 'revision': revision, 'inputSha256': hashlib.sha256(data).hexdigest(), 'audio': meta}, sort_keys=True))
    (out / 'pid').write_text(str(os.getpid()))
    channel.write(json.dumps({'ok': True}) + '\n')
'''


@pytest.fixture
def fake_runtime(tmp_path):
    from media_lab_core.music_artifact import inspect_wav
    sample = tmp_path / 'sample.wav'; sample.write_bytes(wav_bytes())
    script = tmp_path / 'fake_render.py'; script.write_text(FAKE)
    runtime = tmp_path / 'python'
    runtime.write_text(f'#!/bin/sh\nexport FAKE_WAV={sample}\nexport FAKE_META=\'{json.dumps(inspect_wav(sample.read_bytes()))}\'\n'
                       f'exec {os.environ.get("PYTHON", "python3")} {script} "$@"\n')
    runtime.chmod(0o700)
    return runtime


def make(tmp_path, fake_runtime, **kwargs):
    return ResidentRenderer(runtime=fake_runtime, checkpoints=tmp_path / 'ck', revision='rev-test',
                            home=tmp_path / 'resident', memory_bytes=1024**3, **kwargs)


def request(tmp_path, name, **extra):
    stage = tmp_path / name; out = stage / 'out'; out.mkdir(parents=True)
    body = {'prompt': 'calm piano', 'lyrics': '[inst]', 'seconds': 20, 'seed': 7, **extra}
    (stage / 'input.json').write_text(json.dumps(body))
    return stage / 'input.json', out


def test_one_process_serves_many_jobs_and_is_reaped_when_idle(tmp_path, fake_runtime):
    resident = make(tmp_path, fake_runtime, idle_seconds=60)
    first = resident.render(input_path=request(tmp_path, 'a')[0], output_dir=tmp_path / 'a/out', timeout=20)
    second = resident.render(input_path=request(tmp_path, 'b')[0], output_dir=tmp_path / 'b/out', timeout=20)
    assert (tmp_path / 'a/out/pid').read_text() == (tmp_path / 'b/out/pid').read_text()
    assert resident.loads == 1 and resident.renders == 2 and first['warm'] is False and second['warm'] is True
    assert (tmp_path / 'resident/resident.log').read_text().count('rendering') == 2  # library chatter went to the log
    assert resident.reap_idle(now=time.monotonic() + 30) is False and resident.alive()
    assert resident.reap_idle(now=time.monotonic() + 61) is True and not resident.alive()
    third = resident.render(input_path=request(tmp_path, 'c')[0], output_dir=tmp_path / 'c/out', timeout=20)
    assert resident.loads == 2 and third['warm'] is True


def test_cancellation_and_time_budget_terminate_the_warm_process(tmp_path, fake_runtime):
    resident = make(tmp_path, fake_runtime, idle_seconds=60)
    with pytest.raises(WorkerStopped, match='cancelled'):
        resident.render(input_path=request(tmp_path, 'h', note='hang')[0], output_dir=tmp_path / 'h/out', timeout=20,
                        cancelled=lambda: True)
    assert not resident.alive()
    with pytest.raises(RuntimeError, match='time budget'):
        resident.render(input_path=request(tmp_path, 'h2', note='hang')[0], output_dir=tmp_path / 'h2/out', timeout=0.5)
    assert not resident.alive()
    resident.render(input_path=request(tmp_path, 'ok')[0], output_dir=tmp_path / 'ok/out', timeout=20)
    assert resident.loads == 3


def test_run_music_job_uses_the_resident_and_still_verifies(tmp_path, fake_runtime):
    resident = make(tmp_path, fake_runtime, idle_seconds=60)
    root = tmp_path / 'jobs'; root.mkdir()
    data = json.dumps({'prompt': 'calm piano', 'lyrics': '[inst]', 'seconds': 20, 'seed': 7}).encode()
    kwargs = dict(root=root, runtime=fake_runtime, checkpoints=tmp_path / 'ck', inference_lock=tmp_path / 'lock',
                  revision='rev-test', resident=resident)
    one = run_music_job(job_id='a' * 32, data=data, **kwargs)
    two = run_music_job(job_id='b' * 32, data=data, **kwargs)
    assert one['recovered'] is False and two['recovered'] is False and resident.loads == 1
    assert (root / ('a' * 32) / 'output.wav').is_file()
    again = run_music_job(job_id='a' * 32, data=data, **kwargs)
    assert again['recovered'] is True and resident.renders == 2
    resident.terminate()


def test_config_accepts_optional_idle_and_rejects_bad_values(tmp_path):
    base = {'version': 1, 'runtime': '/r', 'runtime_sha256': 'a' * 64, 'checkpoints': '/c', 'weights_manifest': '/m',
            'inference_lock': '/l', 'revision': 'rev'}
    path = tmp_path / 'music.json'
    path.write_text(json.dumps(base)); assert 'resident_idle_seconds' not in host.read_config(path)
    path.write_text(json.dumps({**base, 'resident_idle_seconds': 0})); assert host.read_config(path)['resident_idle_seconds'] == 0
    for bad in (-1, 1.5, '600', 90000):
        path.write_text(json.dumps({**base, 'resident_idle_seconds': bad}))
        with pytest.raises(ValueError):
            host.read_config(path)
    path.write_text(json.dumps({**base, 'extra': 1}))
    with pytest.raises(ValueError):
        host.read_config(path)


def test_warm_renderer_only_needs_its_working_margin(tmp_path, fake_runtime, monkeypatch):
    import types
    from media_lab_core import music_worker
    from media_lab_core.cpu_worker import WorkerBusy
    resident = ResidentRenderer(runtime=fake_runtime, checkpoints=tmp_path / 'ck', revision='rev-test', home=tmp_path / 'resident',
                                memory_bytes=1024**3, idle_seconds=60, working_margin=64 * 1024**2)
    resident.render(input_path=request(tmp_path, 'a')[0], output_dir=tmp_path / 'a/out', timeout=20)
    assert resident.alive()
    monkeypatch.setattr(music_worker.psutil, 'virtual_memory', lambda: types.SimpleNamespace(available=512 * 1024**2))
    resident.render(input_path=request(tmp_path, 'b')[0], output_dir=tmp_path / 'b/out', timeout=20)  # below the model budget, above the margin
    resident.terminate()
    with pytest.raises(WorkerBusy):
        resident.render(input_path=request(tmp_path, 'c')[0], output_dir=tmp_path / 'c/out', timeout=20)  # cold start needs the whole budget


def test_renderer_killed_during_shutdown_requeues_instead_of_failing(tmp_path, fake_runtime):
    import threading
    from media_lab_core.cpu_worker import WorkerBusy
    stopping = threading.Event()
    resident = ResidentRenderer(runtime=fake_runtime, checkpoints=tmp_path / 'ck', revision='rev-test', home=tmp_path / 'resident',
                                memory_bytes=1024**3, idle_seconds=60, shutting_down=stopping.is_set)
    resident.render(input_path=request(tmp_path, 'a')[0], output_dir=tmp_path / 'a/out', timeout=20)
    pid = resident.process.pid

    def kill_soon():
        time.sleep(0.5); stopping.set(); os.kill(pid, 15)
    threading.Thread(target=kill_soon, daemon=True).start()
    with pytest.raises(WorkerBusy, match='shutting down'):
        resident.render(input_path=request(tmp_path, 'h', note='hang')[0], output_dir=tmp_path / 'h/out', timeout=20)
    assert not resident.alive()
    stopping.clear()
    resident.render(input_path=request(tmp_path, 'b')[0], output_dir=tmp_path / 'b/out', timeout=20)  # a later start recovers


def test_renderer_killed_by_the_platform_requeues_even_before_the_stop_flag(tmp_path, fake_runtime):
    """systemd signals the whole control group: the renderer can die before the host knows it is stopping."""
    import threading
    from media_lab_core.cpu_worker import WorkerBusy
    resident = ResidentRenderer(runtime=fake_runtime, checkpoints=tmp_path / 'ck', revision='rev-test', home=tmp_path / 'resident',
                                memory_bytes=1024**3, idle_seconds=60)  # shutting_down stays False throughout
    resident.render(input_path=request(tmp_path, 'a')[0], output_dir=tmp_path / 'a/out', timeout=20)
    pid = resident.process.pid
    threading.Thread(target=lambda: (time.sleep(0.5), os.kill(pid, 15)), daemon=True).start()
    with pytest.raises(WorkerBusy, match='shutting down'):
        resident.render(input_path=request(tmp_path, 'h', note='hang')[0], output_dir=tmp_path / 'h/out', timeout=20)


def test_a_renderer_that_dies_on_its_own_still_fails_the_job(tmp_path, fake_runtime):
    """A crash is not a shutdown: only SIGTERM/SIGINT or the host's own stop flag requeue."""
    import threading
    resident = ResidentRenderer(runtime=fake_runtime, checkpoints=tmp_path / 'ck', revision='rev-test', home=tmp_path / 'resident',
                                memory_bytes=1024**3, idle_seconds=60)
    resident.render(input_path=request(tmp_path, 'a')[0], output_dir=tmp_path / 'a/out', timeout=20)
    pid = resident.process.pid
    threading.Thread(target=lambda: (time.sleep(0.5), os.kill(pid, 9)), daemon=True).start()
    with pytest.raises(RuntimeError, match='exited unexpectedly'):
        resident.render(input_path=request(tmp_path, 'h', note='hang')[0], output_dir=tmp_path / 'h/out', timeout=20)


def test_terminate_does_not_wait_for_an_in_flight_render(tmp_path, fake_runtime):
    """A supervisor stop must end the runtime immediately, not queue behind the render holding the lock."""
    import threading
    from media_lab_core.cpu_worker import WorkerBusy
    resident = ResidentRenderer(runtime=fake_runtime, checkpoints=tmp_path / 'ck', revision='rev-test',
                                home=tmp_path / 'resident', memory_bytes=1024**3, idle_seconds=60)
    resident.render(input_path=request(tmp_path, 'a')[0], output_dir=tmp_path / 'a/out', timeout=20)
    failure = []
    def long_render():
        try:
            resident.render(input_path=request(tmp_path, 'h', note='hang')[0], output_dir=tmp_path / 'h/out', timeout=30)
        except Exception as error:  # noqa: BLE001 - recorded for the assertion below
            failure.append(error)
    thread = threading.Thread(target=long_render, daemon=True); thread.start()
    time.sleep(1)
    began = time.monotonic(); resident.terminate(); elapsed = time.monotonic() - began
    thread.join(10)
    assert elapsed < 8, f'terminate waited {elapsed:.1f}s for the render'
    assert failure and isinstance(failure[0], WorkerBusy), failure
    assert not resident.alive()


FAKE_LOAD_FAILS = r'''
import json, os, sys
channel = os.fdopen(os.dup(1), 'w', buffering=1)
os.dup2(2, 1); sys.stdout = sys.stderr
for line in sys.stdin:
    print('pretend load failure', file=sys.stderr)
    channel.write(json.dumps({'error': 'AcceleratorError', 'stage': 'load'}) + '\n')
'''


def test_a_model_that_cannot_load_requeues_with_backoff_then_gives_up(tmp_path, monkeypatch):
    from media_lab_core import music_worker
    from media_lab_core.cpu_worker import WorkerBusy
    script = tmp_path / 'fake_load_fails.py'; script.write_text(FAKE_LOAD_FAILS)
    runtime = tmp_path / 'python'
    runtime.write_text(f'#!/bin/sh\nexec {os.environ.get("PYTHON", "python3")} {script} "$@"\n'); runtime.chmod(0o700)
    resident = ResidentRenderer(runtime=runtime, checkpoints=tmp_path / 'ck', revision='rev-test', home=tmp_path / 'resident',
                                memory_bytes=1024**3, idle_seconds=60)
    clock = [1000.0]
    monkeypatch.setattr(music_worker.time, 'monotonic', lambda: clock[0])
    with pytest.raises(WorkerBusy, match='could not be loaded right now'):
        resident.render(input_path=request(tmp_path, 'a')[0], output_dir=tmp_path / 'a/out', timeout=20)
    assert not resident.alive() and resident.load_failures == 1
    assert 'pretend load failure' in (tmp_path / 'resident/resident.log').read_text()
    # inside the backoff window nothing is spawned
    with pytest.raises(WorkerBusy, match='waiting before trying again'):
        resident.render(input_path=request(tmp_path, 'b')[0], output_dir=tmp_path / 'b/out', timeout=20)
    assert resident.loads == 1
    clock[0] += music_worker.LOAD_RETRY_SECONDS + 1
    with pytest.raises(WorkerBusy):
        resident.render(input_path=request(tmp_path, 'c')[0], output_dir=tmp_path / 'c/out', timeout=20)
    clock[0] += music_worker.LOAD_RETRY_SECONDS + 1
    with pytest.raises(RuntimeError, match='repeated attempts'):
        resident.render(input_path=request(tmp_path, 'd')[0], output_dir=tmp_path / 'd/out', timeout=20)
    assert resident.loads == 3 and resident.load_failures == 0


def test_a_cold_start_reclaims_another_packs_idle_resident(tmp_path, fake_runtime, monkeypatch):
    """Two packs share one host: when memory is short for a cold start, the other pack's idle model is dropped."""
    import types
    from media_lab_core import music_worker
    from media_lab_core.cpu_worker import WorkerBusy
    warm = ResidentRenderer(runtime=fake_runtime, checkpoints=tmp_path / 'ck', revision='rev-a', home=tmp_path / 'a', memory_bytes=1024**3, idle_seconds=600)
    warm.render(input_path=request(tmp_path, 'w')[0], output_dir=tmp_path / 'w/out', timeout=20)
    assert warm.alive()
    cold = ResidentRenderer(runtime=fake_runtime, checkpoints=tmp_path / 'ck', revision='rev-b', home=tmp_path / 'b', memory_bytes=1024**3, idle_seconds=600)
    state = {'available': 512 * 1024**2}
    monkeypatch.setattr(music_worker.psutil, 'virtual_memory', lambda: types.SimpleNamespace(available=state['available']))
    # too recent to reclaim: the cold start stays queued and the warm one survives
    with pytest.raises(WorkerBusy):
        cold.render(input_path=request(tmp_path, 'c1')[0], output_dir=tmp_path / 'c1/out', timeout=20)
    assert warm.alive()
    # once the other resident has been idle long enough, it is dropped and memory comes back
    warm.last_used = time.monotonic() - music_worker.RECLAIM_IDLE_SECONDS - 1
    real_terminate = warm._terminate
    def freeing_terminate():
        real_terminate(); state['available'] = 2 * 1024**3
    monkeypatch.setattr(warm, '_terminate', freeing_terminate)
    cold.render(input_path=request(tmp_path, 'c2')[0], output_dir=tmp_path / 'c2/out', timeout=20)
    assert not warm.alive() and cold.alive()
    cold.terminate()
