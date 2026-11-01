import hashlib
from fastapi import FastAPI
from fastapi.testclient import TestClient
from media_lab_core.job_store import JobStore
from media_lab_core.studio_jobs import router
from tests.test_triposr_result import result as make_result


def test_owned_model_content_returns_exact_portable_bytes(tmp_path):
    store=JobStore(tmp_path/'jobs.sqlite')
    owner='a'*32
    jid=store.enqueue_once(owner,'request-model-content-001','model',{'kind':'model'})
    directory=tmp_path/jid;directory.mkdir()
    _,data=make_result(directory)
    store.claim_next('worker')
    store.transition(jid,'worker','succeeded',result={'artifact':{
        'path':jid+'/output.glb','bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}})
    app=FastAPI();app.include_router(router(lambda:store,lambda token:owner if token=='one' else 'b'*32,
                                           lambda:[],lambda _:None,artifact_root=tmp_path))
    client=TestClient(app);url=f'/api/studio/jobs/{jid}/content'
    assert client.get(url).status_code==401
    assert client.get(url,headers={'Authorization':'Bearer two'}).status_code==404
    headers={'Authorization':'Bearer one'}
    response=client.get(url,headers=headers)
    assert response.status_code==200 and response.content==data
    assert response.headers['content-type']=='model/gltf-binary'
    assert response.headers['x-studio-portable']=='glb-v1'
    assert response.headers['cache-control']=='no-store'
    assert client.get(f'/api/studio/jobs/{jid}/preview',headers=headers).status_code==404
    (directory/'output.glb').write_bytes(b'x'*len(data))
    assert client.get(url,headers=headers).status_code==409
