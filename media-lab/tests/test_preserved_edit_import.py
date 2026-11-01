from PIL import Image
from fastapi.testclient import TestClient
from media_lab_core import cut
from media_lab_core.studio_server import create_paired_app
from media_lab_core.studio_gate import Credentials


def test_preserved_import_is_scoped_idempotent_and_keeps_newer_edits(tmp_path):
    media=tmp_path/'media';media.mkdir();image=media/'frame.png';Image.new('RGB',(64,48),'blue').save(image)
    pid='cut-1234567890'
    manifest=cut.build_gallery_project(pid,'Preserved',[{**cut.probe_gallery_file(image),'id':'asset-original'}])
    cut.create_project(tmp_path/'state/preserved-edits',manifest)
    c=Credentials('a'*64,'b'*64)
    app=create_paired_app(state_root=tmp_path/'state',artifact_root=tmp_path/'artifacts',media_root=media,load_rows=lambda:[],credentials=c)
    with TestClient(app) as client:
        path=f'/api/studio/editing/preserved/{pid}/import'
        assert client.post(path).status_code==401
        def grants(owner):
            edit=client.post('/api/gate',json={'code':c.code,'studio_edit':True,'studio_device':owner}).json()['editToken']
            library=client.post('/api/gate',json={'code':c.code,'studio_library':True}).json()['token']
            return {'Authorization':'Bearer '+edit,'X-Library-Authorization':'Bearer '+library}
        headers=grants('c'*32)
        assert client.post(path,headers={'Authorization':headers['Authorization']}).status_code==403
        assert client.get('/api/studio/editing/preserved',headers=headers).json()['projects'][0]['project_id']==pid
        first=client.post(path,headers=headers);assert first.status_code==200,first.text
        project=first.json()['project'];clip=project['timeline']['tracks'][0]['clips'][0]['id']
        edited=client.post(f'/api/studio/editing/projects/{pid}/transactions',headers=headers,json={'transactionId':'preserved-trim-transaction','revision':0,'commands':[{'id':'trim','type':'clip.trim','payload':{'clip_id':clip,'trim_in_frames':0,'trim_out_frames':48}}]})
        assert edited.status_code==200,edited.text
        assert client.post(path,headers=headers).json()['project']['revision']==1
        other=grants('d'*32)
        assert client.get(f'/api/studio/editing/projects/{pid}',headers=other).status_code==404
        assert client.post(path,headers=other).json()['project']['revision']==0
        assert cut.open_project(tmp_path/'state/preserved-edits',pid).load()['revision']==0
