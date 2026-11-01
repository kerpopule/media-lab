import json
from fastapi.testclient import TestClient
from media_lab_core.studio_server import create_paired_app
from media_lab_core.studio_gate import Credentials
from media_lab_core.director_library import collections_context

def test_bounded_summary_excludes_raw_references():
    data={'characters':[{'id':str(i),'name':'Cast','archived':True,'appearance':'A character','sheet_url':'/private/sheet.png','beats':[{}]} for i in range(10)],'relationshipIssues':[{'recordId':'0','targetId':'/private/missing.png'}]}
    message=collections_context(data)
    groups=json.loads(message['content'].split('\n',1)[1])
    assert groups[0]['available']==10 and len(groups[0]['records'])==8
    assert groups[0]['records'][0]['missingReferences']==1
    assert groups[0]['records'][0]['archived'] is True
    assert '/private/' not in message['content']

def test_explicit_request_and_library_permission_required(tmp_path):
    loads=[];calls=[]
    def load():loads.append(True);return {'characters':[{'id':'cast','name':'Retained cast'}]}
    app=create_paired_app(state_root=tmp_path/'state',artifact_root=tmp_path/'artifacts',media_root=tmp_path/'media',load_rows=lambda:[],credentials=Credentials('a'*64,'b'*64),load_collections=load,director_reply=lambda owner,messages:calls.append(messages) or 'Plan')
    with TestClient(app) as client:
        tokens=client.post('/api/gate',json={'code':'b'*64,'studio_library':True,'studio_render':True,'studio_device':'c'*32}).json()
        headers={'Authorization':'Bearer '+tokens['renderToken']}
        body={'messages':[{'role':'user','content':'Who is in my cast?'}]}
        assert client.post('/api/studio/director',headers=headers,json=body).status_code==200
        assert not loads
        body['include_collections']=True
        assert client.post('/api/studio/director',headers=headers,json=body).status_code==403
        assert not loads
        headers['X-Library-Authorization']='Bearer '+tokens['token']
        assert client.post('/api/studio/director',headers=headers,json=body).status_code==200
        assert loads==[True]
        assert 'Retained cast' in calls[-1][-2]['content']
        assert calls[-1][-1]==body['messages'][0]
