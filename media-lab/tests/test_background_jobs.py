import hashlib
import json
from unittest.mock import patch

import pytest

from media_lab_core import background_jobs as jobs
from media_lab_core.cpu_worker import WorkerBusy, cpu_slot
from media_lab_core.job_store import JobStore

OWNER = 'a' * 32
DATA = b'immutable test input; fake executor only'


def payload():
    return {'kind': 'image', 'engineId': jobs.ENGINE,
            'revision': json.loads(jobs.MANIFEST.read_text())['revision'], 'prompt': 'Remove background',
            'settings': {'operation': 'remove-background', 'inputId': 'b'*32,
                         'inputSha256': hashlib.sha256(DATA).hexdigest()}}


def setup(tmp_path):
    store = JobStore(tmp_path / 'jobs.sqlite')
    jid = store.enqueue_once(OWNER, 'request-background-001', 'image', payload())
    args = dict(root=tmp_path/'results', runtime=tmp_path/'python', package=tmp_path/'model',
                cache=tmp_path/'cache', read_input=lambda job: DATA)
    return store, jid, args


def test_queue_controller_keeps_lock_and_finishes_owned_job(tmp_path):
    store, jid, args = setup(tmp_path)
    legacy = store.enqueue('image', payload())
    other = store.enqueue_once(OWNER, 'request-another-engine', 'image', {**payload(), 'engineId': 'other'})
    def execute(**kwargs):
        assert kwargs['job_id'] == jid and kwargs['data'] == DATA
        assert not kwargs['cancelled']()
        with pytest.raises(WorkerBusy), cpu_slot(args['root']):
            pass
        return {'path': jid+'/output.png', 'bytes': 1, 'sha256': 'f'*64}
    result = jobs.run_next(store, **args, execute=execute)
    assert result['status'] == 'succeeded'
    assert result['result']['artifact']['path'] == jid+'/output.png'
    assert store.get(legacy)['status'] == store.get(other)['status'] == 'queued'
    assert jobs.run_next(store, **args, execute=execute) is None


def test_running_cancellation_wins_over_result_publication(tmp_path):
    store, jid, args = setup(tmp_path)
    def execute(**kwargs):
        store.request_cancel(jid)
        assert kwargs['cancelled']()
        return {'path': jid+'/output.png'}
    assert jobs.run_next(store, **args, execute=execute)['status'] == 'cancelled'
    assert store.get(jid)['result'] is None


def test_input_integrity_refuses_model_execution(tmp_path):
    store, jid, args = setup(tmp_path)
    args['read_input'] = lambda job: b'changed'
    with patch.object(jobs, 'run_background_job') as execute:
        assert jobs.run_next(store, **args, execute=execute)['status'] == 'failed'
        execute.assert_not_called()


def test_cancellation_race_at_terminal_transaction(tmp_path):
    store, jid, args = setup(tmp_path)
    original = store.transition
    def race(job_id, worker_id, target, **kwargs):
        if target == 'succeeded':
            store.request_cancel(jid)
        return original(job_id, worker_id, target, **kwargs)
    with patch.object(store, 'transition', side_effect=race):
        result = jobs.run_next(store, **args, execute=lambda **kwargs: {})
    assert result['status'] == 'cancelled' and result['result'] is None


def test_expired_claim_recovery_waits_for_actual_process_lock(tmp_path):
    store, jid, args = setup(tmp_path)
    with patch('media_lab_core.job_store.time.time', return_value=100):
        first = store.claim_cpu_job('crashed-worker', jobs.ENGINE)
    assert first['id'] == jid
    with cpu_slot(args['root']):
        with pytest.raises(WorkerBusy):
            jobs.run_next(store, **args, execute=lambda **kwargs: {})
        assert store.get(jid)['claimed_by'] == 'crashed-worker'
    result = jobs.run_next(store, **args, execute=lambda **kwargs: {'recovered': True})
    assert result['status'] == 'succeeded'
    assert result['claimed_by'] != 'crashed-worker'
    assert result['result']['artifact']['recovered']


def test_expired_cancelled_job_is_not_rerun(tmp_path):
    store, jid, args = setup(tmp_path)
    with patch('media_lab_core.job_store.time.time', return_value=100):
        store.claim_cpu_job('crashed-worker', jobs.ENGINE)
        store.request_cancel(jid)
    assert jobs.run_next(store, **args, execute=lambda **kwargs: pytest.fail('cancelled job ran')) is None
    assert store.get(jid)['status'] == 'cancelled'


def test_live_claim_is_not_reclaimed_and_revision_never_substituted(tmp_path):
    store, jid, args = setup(tmp_path)
    store.claim_cpu_job('live-worker', jobs.ENGINE)
    assert jobs.run_next(store, **args) is None
    wrong = {**payload(), 'revision': 'unqualified'}
    bad = store.enqueue_once(OWNER, 'request-wrong-revision', 'image', wrong)
    result = jobs.run_next(store, **args, execute=lambda **kwargs: pytest.fail('wrong model ran'))
    assert result['id'] == bad and result['status'] == 'failed'
    assert store.get(jid)['claimed_by'] == 'live-worker'
