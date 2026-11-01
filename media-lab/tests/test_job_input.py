import hashlib
import io
from fastapi import FastAPI
from fastapi.testclient import TestClient
from PIL import Image
from media_lab_core.job_store import JobStore
from media_lab_core.studio_jobs import router, is_jobs_path


def test_owned_result_snapshots_without_library_permission(tmp_path):
    store=JobStore(tmp_path/'jobs.sqlite');owner='a'*32
    jid=store.enqueue_once(owner,'request-cutout-001','image',{'kind':'image'})
    directory=tmp_path/jid;directory.mkdir()
    stream=io.BytesIO();Image.new('RGBA',(4,3),(255,0,0,128)).save(stream,format='PNG');data=stream.getvalue()
    path=directory/'output.png';path.write_bytes(data)
    app=FastAPI();app.include_router(router(lambda:store,lambda token:{'one':owner,'two':'b'*32}.get(token),
                                           lambda:[],lambda _:None,tmp_path))
    client=TestClient(app);url=f'/api/studio/jobs/{jid}/input';headers={'Authorization':'Bearer one'}
    assert is_jobs_path(url)
    assert client.post(url).status_code==401
    assert client.post(url,headers={'Authorization':'Bearer two'}).status_code==404
    assert client.post(url,headers=headers).status_code==404
    store.claim_next('worker');store.transition(jid,'worker','succeeded',result={'artifact':{
        'path':jid+'/output.png','bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}})
    response=client.post(url,headers=headers)
    assert response.status_code==200
    accepted=response.json()
    assert accepted['sha256']==hashlib.sha256(data).hexdigest()
    assert accepted['width']==4 and accepted['height']==3
    assert store.input_metadata(owner,accepted['id']) is not None
    assert store.input_metadata('b'*32,accepted['id']) is None
    assert client.post(url,headers=headers).json()['id']==accepted['id']
    path.write_bytes(b'x'*len(data))
    assert client.post(url,headers=headers).status_code==409
