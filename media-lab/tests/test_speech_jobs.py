import json
from unittest.mock import patch
import pytest
from media_lab_core import speech_jobs as jobs
from media_lab_core.job_store import JobStore
from media_lab_core.cpu_worker import cpu_slot, WorkerBusy


def setup(tmp_path):
    store=JobStore(tmp_path/'jobs.sqlite')
    payload={'kind':'audio','engineId':jobs.ENGINE,'revision':'test-exact-revision',
        'prompt':'Welcome to your creative studio.','settings':{'operation':'speak','voice':jobs.VOICE,'seed':7}}
    jid=store.enqueue_once('a'*32,'speech-test-request-01','audio',payload)
    return store,jid,payload,dict(root=tmp_path/'results',revision='test-exact-revision')


def test_text_uses_owned_queue_without_image_snapshot(tmp_path):
    store,jid,payload,args=setup(tmp_path)
    other=store.enqueue_once('b'*32,'speech-other-engine-01','audio',{**payload,'engineId':'other'})
    legacy=store.enqueue('audio',payload)
    def execute(**kw):
        assert kw['job_id']==jid
        assert json.loads(kw['data'])=={'text':payload['prompt'],'voice':jobs.VOICE,'seed':7}
        assert not kw['cancelled']()
        with pytest.raises(WorkerBusy),cpu_slot(args['root']):pass
        return {'path':jid+'/output.wav'}
    assert jobs.run_next(store,**args,execute=execute)['status']=='succeeded'
    assert store.get(other)['status']==store.get(legacy)['status']=='queued'


def test_cancel_wins_over_speech_result(tmp_path):
    store,jid,_,args=setup(tmp_path)
    def execute(**kw):
        store.request_cancel(jid);assert kw['cancelled']();return {'path':'ignored.wav'}
    assert jobs.run_next(store,**args,execute=execute)['status']=='cancelled'
    assert store.get(jid)['result'] is None


def test_expired_claim_recovers_only_after_cpu_lock_released(tmp_path):
    store,jid,_,args=setup(tmp_path)
    with patch('media_lab_core.job_store.time.time',return_value=100):
        store.claim_cpu_job('expired',jobs.ENGINE,kind='audio')
    with cpu_slot(args['root']),pytest.raises(WorkerBusy):
        jobs.run_next(store,**args,execute=lambda **kw:{})
    assert jobs.run_next(store,**args,execute=lambda **kw:{})['status']=='succeeded'
    assert store.get(jid)['claimed_by']!='expired'


def test_wrong_revision_never_executes(tmp_path):
    store,jid,_,args=setup(tmp_path);args['revision']='different'
    assert jobs.run_next(store,**args,execute=lambda **kw:pytest.fail('wrong revision ran'))['status']=='failed'


@pytest.mark.parametrize('change',[{'voice':'other'},{'seed':True},{'seed':-1},{'operation':'clone'}])
def test_invalid_settings_are_rejected(tmp_path,change):
    _,_,payload,_=setup(tmp_path);payload['settings'].update(change)
    with pytest.raises(ValueError):jobs.validate_payload(payload,'test-exact-revision')


def test_restart_preserves_text_and_explicit_voice(tmp_path):
    store,jid,payload,args=setup(tmp_path)
    reopened=JobStore(tmp_path/'jobs.sqlite')
    seen=[]
    result=jobs.run_next(reopened,**args,execute=lambda **kw:seen.append(json.loads(kw['data'])) or {})
    assert result['id']==jid and result['status']=='succeeded'
    assert seen==[{'text':payload['prompt'],'voice':jobs.VOICE,'seed':7}]
