import hashlib
import json
from pathlib import Path
import zipfile

import pytest
from media_lab_core.project_archive import pack, unpack, FORMAT


def project(tmp_path):
    root = tmp_path/'project';root.mkdir()
    (root/'project.json').write_text('{"id":"keep-original-id","name":"Game"}')
    (root/'chat.json').write_text('[]')
    (root/'files/assets').mkdir(parents=True)
    (root/'files/index.html').write_text('<video src="assets/win.mp4"></video>')
    return root


def test_streaming_large_media_roundtrip_and_exclusive_restore(tmp_path):
    root = project(tmp_path)
    video = root/'files/assets/win.mp4'
    with video.open('wb') as stream:
        for _ in range(36):stream.write(b'x'*1024*1024)
    archive=tmp_path/'project.vibexdir'
    receipt=pack(root, archive)
    assert receipt['bytes']>36*1024*1024 and receipt['files']==4
    restored=tmp_path/'restored'
    assert unpack(archive,restored)['activated'] is False
    for path in root.rglob('*'):
        if path.is_file():
            with path.open('rb') as a,(restored/path.relative_to(root)).open('rb') as b:
                assert hashlib.file_digest(a,'sha256').digest()==hashlib.file_digest(b,'sha256').digest()
    with pytest.raises(ValueError,match='new archive'):pack(root,archive)
    with pytest.raises(ValueError,match='new directory'):unpack(archive,root)
    assert (root/'project.json').is_file()


def test_archive_refuses_links_and_nonportable_paths(tmp_path):
    root=project(tmp_path)
    (root/'files/assets/link').symlink_to(root/'project.json')
    with pytest.raises(ValueError):pack(root,tmp_path/'bad.vibexdir')
    assert not (tmp_path/'bad.vibexdir').exists()
    (root/'files/assets/link').unlink()
    from media_lab_core.project_archive import valid_path
    for path in ['../outside','files/../../outside','/files/absolute','files/C:/x','files//x','files/CON.txt','files/file.','files/file ']:
        with pytest.raises(ValueError):valid_path(path)


def test_restore_rejects_corruption_and_extra_files_without_publishing(tmp_path):
    root=project(tmp_path);good=tmp_path/'good.vibexdir';pack(root,good)
    for mode in ['corrupt','extra','traversal']:
        bad=tmp_path/(mode+'.zip')
        with zipfile.ZipFile(good) as source,zipfile.ZipFile(bad,'w') as target:
            for info in source.infolist():
                content=source.read(info)
                if mode=='corrupt' and info.filename=='project.json':content=b'x'*len(content)
                target.writestr(info.filename,content)
            if mode=='extra':target.writestr('files/unlisted.txt','hidden')
            if mode=='traversal':target.writestr('../outside','bad')
        destination=tmp_path/(mode+'-restore')
        with pytest.raises(ValueError):unpack(bad,destination)
        assert not destination.exists()
        assert not list(tmp_path.glob('.vibex-restore-*'))
    assert not (tmp_path/'outside').exists()


def test_pack_refuses_a_file_changed_during_streaming(tmp_path,monkeypatch):
    from media_lab_core import project_archive
    root=project(tmp_path);before=(root/'project.json').read_bytes()
    original=project_archive.inventory;calls=0
    def changing(path):
        nonlocal calls
        calls+=1
        if calls==2:(root/'project.json').write_bytes(before+b' ')
        return original(path)
    monkeypatch.setattr(project_archive,'inventory',changing)
    with pytest.raises(ValueError,match='changed'):pack(root,tmp_path/'changed.vibexdir')
    assert not (tmp_path/'changed.vibexdir').exists()
    assert not list(tmp_path.glob('.vibex-archive-*'))


def test_restore_rejects_casefold_collisions(tmp_path):
    rows=[{'path':name,'bytes':2,'sha256':hashlib.sha256(b'{}').hexdigest()} for name in ['project.json','files/A.txt','files/a.txt']]
    source=tmp_path/'collision.zip'
    with zipfile.ZipFile(source,'w') as archive:
        archive.writestr('manifest.json',json.dumps({'format':FORMAT,'version':1,'files':rows}))
        for row in rows:archive.writestr(row['path'],b'{}')
    with pytest.raises(ValueError,match='Conflicting'):unpack(source,tmp_path/'restored')
    assert not (tmp_path/'restored').exists()
