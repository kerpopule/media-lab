"""A busy shared resource hands the claimed job back to the queue instead of failing it."""
import json
from pathlib import Path

from media_lab_core.cpu_jobs import run_next_cpu
from media_lab_core.cpu_worker import WorkerBusy
from media_lab_core.job_store import JobStore
import pytest


def test_worker_busy_requeues_and_a_later_worker_succeeds(tmp_path):
    store = JobStore(tmp_path / 'jobs.sqlite')
    payload = {'engineId': 'e', 'revision': 'r', 'kind': 'audio', 'prompt': 'p', 'settings': {'operation': 'compose'}}
    job = {'id': store.enqueue_once('a1b2c3d4e5f60718293a4b5c6d7e8f90', 'release-request-0001', 'audio', payload)}
    calls = []

    def busy(**kwargs):
        calls.append('busy'); raise WorkerBusy('GPU busy')

    def ok(**kwargs):
        calls.append('ok'); return {'path': kwargs['job_id'] + '/x', 'bytes': 1, 'sha256': 'a' * 64}
    common = dict(root=tmp_path, engine='e', kind='audio', stage='s', validate=lambda p: None, failure_message='failed',
                  prepare_input=lambda j: b'{}')
    with pytest.raises(WorkerBusy):
        run_next_cpu(store, execute=busy, **common)
    row = store.get(job['id'])
    assert row['status'] == 'queued' and row['claimed_by'] is None and row['error'] is None
    assert run_next_cpu(store, execute=ok, **common)['status'] == 'succeeded'
    assert calls == ['busy', 'ok']
