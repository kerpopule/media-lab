from fastapi.testclient import TestClient
from media_lab_core.studio_server import create_paired_app
from media_lab_core.studio_gate import Credentials


def test_collections_require_library_scope_before_loading_and_preserve_ids(tmp_path):
    credentials=Credentials('a'*64,'b'*64);calls=[]
    def load():
        calls.append(True)
        return {'characters':[{'id':'original-character','voice_id':'original-voice'}]}
    app=create_paired_app(state_root=tmp_path/'state',artifact_root=tmp_path/'artifacts',media_root=tmp_path/'media',load_rows=lambda:[],credentials=credentials,load_collections=load)
    with TestClient(app) as client:
        path='/api/studio/collections/characters'
        assert client.get(path).status_code==401
        assert calls==[]
        job=client.post('/api/gate',json={'code':credentials.code,'studio_render':True,'studio_device':'c'*32}).json()['token']
        assert client.get(path,headers={'Authorization':'Bearer '+job}).status_code==401
        assert calls==[]
        token=client.post('/api/gate',json={'code':credentials.code,'studio_library':True}).json()['token']
        headers={'Authorization':'Bearer '+token}
        assert client.get(path,headers=headers).json()['records'][0]['id']=='original-character'
        assert client.get(path+'/original-character',headers=headers).json()['record']['voice_id']=='original-voice'
        assert client.get(path+'/missing',headers=headers).status_code==404
        assert client.get('/api/studio/collections/credentials',headers=headers).status_code==404
        assert client.get('/manifest.json').json()['vibexStudio']['libraryCollections'] is True


def test_collection_links_only_files_available_in_scoped_library(tmp_path):
    c=Credentials('a'*64,'b'*64);media=tmp_path/'media';(media/'Images').mkdir(parents=True);(media/'Images/face.png').write_bytes(b'image')
    original={'id':'character','sheet_url':'/media/Images/face.png','refs':['/media/Images/face.png','/media/missing.png']}
    app=create_paired_app(state_root=tmp_path/'state',artifact_root=tmp_path/'artifacts',media_root=media,load_rows=lambda:[{'id':'stable-image','url':'/media/Images/face.png','title':'Reference'}],credentials=c,load_collections=lambda:{'characters':[original]})
    with TestClient(app) as client:
        token=client.post('/api/gate',json={'code':c.code,'studio_library':True}).json()['token']
        row=client.get('/api/studio/collections/characters',headers={'Authorization':'Bearer '+token}).json()['records'][0]
        assert row['libraryAssets']==[{'id':'stable-image','kind':'image','title':'Reference'}]
        assert 'libraryAssets' not in original


def test_relationship_gaps_are_scoped_and_do_not_mutate_saved_records(tmp_path):
    c=Credentials('a'*64,'b'*64)
    original={'id':'voice','character_id':'missing-character'}
    collections={'voices':[original], 'relationshipIssues':[
        {'recordId':'voice','field':'character_id','targetId':'missing-character','reason':'target_not_in_preserved_collection'},
        {'recordId':'other','field':'cast','targetId':'other-character','reason':'target_not_in_preserved_collection'}]}
    app=create_paired_app(state_root=tmp_path/'state',artifact_root=tmp_path/'artifacts',media_root=tmp_path/'media',load_rows=lambda:[],credentials=c,load_collections=lambda:collections)
    with TestClient(app) as client:
        token=client.post('/api/gate',json={'code':c.code,'studio_library':True}).json()['token']
        headers={'Authorization':'Bearer '+token}
        row=client.get('/api/studio/collections/voices/voice',headers=headers).json()['record']
        assert row['character_id']=='missing-character'
        assert row['relationshipIssues']==[{'field':'character_id','targetId':'missing-character','reason':'target_not_in_preserved_collection'}]
        assert original=={'id':'voice','character_id':'missing-character'}


def test_storyboard_scene_links_keep_roles_and_order_without_mutation():
    from fastapi import FastAPI
    from media_lab_core.studio_collections import router
    source={'id':'board','beats':[{'title':'First','duration':4,'still_url':'/media/scene%20one.png','clip_url':'/media/clip.mp4'}, {'title':'Second','assembly_source_url':'/media/clip.mp4','still_url':'/media/missing.png'}]}
    assets={'/media/scene one.png':{'id':'still','kind':'image','title':'Still'},'/media/clip.mp4':{'id':'clip','kind':'video','title':'Clip'}}
    app=FastAPI();app.include_router(router(lambda:{'storyboards':[source]},lambda token:token=='allowed',lambda:assets))
    client=TestClient(app)
    assert client.get('/api/studio/collections/storyboards').status_code==401
    row=client.get('/api/studio/collections/storyboards',headers={'Authorization':'Bearer allowed'}).json()['records'][0]
    assert [beat['title'] for beat in row['beats']]==['First','Second']
    assert row['beats'][0]['duration']==4
    assert [(link['field'],link['id']) for link in row['beats'][0]['mediaLinks']]==[('still_url','still'),('clip_url','clip')]
    assert [(link['field'],link['id']) for link in row['beats'][1]['mediaLinks']]==[('assembly_source_url','clip')]
    assert all('mediaLinks' not in beat for beat in source['beats'])
