import json
import os

import pytest
from fastapi.testclient import TestClient
from media_lab_core.studio_cli import initialize,read_credentials,parser,application,main


def test_init_preserves_existing_data_and_credentials_are_private(tmp_path):
    root=tmp_path/'host'
    initialize(root)
    first=read_credentials(root)
    assert first.secret!=first.code
    assert (root/'credentials.json').stat().st_mode&0o777==0o600
    with pytest.raises(FileExistsError):initialize(root)
    assert read_credentials(root)==first
    os.chmod(root/'credentials.json',0o644)
    with pytest.raises(ValueError,match='private'):read_credentials(root)


def test_cli_factory_pairs_without_legacy_routes_or_invented_engines(tmp_path):
    root=tmp_path/'host';initialize(root)
    args=parser().parse_args(['serve',str(root),'--origin','https://studio.example'])
    assert args.bind=='127.0.0.1' and args.port==7864
    app=application(args)
    with TestClient(app) as client:
        token=client.post('/api/gate',json={'code':read_credentials(root).code,'studio_render':True,'studio_device':'a'*32}).json()['token']
        headers={'Authorization':'Bearer '+token}
        assert client.get('/api/studio/engines',headers=headers).json()['engines']==[]
        assert client.post('/api/studio/director',headers=headers,json={'messages':[{'role':'user','content':'Hi'}]}).status_code==503
        assert client.post('/api/chat').status_code==404


def test_invalid_configuration_and_symlink_credentials_refused(tmp_path):
    root=tmp_path/'host';initialize(root)
    with pytest.raises(ValueError,match='together'):
        application(parser().parse_args(['serve',str(root),'--director-model','exact']))
    (root/'library.json').write_text('{}')
    with pytest.raises(ValueError,match='list'):application(parser().parse_args(['serve',str(root)]))
    original=root/'credentials.json';original.rename(root/'original.json');original.symlink_to(root/'original.json')
    with pytest.raises(OSError):read_credentials(root)


def test_command_error_does_not_print_credentials(tmp_path,capsys):
    root=tmp_path/'host';initialize(root)
    raw=(root/'credentials.json').read_text()
    data=json.loads(raw)
    assert main(['init',str(root)])==2
    output=capsys.readouterr().out
    assert data['secret'] not in output and data['code'] not in output


def test_public_cli_dispatch_never_reads_legacy_configuration(tmp_path,monkeypatch):
    from media_lab_core import cli
    def unexpected(*args):raise AssertionError('independent command read legacy state')
    monkeypatch.setattr(cli,'load_config',unexpected)
    assert cli.main(['studio','init',str(tmp_path/'independent')])==0


def test_inspection_is_read_only_and_does_not_claim_runtime_health(tmp_path,capsys,monkeypatch):
    from media_lab_core import studio_cli
    root=tmp_path/'host';initialize(root)
    credentials=read_credentials(root)
    before={str(path.relative_to(root)):path.read_bytes() for path in root.rglob('*') if path.is_file()}
    monkeypatch.setattr(studio_cli,'application',lambda _:pytest.fail('inspection started app'))
    assert main(['inspect',str(root)])==0
    output=capsys.readouterr().out
    value=json.loads(output)
    assert value['credential_storage_verified'] is True
    assert value['running'] is None and value['engines_qualified'] is None
    assert credentials.secret not in output and credentials.code not in output
    assert before=={str(path.relative_to(root)):path.read_bytes() for path in root.rglob('*') if path.is_file()}


def test_inspection_refuses_bad_catalog_and_missing_data_directory(tmp_path,capsys):
    root=tmp_path/'host';initialize(root)
    (root/'library.json').write_text('{}')
    assert main(['inspect',str(root)])==2
    assert json.loads(capsys.readouterr().out)['configuration_valid'] is False
    (root/'library.json').write_text('[]')
    (root/'artifacts').rmdir()
    assert main(['inspect',str(root)])==2
    assert not (root/'artifacts').exists()


def test_integrated_web_keeps_api_auth_and_data_private(tmp_path):
    root=tmp_path/'host';initialize(root)
    web=tmp_path/'web';web.mkdir()
    (web/'index.html').write_text('Studio home')
    (web/'editor.html').write_text('Studio editor')
    (web/'app.js').write_text('/* bundle */')
    args=parser().parse_args(['serve',str(root),'--web-root',str(web)])
    with TestClient(application(args)) as client:
        assert client.get('/').text=='Studio home'
        assert client.get('/editor').text=='Studio editor'
        assert client.get('/editor/').text=='Studio editor'
        assert client.get('/app.js').text=='/* bundle */'
        assert client.get('/manifest.json').json()['vibexStudio']['webInterface'] is True
        assert client.get('/api/not-present').status_code==404
        assert client.get('/credentials.json').status_code==404
        assert client.get('/%2e%2e/host/credentials.json').status_code==404
        assert client.get('/api/studio/engines').status_code==401


def test_integrated_web_refuses_missing_export(tmp_path):
    root=tmp_path/'host';initialize(root)
    with pytest.raises(ValueError,match='index.html'):
        application(parser().parse_args(['serve',str(root),'--web-root',str(tmp_path)]))


def test_import_media_is_visible_idempotent_and_preserves_source(tmp_path):
    from PIL import Image
    from media_lab_core.studio_cli import import_media
    from media_lab_core.studio_library import catalog
    root=tmp_path/'host';initialize(root)
    source=tmp_path/'My scene.png'
    Image.new('RGB',(64,64),'purple').save(source)
    original=source.read_bytes()
    receipt=import_media(root,source,'My scene')
    assert receipt['kind']=='image' and receipt['already_imported'] is False
    assert import_media(root,source)['already_imported'] is True
    rows=json.loads((root/'library.json').read_text())
    assert len(rows)==1
    items=catalog(rows,root/'media')
    assert items[0][0]['title']=='My scene'
    assert items[0][0]['folder']=='Images/Imported'
    assert items[0][1].name.startswith('My scene--import-')
    assert items[0][1].read_bytes()==original==source.read_bytes()
    assert len(list((root/'media').iterdir()))==1
    with TestClient(application(parser().parse_args(['serve',str(root)]))) as client:
        paired=client.post('/api/gate',json={'code':read_credentials(root).code,
            'studio_library':True,'studio_edit':True,'studio_device':'b'*32}).json()
        library_headers={'Authorization':'Bearer '+paired['token']}
        assert client.get('/api/studio/library',headers=library_headers).status_code==200
        created=client.post('/api/studio/editing/projects',headers={
            'Authorization':'Bearer '+paired['editToken'],
            'X-Library-Authorization':'Bearer '+paired['token']},json={
                'requestId':'import-to-editor-test','title':'Imported scene draft','assetIds':[receipt['id']]})
        assert created.status_code==200,created.text
        base='/api/studio/editing/projects/'+created.json()['project']['project_id']
        editing_headers={'Authorization':'Bearer '+paired['editToken']}
        both={**editing_headers,'X-Library-Authorization':'Bearer '+paired['token']}
        assert client.post(base+'/exports/0/library',headers=both).status_code==404
        assert client.post(base+'/export',json={'revision':0},headers=editing_headers).status_code==200
        assert client.post(base+'/exports/0/library',headers=editing_headers).status_code==403
        saved=client.post(base+'/exports/0/library',headers=both)
        assert saved.status_code==200,saved.text
        assert saved.json()['kind']=='video'
        assert client.post(base+'/exports/0/library',headers=both).json()['already_imported'] is True
        assert len(json.loads((root/'library.json').read_text()))==2
        assert any(meta['folder']=='Videos/Edits' for meta,_ in catalog(json.loads((root/'library.json').read_text()),root/'media'))


def test_import_preserves_legacy_flat_paths_and_rejects_symbolic_folders(tmp_path):
    from PIL import Image
    from media_lab_core.studio_cli import import_media
    from urllib.parse import unquote
    root=tmp_path/'host';initialize(root)
    source=tmp_path/'image.png';Image.new('RGB',(8,8),'purple').save(source)
    imported=import_media(root,source,'Original')
    rows=json.loads((root/'library.json').read_text())
    current=root/'media'/unquote(rows[0]['url'][7:]);legacy=root/'media'/(imported['id']+'.png')
    current.rename(legacy);rows[0]['url']='/media/'+legacy.name
    (root/'library.json').write_text(json.dumps(rows))
    assert import_media(root,source,'Another title')['already_imported']
    assert json.loads((root/'library.json').read_text())==rows
    assert legacy.is_file() and not current.exists()
    other=tmp_path/'other';initialize(other)
    outside=tmp_path/'outside';outside.mkdir();(other/'media/Images').symlink_to(outside)
    with pytest.raises(ValueError,match='symbolic'):
        import_media(other,source,'Unsafe destination')
    assert not list(outside.iterdir())
    assert json.loads((other/'library.json').read_text())==[]


def test_import_rejects_invalid_media_without_catalog_changes(tmp_path):
    from media_lab_core.studio_cli import import_media
    root=tmp_path/'host';initialize(root)
    source=tmp_path/'broken.png';source.write_bytes(b'not an image')
    with pytest.raises(ValueError):import_media(root,source)
    assert json.loads((root/'library.json').read_text())==[]
    assert list((root/'media').iterdir())==[]
    with pytest.raises(ValueError):import_media(root,source,' ')
    unknown=tmp_path/'file.txt';unknown.write_text('text')
    with pytest.raises(ValueError):import_media(root,unknown)


def test_upload_requires_scoped_permissions_and_retries_without_duplicates(tmp_path, monkeypatch):
    import io
    from PIL import Image
    root=tmp_path/'host';initialize(root)
    data=io.BytesIO();Image.new('RGB',(64,64),'blue').save(data,format='PNG')
    with TestClient(application(parser().parse_args(['serve',str(root)]))) as client:
        url='/api/studio/library/import?filename=scene.png&title=Uploaded'
        assert client.post(url,content=data.getvalue()).status_code==401
        paired=client.post('/api/gate',json={'code':read_credentials(root).code,
            'studio_library':True,'studio_edit':True,'studio_device':'c'*32}).json()
        headers={'Authorization':'Bearer '+paired['editToken'],'X-Library-Authorization':'Bearer '+paired['token']}
        result=client.post(url,content=data.getvalue(),headers=headers)
        assert result.status_code==200,result.text
        assert result.json()['already_imported'] is False
        assert client.post(url,content=data.getvalue(),headers=headers).json()['already_imported'] is True
        assert client.post(url,content=b'bad',headers=headers).status_code==422
        assert client.post(url,content=b'',headers=headers).status_code==413
        assert client.post('/api/studio/library/import?filename=../scene.png',content=data.getvalue(),headers=headers).status_code==422
        from media_lab_core import studio_import
        monkeypatch.setattr(studio_import,'MAX_BYTES',4)
        assert client.post(url,content=data.getvalue(),headers=headers).status_code==413
        assert client.post(url,content=iter([b'123',b'456']),headers=headers).status_code==413
        assert len(json.loads((root/'library.json').read_text()))==1
        assert not list((root/'state').glob('tmp*'))
