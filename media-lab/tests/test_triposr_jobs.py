import hashlib
import pytest
from media_lab_core import triposr_jobs as jobs
from media_lab_core.job_store import JobStore
from media_lab_core.cpu_worker import cpu_slot, WorkerBusy

DATA=b'owned cutout; fake executor only'
OWNER='a'*32


def setup(root):
    payload={'kind':'model','engineId':jobs.ENGINE,'revision':jobs.MODEL_SHA,'settings':{
        'operation':'image-to-3d','variant':jobs.VARIANT,'inputId':'b'*32,
        'inputSha256':hashlib.sha256(DATA).hexdigest()}}
    store=JobStore(root/'jobs.sqlite')
    jid=store.enqueue_once(OWNER,'request-model-001','model',payload)
    return store,jid,payload


def test_model_queue_preserves_other_kinds_and_legacy_jobs(tmp_path):
    store,jid,payload=setup(tmp_path)
    image=store.enqueue_once(OWNER,'request-image-001','image',{**payload,'kind':'image'})
    legacy=store.enqueue('model',payload)
    def execute(**kwargs):
        assert kwargs['job_id']==jid and kwargs['data']==DATA
        assert not kwargs['cancelled']()
        with pytest.raises(WorkerBusy),cpu_slot(tmp_path): pass
        return {'path':jid+'/output.glb'}
    result=jobs.run_next(store,root=tmp_path,execute=execute,read_input=lambda _:DATA)
    assert result['status']=='succeeded'
    assert store.get(image)['status']==store.get(legacy)['status']=='queued'


def test_cancellation_wins(tmp_path):
    store,jid,_=setup(tmp_path)
    def execute(**kwargs):
        store.request_cancel(jid)
        assert kwargs['cancelled']()
        return {'path':jid+'/output.glb'}
    result=jobs.run_next(store,root=tmp_path,execute=execute,read_input=lambda _:DATA)
    assert result['status']=='cancelled' and result['result'] is None


@pytest.mark.parametrize('change',['input','variant'])
def test_wrong_input_or_variant_never_executes(tmp_path,change):
    store,jid,payload=setup(tmp_path)
    if change=='variant':
        store.request_cancel(jid)
        payload['settings']['variant']='different'
        store.enqueue_once(OWNER,'request-model-002','model',payload)
    result=jobs.run_next(store,root=tmp_path,execute=lambda **_:pytest.fail('executed'),
                         read_input=lambda _:b'changed' if change=='input' else DATA)
    assert result['status']=='failed'
