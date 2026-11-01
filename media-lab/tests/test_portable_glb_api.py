import json
from fastapi.testclient import TestClient
from .test_cut_api import media_app
from .test_glb_contract import glb, fixture


def test_real_app_exposes_portable_marker_only_after_scoped_library_auth(media_app):
    path=media_app.MEDIA/'portable-test.glb';path.parent.mkdir(parents=True,exist_ok=True)
    data=glb(fixture());path.write_bytes(data)
    (media_app.ROOT/'gallery.json').write_text(json.dumps([{'id':'portable-model','status':'done','url':'/media/portable-test.glb'}]))
    client=TestClient(media_app.app,base_url='https://portable-test.invalid')
    url='/api/studio/library/portable-model/content?portable=1'
    assert client.get(url).status_code==401
    paired=client.post('/api/gate',json={'code':media_app.ACCESS_CODE,'studio_library':True}).json()
    client.cookies.clear()
    response=client.get(url,headers={'Authorization':'Bearer '+paired['token'],'Origin':'https://studio-test.invalid'})
    assert response.status_code==200 and response.content==data
    assert response.headers['X-Studio-Portable']=='glb-v1'
    assert 'X-Studio-Portable' in response.headers['Access-Control-Expose-Headers']
    assert response.headers['Access-Control-Allow-Origin']=='*'
    assert response.headers['Cache-Control']=='private, no-store'
    assert client.get(url,headers={'Authorization':'Bearer invalid'}).status_code==401
    bad=fixture();bad['buffers'][0]['uri']='https://outside.invalid/model.bin';path.write_bytes(glb(bad))
    rejected=client.get(url,headers={'Authorization':'Bearer '+paired['token']})
    assert rejected.status_code==422 and 'X-Studio-Portable' not in rejected.headers
