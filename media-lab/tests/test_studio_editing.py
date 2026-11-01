from fastapi.testclient import TestClient
from PIL import Image
from media_lab_core import studio_editing, studio_jobs
from media_lab_core.studio_gate import Credentials
from media_lab_core.studio_server import create_paired_app

OWNER = 'a' * 32
OTHER = 'b' * 32
CREDENTIALS = Credentials('1' * 64, '2' * 64)


def test_large_portrait_preview_scales_without_changing_saved_timeline(tmp_path):
    from media_lab_core import cut
    media=tmp_path/'media';media.mkdir()
    Image.new('RGB',(1200,1600),'purple').save(media/'portrait.png')
    headers={'Authorization':'Bearer '+studio_editing.ticket(CREDENTIALS.secret,CREDENTIALS.code,OWNER)}
    with TestClient(create_paired_app(state_root=tmp_path/'state',artifact_root=tmp_path/'artifacts',
            credentials=CREDENTIALS,media_root=media,load_rows=lambda:[{'id':'portrait','url':'/media/portrait.png'}])) as client:
        library=client.post('/api/gate',json={'code':CREDENTIALS.code,'studio_library':True}).json()['token']
        created=client.post('/api/studio/editing/projects',headers={**headers,'X-Library-Authorization':'Bearer '+library},
            json={'requestId':'large-portrait-preview-01','title':'Portrait','assetIds':['portrait']})
        assert created.status_code==200,created.text
        url='/api/studio/editing/projects/'+created.json()['project']['project_id']
        before=client.get(url,headers=headers).json()
        rendered=client.post(url+'/preview',headers=headers,json={'revision':0})
        assert rendered.status_code==200,rendered.text
        output=next((tmp_path/'state/editing').glob('*/cut/projects/*/previews/0/preview.mp4'))
        probe=cut.probe_media(output)
        video=next(stream for stream in probe['streams'] if stream['codec_type']=='video')
        assert (video['width'],video['height'])==(720,960)
        assert client.get(url,headers=headers).json()==before


def test_edit_tickets_are_scoped_expiring_and_bound_to_host_code():
    token = studio_editing.ticket(CREDENTIALS.secret, CREDENTIALS.code, OWNER, now=1800000000)
    assert studio_editing.identity(token, CREDENTIALS.secret, CREDENTIALS.code, now=1800000001) == OWNER
    assert studio_editing.identity(token, CREDENTIALS.secret, '3' * 64, now=1800000001) is None
    assert studio_editing.identity(token, CREDENTIALS.secret, CREDENTIALS.code, now=1900000000) is None
    assert studio_jobs.identity(token, CREDENTIALS.secret, lambda _: CREDENTIALS.code) is None
    assert CREDENTIALS.authorize_editing(studio_jobs.ticket(CREDENTIALS.secret, 'user', CREDENTIALS.code, OWNER)) is None


def test_owned_draft_actual_probe_edit_restart_and_no_source_mutation(tmp_path):
    media = tmp_path / 'media'
    media.mkdir()
    source = media / 'scene.png'
    Image.new('RGB', (64, 48), 'blue').save(source)
    before = source.read_bytes()
    rows = [{'id': 'scene', 'url': '/media/scene.png', 'title': 'Scene'}]
    args = dict(state_root=tmp_path/'state', artifact_root=tmp_path/'artifacts',
                credentials=CREDENTIALS, media_root=media, load_rows=lambda: rows)
    body = {'requestId': 'create-draft-fixture-01', 'title': 'My cut', 'assetIds': ['scene']}
    with TestClient(create_paired_app(**args)) as client:
        normal = client.post('/api/gate', json={'code': CREDENTIALS.code, 'studio_library': True}).json()
        assert 'editToken' not in normal
        assert client.post('/api/gate', json={'code': CREDENTIALS.code, 'studio_edit': True}).status_code == 422
        grant = client.post('/api/gate', json={'code': CREDENTIALS.code, 'studio_edit': True,
                                              'studio_device': OWNER}).json()
        assert grant['editScope'] == 'editing:own'
        editing = {'Authorization': 'Bearer ' + grant['editToken']}
        headers = {**editing, 'X-Library-Authorization': 'Bearer ' + normal['token']}
        assert client.get('/api/studio/editing/projects').status_code == 401
        assert client.get('/api/studio/editing/projects', headers={'Authorization': 'Bearer '+normal['token']}).status_code == 401
        assert client.post('/api/studio/editing/projects', headers=editing, json=body).status_code == 403
        response = client.post('/api/studio/editing/projects', headers=headers, json=body)
        assert response.status_code == 200, response.text
        project = response.json()['project']
        project_id = project['project_id']
        url = '/api/studio/editing/projects/' + project_id
        assert client.post('/api/studio/editing/projects', headers=headers, json=body).json()['project'] == project
        assert client.post('/api/studio/editing/projects', headers=headers, json={**body, 'title':'Changed'}).status_code == 409
        other = {'Authorization': 'Bearer ' + studio_editing.ticket(CREDENTIALS.secret, CREDENTIALS.code, OTHER)}
        assert client.get(url, headers=other).status_code == 404
        assert client.get('/api/studio/editing/projects', headers=other).json() == {'projects': []}
        clip = project['timeline']['tracks'][0]['clips'][0]
        change = {'transactionId': 'trim-draft-fixture-01', 'revision': 0,
                  'commands': [{'id':'trim-1', 'type':'clip.trim', 'payload':{'clip_id':clip['id'], 'trim_in_frames':0, 'trim_out_frames':48}}]}
        edited = client.post(url+'/transactions', headers=editing, json=change)
        assert edited.status_code == 200, edited.text
        assert edited.json()['project']['revision'] == 1
        assert client.post(url+'/transactions', headers=editing, json=change).status_code == 200
        collision = {**change, 'commands': [{'id':'trim-1', 'type':'clip.trim', 'payload':{'clip_id':clip['id'], 'trim_in_frames':0, 'trim_out_frames':24}}]}
        assert client.post(url+'/transactions', headers=editing, json=collision).status_code == 422
        assert client.get(url, headers=editing).json()['project']['revision'] == 1
        assert client.post(url+'/transactions', headers=editing, json={**change, 'transactionId':'trim-draft-fixture-02'}).status_code == 409
        for invalid in (None, [], 'bad'):
            malformed = {**change, 'commands':[{'id':'bad', 'type':'clip.trim', 'payload':invalid}]}
            assert client.post(url+'/transactions', headers=editing, json=malformed).status_code == 422
        for forbidden in ('render.master', 'project.approve'):
            denied = {**change, 'commands':[{'id':'forbidden', 'type':forbidden, 'payload':{}}]}
            assert client.post(url+'/transactions', headers=editing, json=denied).status_code == 422
    with TestClient(create_paired_app(**args)) as client:
        assert client.get(url, headers=editing).json()['project']['revision'] == 1
    assert source.read_bytes() == before


def test_preview_renders_decodes_reuses_and_protects_real_output(tmp_path, monkeypatch):
    import hashlib
    from media_lab_core import cut
    media = tmp_path/'media';media.mkdir()
    Image.new('RGB',(64,48),'navy').save(media/'frame.png')
    args=dict(state_root=tmp_path/'state',artifact_root=tmp_path/'artifacts',credentials=CREDENTIALS,
              media_root=media,load_rows=lambda:[{'id':'frame','url':'/media/frame.png'}])
    headers={'Authorization':'Bearer '+studio_editing.ticket(CREDENTIALS.secret,CREDENTIALS.code,OWNER)}
    other={'Authorization':'Bearer '+studio_editing.ticket(CREDENTIALS.secret,CREDENTIALS.code,OTHER)}
    original=cut.render_timeline;calls=[]
    def counted(*args,**kwargs):calls.append(True);return original(*args,**kwargs)
    monkeypatch.setattr(cut,'render_timeline',counted)
    with TestClient(create_paired_app(**args)) as client:
        library=client.post('/api/gate',json={'code':CREDENTIALS.code,'studio_library':True}).json()['token']
        created=client.post('/api/studio/editing/projects',headers={**headers,'X-Library-Authorization':'Bearer '+library},json={'requestId':'preview-test-create-01','title':'Preview','assetIds':['frame']})
        assert created.status_code==200,created.text
        pid=created.json()['project']['project_id'];url='/api/studio/editing/projects/'+pid
        assert client.post(url+'/preview',json={'revision':0}).status_code==401
        assert client.post(url+'/preview',headers=other,json={'revision':0}).status_code==404
        response=client.post(url+'/preview',headers=headers,json={'revision':0})
        assert response.status_code==200,response.text
        receipt=response.json();assert receipt['candidate'] and receipt['seconds']==4
        assert client.post(url+'/preview',headers=headers,json={'revision':0}).json()==receipt
        assert len(calls)==1
        content=url+'/previews/0/content'
        assert client.get(content).status_code==401
        assert client.get(content,headers=other).status_code==404
        data=client.get(content,headers=headers)
        assert len(data.content)==receipt['bytes'] and hashlib.sha256(data.content).hexdigest()==receipt['sha256']
        assert data.headers['content-type']=='video/mp4'
        assert 'path' not in receipt
        assert client.post(url+'/preview',headers=headers,json={'revision':1}).status_code==409
    with TestClient(create_paired_app(**args)) as client:
        assert client.post(url+'/preview',headers=headers,json={'revision':0}).json()==receipt
        assert len(calls)==1
        output=next((tmp_path/'state/editing').glob('*/cut/projects/*/previews/0/preview.mp4'))
        output.write_bytes(b'incomplete')
        assert client.get(content,headers=headers).status_code==404

        original_load = cut.CutProjectStore.load
        def too_long(store):
            project = original_load(store);project['duration_seconds']=61;return project
        monkeypatch.setattr(cut.CutProjectStore,'load',too_long)
        assert client.post(url+'/preview',headers=headers,json={'revision':0}).status_code==422
        assert len(calls)==1
        def gap(store):
            project=original_load(store);project['timeline']['tracks'][0]['clips'][0]['start_frame']=1;return project
        monkeypatch.setattr(cut.CutProjectStore,'load',gap)
        assert client.post(url+'/preview',headers=headers,json={'revision':0}).status_code==422
        assert len(calls)==1


def test_clip_volume_and_mute_change_actual_rendered_audio(tmp_path):
    import array
    import math
    import subprocess
    media=tmp_path/'media';media.mkdir()
    source=media/'tone.mp4'
    subprocess.run(['ffmpeg','-y','-loglevel','error','-f','lavfi','-i','color=c=navy:s=64x48:r=24:d=1',
                    '-f','lavfi','-i','sine=frequency=440:duration=1','-c:v','libx264','-pix_fmt','yuv420p','-c:a','aac','-shortest',str(source)],check=True)
    before=source.read_bytes()
    args=dict(state_root=tmp_path/'state',artifact_root=tmp_path/'artifacts',credentials=CREDENTIALS,
              media_root=media,load_rows=lambda:[{'id':'tone','url':'/media/tone.mp4'}])
    headers={'Authorization':'Bearer '+studio_editing.ticket(CREDENTIALS.secret,CREDENTIALS.code,OWNER)}
    with TestClient(create_paired_app(**args)) as client:
        library=client.post('/api/gate',json={'code':CREDENTIALS.code,'studio_library':True}).json()['token']
        response=client.post('/api/studio/editing/projects',headers={**headers,'X-Library-Authorization':'Bearer '+library},json={'requestId':'audio-test-create-01','title':'Audio','assetIds':['tone']})
        assert response.status_code==200,response.text
        project=response.json()['project'];clip=project['timeline']['tracks'][0]['clips'][0]
        assert clip['audio']['linked']
        url='/api/studio/editing/projects/'+project['project_id']
        def rms(revision):
            rendered=client.post(url+'/preview',headers=headers,json={'revision':revision})
            assert rendered.status_code==200,rendered.text
            data=client.get(url+f'/previews/{revision}/content',headers=headers).content
            output=tmp_path/f'preview-{revision}.mp4';output.write_bytes(data)
            decoded=subprocess.run(['ffmpeg','-loglevel','error','-i',str(output),'-f','f32le','-ac','1','pipe:1'],check=True,capture_output=True).stdout
            values=array.array('f');values.frombytes(decoded)
            return math.sqrt(sum(value*value for value in values)/len(values))
        baseline=rms(0)
        for revision,gain,muted in [(1,-6,False),(2,-6,True)]:
            change={'transactionId':f'audio-test-change-{revision}','revision':revision-1,'commands':[{'id':'audio','type':'audio.mix','payload':{'target':'clip','clip_id':clip['id'],'gain_db':gain,'muted':muted}}]}
            edited=client.post(url+'/transactions',headers=headers,json=change)
            assert edited.status_code==200,edited.text
            assert client.post(url+'/transactions',headers=headers,json=change).json()==edited.json()
            level=rms(revision)
            if muted:assert level<0.00001
            else:assert 0.45<level/baseline<0.56
    assert source.read_bytes()==before


def test_high_quality_export_is_separate_verified_owned_and_reusable(tmp_path, monkeypatch):
    from media_lab_core import cut
    import hashlib
    media=tmp_path/'media';media.mkdir()
    Image.new('RGB',(1440,810),'navy').save(media/'frame.png')
    args=dict(state_root=tmp_path/'state',artifact_root=tmp_path/'artifacts',credentials=CREDENTIALS,
              media_root=media,load_rows=lambda:[{'id':'frame','url':'/media/frame.png'}])
    headers={'Authorization':'Bearer '+studio_editing.ticket(CREDENTIALS.secret,CREDENTIALS.code,OWNER)}
    other={'Authorization':'Bearer '+studio_editing.ticket(CREDENTIALS.secret,CREDENTIALS.code,OTHER)}
    original=cut.render_timeline;calls=[]
    def counted(*args,**kwargs):
        calls.append(kwargs['export_request']['quality'])
        return original(*args,**kwargs)
    monkeypatch.setattr(cut,'render_timeline',counted)
    with TestClient(create_paired_app(**args)) as client:
        library=client.post('/api/gate',json={'code':CREDENTIALS.code,'studio_library':True}).json()['token']
        created=client.post('/api/studio/editing/projects',headers={**headers,'X-Library-Authorization':'Bearer '+library},json={'requestId':'export-test-create-01','title':'High quality','assetIds':['frame']})
        assert created.status_code==200,created.text
        pid=created.json()['project']['project_id'];url='/api/studio/editing/projects/'+pid
        preview=client.post(url+'/preview',headers=headers,json={'revision':0})
        assert preview.status_code==200,preview.text
        assert client.post(url+'/export',json={'revision':0}).status_code==401
        assert client.post(url+'/export',headers=other,json={'revision':0}).status_code==404
        status_url=url+'/exports/0/status'
        assert client.get(status_url).status_code==401
        assert client.get(status_url,headers=other).status_code==404
        assert client.get(status_url,headers=headers).json()['state']=='not-ready'
        assert calls==['preview']
        rendered=client.post(url+'/export',headers=headers,json={'revision':0})
        assert rendered.status_code==200,rendered.text
        receipt=rendered.json()
        status=client.get(status_url,headers=headers)
        assert status.json()=={'projectId':pid,'revision':0,'state':'ready','receipt':receipt}
        assert status.headers['cache-control']=='no-store'
        assert client.get(url+'/exports/-1/status',headers=headers).status_code==422
        assert receipt['quality']=='high' and receipt['width']==1440 and receipt['height']==810
        assert receipt['seconds']==4 and calls==['preview','high']
        assert client.post(url+'/export',headers=headers,json={'revision':0}).json()==receipt
        content=url+'/exports/0/content'
        assert client.get(content).status_code==401
        assert client.get(content,headers=other).status_code==404
        data=client.get(content,headers=headers)
        assert data.status_code==200 and data.headers['content-disposition'].startswith('attachment;')
        assert hashlib.sha256(data.content).hexdigest()==receipt['sha256']
        preview_content=client.get(url+'/previews/0/content',headers=headers)
        assert preview_content.status_code==200
        assert hashlib.sha256(preview_content.content).hexdigest()==preview.json()['sha256']
        assert preview.json()['sha256']!=receipt['sha256']
    with TestClient(create_paired_app(**args)) as client:
        assert client.post(url+'/export',headers=headers,json={'revision':0}).json()==receipt
        assert calls==['preview','high']
        output=next((tmp_path/'state/editing').glob('*/cut/projects/*/exports/0/export.mp4'))
        assert client.get(status_url,headers=headers).json()['receipt']==receipt
        output.write_bytes(b'corrupt')
        assert client.get(status_url,headers=headers).json()['state']=='not-ready'
        assert calls==['preview','high']
        assert client.get(content,headers=headers).status_code==404
        assert client.post(url+'/export',headers=headers,json={'revision':1}).status_code==409


def test_transition_changes_render_duration_and_can_be_removed(tmp_path):
    media=tmp_path/'media';media.mkdir()
    for name,color in [('first','navy'),('second','purple')]:Image.new('RGB',(128,96),color).save(media/f'{name}.png')
    args=dict(state_root=tmp_path/'state',artifact_root=tmp_path/'artifacts',credentials=CREDENTIALS,
              media_root=media,load_rows=lambda:[{'id':name,'url':f'/media/{name}.png'} for name in ['first','second']])
    headers={'Authorization':'Bearer '+studio_editing.ticket(CREDENTIALS.secret,CREDENTIALS.code,OWNER)}
    with TestClient(create_paired_app(**args)) as client:
        library=client.post('/api/gate',json={'code':CREDENTIALS.code,'studio_library':True}).json()['token']
        created=client.post('/api/studio/editing/projects',headers={**headers,'X-Library-Authorization':'Bearer '+library},json={'requestId':'transition-test-create','title':'Transition','assetIds':['first','second']})
        assert created.status_code==200,created.text
        project=created.json()['project'];clips=project['timeline']['tracks'][0]['clips'];url='/api/studio/editing/projects/'+project['project_id']
        payload={'from_clip_id':clips[0]['id'],'to_clip_id':clips[1]['id'],'kind':'dissolve','duration_frames':12}
        for revision,kind,expected in [(1,'transition.set',7.5),(2,'transition.remove',8)]:
            changed=client.post(url+'/transactions',headers=headers,json={'transactionId':f'transition-change-{revision}','revision':revision-1,'commands':[{'id':'transition','type':kind,'payload':payload}]})
            assert changed.status_code==200,changed.text
            rendered=client.post(url+'/preview',headers=headers,json={'revision':revision})
            assert rendered.status_code==200,rendered.text
            assert abs(rendered.json()['seconds']-expected)<0.1
            assert client.get(url+f'/previews/{revision}/content',headers=headers).status_code==200


def test_adding_library_source_is_scoped_retryable_and_undoable(tmp_path,monkeypatch):
    from media_lab_core import cut
    media=tmp_path/'media';media.mkdir()
    for name in ['first','second']:Image.new('RGB',(64,48),'navy').save(media/f'{name}.png')
    rows=[{'id':n,'url':f'/media/{n}.png'} for n in ['first','second']]
    args=dict(state_root=tmp_path/'state',artifact_root=tmp_path/'artifacts',credentials=CREDENTIALS,media_root=media,load_rows=lambda:rows)
    headers={'Authorization':'Bearer '+studio_editing.ticket(CREDENTIALS.secret,CREDENTIALS.code,OWNER)}
    with TestClient(create_paired_app(**args)) as client:
        library=client.post('/api/gate',json={'code':CREDENTIALS.code,'studio_library':True}).json()['token']
        both={**headers,'X-Library-Authorization':'Bearer '+library}
        project=client.post('/api/studio/editing/projects',headers=both,json={'requestId':'append-test-create-01','title':'Append','assetIds':['first']}).json()['project']
        url='/api/studio/editing/projects/'+project['project_id']
        change={'transactionId':'append-test-change-01','revision':0,'commands':[{'id':'append','type':'clip.add','payload':{'job_id':'second'}}]}
        assert client.post(url+'/transactions',headers=headers,json=change).status_code==403
        result=client.post(url+'/transactions',headers=both,json=change)
        assert result.status_code==200,result.text
        clips=result.json()['project']['timeline']['tracks'][0]['clips']
        assert len(clips)==2 and clips[1]['start_frame']==96
        monkeypatch.setattr(cut,'probe_gallery_file',lambda _: (_ for _ in ()).throw(AssertionError('retry must not reprobe')))
        assert client.post(url+'/transactions',headers=both,json=change).json()==result.json()
        undo=client.post(url+'/transactions',headers=headers,json={'transactionId':'append-test-undo-01','revision':1,'commands':[{'id':'undo','type':'undo','payload':{}}]})
        assert undo.status_code==200 and len(undo.json()['project']['timeline']['tracks'][0]['clips'])==1
        assert (media/'second.png').is_file()


def test_nested_library_folder_keeps_path_and_renders_without_flattening(tmp_path):
    media=tmp_path/'media';folder=media/'Images/Generated';folder.mkdir(parents=True)
    source=folder/'shared-name.png';Image.new('RGB',(64,48),'purple').save(source)
    Image.new('RGB',(64,48),'red').save(media/'shared-name.png')
    rows=[{'id':'preserved-id','url':'/media/Images/Generated/shared-name.png','title':'Preserved'}]
    args=dict(state_root=tmp_path/'state',artifact_root=tmp_path/'artifacts',
              credentials=CREDENTIALS,media_root=media,load_rows=lambda:rows)
    with TestClient(create_paired_app(**args)) as client:
        library=client.post('/api/gate',json={'code':CREDENTIALS.code,'studio_library':True}).json()['token']
        edit=client.post('/api/gate',json={'code':CREDENTIALS.code,'studio_edit':True,'studio_device':OWNER}).json()['editToken']
        headers={'Authorization':'Bearer '+edit,'X-Library-Authorization':'Bearer '+library}
        response=client.post('/api/studio/editing/projects',headers=headers,json={
            'requestId':'nested-folder-draft','title':'Nested','assetIds':['preserved-id']})
        assert response.status_code==200,response.text
        project=response.json()['project']
        assert project['assets'][0]['source']['path']=='/media/Images/Generated/shared-name.png'
        (media/'shared-name.png').unlink()  # Every render check must use the nested source.
        url='/api/studio/editing/projects/'+project['project_id']
        preview=client.post(url+'/preview',headers=headers,json={'revision':0})
        assert preview.status_code==200,preview.text
        assert client.get(url+'/previews/0/content',headers=headers).status_code==200


def test_nested_source_rejects_traversal_and_symlink_escape(tmp_path):
    import pytest
    from media_lab_core import cut
    for path in ('/media/../private.png','/media/a/../../private.png',
                 '/media/a/%2e%2e/private.png','/media/a\\private.png'):
        with pytest.raises(cut.CutError):cut._media_relative(path)
    media=tmp_path/'media';media.mkdir()
    outside=tmp_path/'private.png';outside.write_bytes(b'private')
    (media/'link').symlink_to(tmp_path,target_is_directory=True)
    with pytest.raises(cut.CutError,match='escapes'):
        cut._asset_file(media,{'source':{'path':'/media/link/private.png'}})


def test_async_export_deduplicates_snapshots_and_recovers_after_restart(tmp_path, monkeypatch):
    import threading
    import time
    import json
    from media_lab_core import cut
    media = tmp_path/'media'; media.mkdir()
    Image.new('RGB', (64, 48), 'navy').save(media/'frame.png')
    args = dict(state_root=tmp_path/'state', artifact_root=tmp_path/'artifacts',
                credentials=CREDENTIALS, media_root=media,
                load_rows=lambda: [{'id':'frame', 'url':'/media/frame.png'}])
    headers = {'Authorization':'Bearer '+studio_editing.ticket(CREDENTIALS.secret,CREDENTIALS.code,OWNER)}
    other = {'Authorization':'Bearer '+studio_editing.ticket(CREDENTIALS.secret,CREDENTIALS.code,OTHER)}
    entered, release = threading.Event(), threading.Event()
    original = cut.render_timeline
    revisions = []
    def delayed(project, **kwargs):
        revisions.append(project['revision'])
        entered.set()
        assert release.wait(10)
        return original(project, **kwargs)
    monkeypatch.setattr(cut, 'render_timeline', delayed)
    with TestClient(create_paired_app(**args)) as client:
        library=client.post('/api/gate',json={'code':CREDENTIALS.code,'studio_library':True}).json()['token']
        project=client.post('/api/studio/editing/projects',headers={**headers,'X-Library-Authorization':'Bearer '+library},json={'requestId':'async-export-create-01','title':'Async','assetIds':['frame']}).json()['project']
        url='/api/studio/editing/projects/'+project['project_id']
        try:
            assert client.post(url+'/export-jobs',json={'revision':0}).status_code==401
            assert client.post(url+'/export-jobs',headers=other,json={'revision':0}).status_code==404
            start=client.post(url+'/export-jobs',headers=headers,json={'revision':0})
            assert start.status_code==202 and start.json()['state']=='running'
            assert entered.wait(5)
            assert client.post(url+'/export-jobs',headers=headers,json={'revision':0}).json()['state']=='running'
            assert client.get(url+'/export-jobs/0',headers=other).status_code==404
            assert client.post(url+'/preview',headers=headers,json={'revision':0}).status_code==409
            changed=client.post(url+'/transactions',headers=headers,json={'transactionId':'async-export-change-01','revision':0,'commands':[{'id':'caption','type':'caption.add','payload':{'text':'Next revision','start_frame':0,'end_frame':24}}]})
            assert changed.status_code==200,changed.text
        finally:
            release.set()
        for _ in range(100):
            status=client.get(url+'/export-jobs/0',headers=headers).json()
            if status['state']!='running':break
            time.sleep(.05)
        assert status['state']=='ready',status
        assert revisions==[0]
        assert client.post(url+'/export-jobs',headers=headers,json={'revision':0}).json()['receipt']==status['receipt']
        assert revisions==[0]
    with TestClient(create_paired_app(**args)) as client:
        assert client.get(url+'/export-jobs/0',headers=headers).json()['state']=='ready'
        directory=next((tmp_path/'state/editing').glob('*/cut/projects/*/exports/0'))
        (directory/'export.mp4').write_bytes(b'corrupt')
        (directory/'job.json').write_text(json.dumps({'runtime':'previous-process','state':'running'}))
        assert client.get(url+'/export-jobs/0',headers=headers).json()['state']=='interrupted'
        assert client.post(url+'/export-jobs',headers=headers,json={'revision':0}).status_code==409
        assert revisions==[0]

        def failed(*args, **kwargs):
            raise OSError('private filesystem detail')
        monkeypatch.setattr(cut, 'render_timeline', failed)
        assert client.post(url+'/export-jobs',headers=headers,json={'revision':1}).status_code==202
        for _ in range(100):
            failed_status=client.get(url+'/export-jobs/1',headers=headers).json()
            if failed_status['state']!='running':break
            time.sleep(.01)
        assert failed_status['state']=='failed'
        assert 'private filesystem' not in str(failed_status)
        monkeypatch.setattr(cut, 'render_timeline', original)
        assert client.post(url+'/export-jobs',headers=headers,json={'revision':1}).status_code==202
        for _ in range(100):
            retried=client.get(url+'/export-jobs/1',headers=headers).json()
            if retried['state']!='running':break
            time.sleep(.05)
        assert retried['state']=='ready',retried


def test_music_video_selection_builds_ordered_scenes_and_playable_soundtrack(cut_media,tmp_path):
    import hashlib
    from media_lab_core import cut
    files={'song':cut_media['m'],'scene-one':cut_media['c'],'scene-two':cut_media['a']}
    original={key:hashlib.sha256(path.read_bytes()).hexdigest() for key,path in files.items()}
    rows=[{'id':key,'url':'/media/'+path.name,'title':key} for key,path in files.items()]
    with TestClient(create_paired_app(state_root=tmp_path/'state',artifact_root=tmp_path/'artifacts',credentials=CREDENTIALS,
                                     media_root=cut_media['dir'],load_rows=lambda:rows)) as client:
        grant=client.post('/api/gate',json={'code':CREDENTIALS.code,'studio_library':True,'studio_edit':True,'studio_device':OWNER}).json()
        headers={'Authorization':'Bearer '+grant['editToken'],'X-Library-Authorization':'Bearer '+grant['token']}
        response=client.post('/api/studio/editing/projects',headers=headers,json={
            'requestId':'music-video-ordered-review','title':'Music video review','assetIds':['song','scene-one','scene-two']})
        assert response.status_code==200,response.text
        project=response.json()['project']
        tracks=project['timeline']['tracks']
        visuals=next(t for t in tracks if t['type']=='video')['clips']
        soundtrack=next(t for t in tracks if t['type']=='music')['clips']
        assert [clip['id'] for clip in visuals]==['clip-scene-one-main','clip-scene-two-main']
        assert soundtrack[0]['id']=='clip-song-music' and soundtrack[0]['start_frame']==0
        url='/api/studio/editing/projects/'+project['project_id']
        preview=client.post(url+'/preview',headers=headers,json={'revision':0})
        assert preview.status_code==200,preview.text
        output=next((tmp_path/'state/editing').glob('*/cut/projects/*/previews/0/preview.mp4'))
        probe=cut.probe_media(output)
        assert {stream['codec_type'] for stream in probe['streams']} >= {'video','audio'}
        assert abs(float(probe['format']['duration'])-project['duration_seconds'])<0.25
        assert {key:hashlib.sha256(path.read_bytes()).hexdigest() for key,path in files.items()}==original
