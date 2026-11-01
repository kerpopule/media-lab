import json,struct
import pytest
from media_lab_core.glb_contract import inspect_generated_glb


def glb(meta):
    raw=json.dumps(meta).encode();raw+=b' '*((-len(raw))%4);binary=b'\0'*12
    return struct.pack('<4sII',b'glTF',2,28+len(raw)+len(binary))+struct.pack('<II',len(raw),0x4e4f534a)+raw+struct.pack('<II',len(binary),0x004e4942)+binary


def fixture():
    return {'asset':{'version':'2.0'},'buffers':[{'byteLength':12}], 'bufferViews':[{'buffer':0,'byteLength':12}], 'meshes':[{'primitives':[]}]}


def test_container_retains_embedded_data_and_rejects_external_dependencies():
    meta=fixture();assert inspect_generated_glb(glb(meta))['self_contained']
    meta['buffers'][0]['uri']='https://example.com/model.bin'
    with pytest.raises(ValueError,match='embedded buffer'):inspect_generated_glb(glb(meta))
    meta=fixture();meta['images']=[{'uri':'texture.png'}]
    with pytest.raises(ValueError,match='embedded buffer views'):inspect_generated_glb(glb(meta))


def test_truncation_and_out_of_bounds_views_are_rejected():
    with pytest.raises(ValueError,match='header'):inspect_generated_glb(glb(fixture())[:-1])
    meta=fixture();meta['bufferViews'][0]['byteOffset']=8
    with pytest.raises(ValueError,match='exceeds'):inspect_generated_glb(glb(meta))


def test_portable_library_response_validates_exact_returned_bytes(tmp_path):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from media_lab_core.studio_library import router
    path=tmp_path/'chair.glb';data=glb(fixture());path.write_bytes(data)
    app=FastAPI();app.include_router(router(lambda:[{'id':'chair','url':'/media/chair.glb','status':'done'}],tmp_path,lambda token:token=='test'))
    client=TestClient(app);url='/api/studio/library/chair/content?portable=1'
    assert client.get(url).status_code==401
    response=client.get(url,headers={'Authorization':'Bearer test'})
    assert response.status_code==200 and response.content==data
    assert response.headers['X-Studio-Portable']=='glb-v1'
    bad=fixture();bad['images']=[{'uri':'outside.png'}];path.write_bytes(glb(bad))
    assert client.get(url,headers={'Authorization':'Bearer test'}).status_code==422
