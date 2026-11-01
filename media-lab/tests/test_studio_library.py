from fastapi import FastAPI
from fastapi.testclient import TestClient
from media_lab_core import studio_library as lib

def test_ticket_expiry_scope_and_rotation():
    now = 1788554000
    token = lib.ticket('secret', 'user', 'CODE', now)
    codes = lambda role: 'CODE'
    assert lib.valid_ticket(token, 'secret', codes, now)
    assert not lib.valid_ticket(token, 'other', codes, now)
    assert not lib.valid_ticket(token, 'secret', lambda role: 'ROTATED', now)
    assert not lib.valid_ticket(token, 'secret', codes, now + lib.TOKEN_AGE + 1)
    assert not lib.valid_ticket(token, 'secret', codes, now - 301)
    assert not lib.valid_ticket(token.replace('.user.', '.admin.'), 'secret', codes, now)
    assert not lib.valid_ticket('user.1788554000.cookie-signature', 'secret', codes, now)

def test_catalog_boundaries(tmp_path):
    media = tmp_path / 'media'
    media.mkdir()
    (media / 'a.png').write_bytes(b'png-test-bytes')
    outside = tmp_path / 'private.txt'
    outside.write_text('must not leak')
    (media / 'escape.png').symlink_to(outside)
    rows = [{'id': 'good', 'url': '/media/a.png', 'prompt': 'Badge', 'ts': 123},
            {'id': 'external', 'url': 'https://other/media/a.png'},
            {'id': 'traversal', 'url': '/media/../private.txt'},
            {'id': 'symlink', 'url': '/media/escape.png'},
            {'id': 'missing', 'url': '/media/no.png'},
            {'id': 'queued', 'url': '/media/a.png', 'status': 'queued'},
            {'id': '../bad', 'url': '/media/a.png'}]
    result = lib.catalog(rows, media)
    assert [meta['id'] for meta, _ in result] == ['good']
    assert result[0][0]['createdAt'] == 123000
    assert 'url' not in result[0][0]

def test_router_auth_and_bytes(tmp_path):
    (tmp_path / 'a.png').write_bytes(b'png-test-bytes')
    app = FastAPI()
    app.include_router(lib.router(lambda: [{'id': 'a', 'url': '/media/a.png'}], tmp_path, lambda token: token == 'scoped-ticket'))
    client = TestClient(app)
    assert client.get('/api/studio/library').status_code == 401
    assert client.get('/api/studio/library', cookies={'mlab_access': 'anything'}).status_code == 401
    headers = {'Authorization': 'Bearer scoped-ticket'}
    assert client.get('/api/studio/library', headers=headers).json()['assets'][0]['id'] == 'a'
    assert client.get('/api/studio/library/a/content', headers=headers).content == b'png-test-bytes'
    assert client.get('/api/studio/library/missing/content', headers=headers).status_code == 404
    assert client.post('/api/studio/library', headers=headers).status_code == 405


def test_authenticated_preview_is_small_and_contains_no_source_metadata(tmp_path):
    import io
    from PIL import Image, PngImagePlugin
    source = Image.new('RGB', (1200, 800), 'purple')
    info = PngImagePlugin.PngInfo()
    info.add_text('private-note', 'not part of the preview')
    source.save(tmp_path / 'poster.png', pnginfo=info)
    (tmp_path / 'video.mp4').write_bytes(b'video-fixture')
    rows = [{'id':'video','url':'/media/video.mp4','poster':'/media/poster.png'}]
    app = FastAPI()
    app.include_router(lib.router(lambda:rows,tmp_path,lambda value:value=='ticket'))
    client = TestClient(app)
    assert lib.is_library_path('/api/studio/library/video/preview')
    assert client.get('/api/studio/library/video/preview').status_code==401
    response = client.get('/api/studio/library/video/preview',headers={'Authorization':'Bearer ticket'})
    assert response.status_code==200
    preview=Image.open(io.BytesIO(response.content))
    assert preview.width<=480 and preview.height<=320
    assert 'private-note' not in preview.info
    assert len(response.content)<2*1024*1024
    rows[0]['poster']='https://outside.example/private.png'
    assert client.get('/api/studio/library/video/preview',headers={'Authorization':'Bearer ticket'}).status_code==404


def test_stateless_sprite_export_uses_authorized_png_ids_in_selected_order(tmp_path):
    import base64
    import io
    from PIL import Image
    rows = []
    for name, color in [('red', 'red'), ('blue', 'blue')]:
        Image.new('RGBA', (8, 8), color).save(tmp_path / f'{name}.png')
        rows.append({'id':name,'url':f'/media/{name}.png'})
    app = FastAPI()
    app.include_router(lib.router(lambda:rows,tmp_path,lambda value:value=='ticket'))
    client = TestClient(app)
    body = {'assetIds':['blue','red']}
    headers = {'Authorization':'Bearer ticket'}
    assert lib.is_library_path('/api/studio/library/sprites')
    assert client.post('/api/studio/library/sprites',json=body).status_code==401
    result = client.post('/api/studio/library/sprites',json=body,headers=headers)
    assert result.status_code==200
    data = result.json()
    sheet=Image.open(io.BytesIO(base64.b64decode(data['pngBase64'])))
    for index, color in [(0,(0,0,255,255)),(1,(255,0,0,255))]:
        box=data['metadata']['frames'][f'frame-{index:03d}.png']['frame']
        assert sheet.getpixel((box['x'],box['y']))==color
    assert sorted(p.name for p in tmp_path.iterdir())==['blue.png','red.png']
    for ids, status in [(['missing'],404),(['red','red'],422),([],422)]:
        assert client.post('/api/studio/library/sprites',json={'assetIds':ids},headers=headers).status_code==status


def test_asset_requests_do_not_touch_unrelated_files_and_recheck_selected_file(tmp_path, monkeypatch):
    from pathlib import Path
    from PIL import Image
    Image.new('RGB', (8, 8), 'purple').save(tmp_path / 'selected.png')
    rows = [{'id': str(i), 'url': f'/media/unrelated-{i}.png'} for i in range(3000)]
    rows.append({'id': 'selected', 'url': '/media/selected.png'})
    original = Path.resolve

    def resolve(path, *args, **kwargs):
        assert not path.name.startswith('unrelated-'), 'A single asset request scanned unrelated files'
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, 'resolve', resolve)
    app = FastAPI()
    app.include_router(lib.router(lambda: rows, tmp_path, lambda token: token == 'ticket'))
    client = TestClient(app)
    headers = {'Authorization': 'Bearer ticket'}
    for endpoint in ('content', 'preview'):
        assert client.get(f'/api/studio/library/selected/{endpoint}').status_code == 401
        assert client.get(f'/api/studio/library/selected/{endpoint}', headers=headers).status_code == 200
    assert client.post('/api/studio/library/sprites', json={'assetIds': ['selected']}, headers=headers).status_code == 200
    # No cached path may continue serving a removed file or an escaped replacement.
    (tmp_path / 'selected.png').unlink()
    outside = tmp_path.parent / 'outside.png'
    outside.write_bytes(b'private')
    (tmp_path / 'selected.png').symlink_to(outside)
    for endpoint in ('content', 'preview'):
        assert client.get(f'/api/studio/library/selected/{endpoint}', headers=headers).status_code == 404
