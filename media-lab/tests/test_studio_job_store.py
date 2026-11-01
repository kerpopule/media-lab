from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from media_lab_core.job_store import JobStore, RequestConflict

OWNER = 'a' * 32
OTHER = 'b' * 32
REQUEST = 'retry-request-0001'


def test_concurrent_retries_and_restart_produce_one_claimable_job(tmp_path):
    path = tmp_path / 'queue.sqlite'
    store = JobStore(path)
    with ThreadPoolExecutor(max_workers=8) as workers:
        ids = list(workers.map(lambda _: JobStore(path).enqueue_once(
            OWNER, REQUEST, 'image', {'seed': 3, 'prompt': 'badge'}), range(16)))
    assert len(set(ids)) == 1
    reopened = JobStore(path)
    assert reopened.enqueue_once(OWNER, REQUEST, 'image', {'prompt': 'badge', 'seed': 3}) == ids[0]
    claimed = store.claim_next('worker')
    assert claimed['id'] == ids[0]
    assert claimed['payload'] == {'seed': 3, 'prompt': 'badge'}
    assert store.claim_next('worker') is None
    store.transition(ids[0], 'worker', 'succeeded', result={'asset_id': 'badge'})
    assert reopened.enqueue_once(OWNER, REQUEST, 'image', {'prompt': 'badge', 'seed': 3}) == ids[0]
    assert reopened.get_owned(OWNER, ids[0])['result'] == {'asset_id': 'badge'}


def test_owner_isolation_and_changed_retry_conflicts(tmp_path):
    store = JobStore(tmp_path / 'queue.sqlite')
    jid = store.enqueue_once(OWNER, REQUEST, 'image', {'prompt': 'badge'})
    assert store.get_owned(OTHER, jid) is None
    assert store.get_owned(OWNER, 'missing') is None
    legacy = store.enqueue('image', {'prompt': 'legacy'})
    assert store.get_owned(OWNER, legacy) is None
    other = store.enqueue_once(OTHER, REQUEST, 'image', {'prompt': 'badge'})
    assert other != jid
    for kind, payload in [('video', {'prompt': 'badge'}), ('image', {'prompt': 'changed'})]:
        with pytest.raises(RequestConflict):
            store.enqueue_once(OWNER, REQUEST, kind, payload)
    assert store.get_owned(OWNER, jid)['payload'] == {'prompt': 'badge'}


def test_ownership_failure_rolls_back_job_and_leaves_retry_possible(tmp_path):
    store = JobStore(tmp_path / 'queue.sqlite')
    with store.connect() as db:
        db.execute("CREATE TRIGGER fail_request BEFORE INSERT ON studio_requests "
                   "BEGIN SELECT RAISE(ABORT, 'simulated write failure'); END")
    with pytest.raises(sqlite3.IntegrityError):
        store.enqueue_once(OWNER, REQUEST, 'image', {'prompt': 'badge'})
    assert store.claim_next('worker') is None
    with store.connect() as db:
        db.execute('DROP TRIGGER fail_request')
    jid = store.enqueue_once(OWNER, REQUEST, 'image', {'prompt': 'badge'})
    assert store.claim_next('worker')['id'] == jid


def test_additive_migration_preserves_existing_jobs(tmp_path):
    path = tmp_path / 'queue.sqlite'
    original = JobStore(path)
    jid = original.enqueue('image', {'prompt': 'already queued'})
    with original.connect() as db:
        db.execute('DROP TABLE studio_requests')
    upgraded = JobStore(path)
    assert upgraded.get(jid) == original.get(jid)
    assert upgraded.claim_next('worker')['id'] == jid


@pytest.mark.parametrize('owner,request_id,payload', [
    ('', REQUEST, {}), (OTHER.upper(), REQUEST, {}),
    (OWNER, '../bad', {}), (OWNER, REQUEST, {'seed': float('nan')}),
])
def test_invalid_requests_never_enter_queue(tmp_path, owner, request_id, payload):
    store = JobStore(tmp_path / 'queue.sqlite')
    with pytest.raises(ValueError):
        store.enqueue_once(owner, request_id, 'image', payload)
    assert store.claim_next('worker') is None
