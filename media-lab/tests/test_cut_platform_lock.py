import os
import subprocess
import sys
import types
from pathlib import Path
import pytest
from media_lab_core import cut


def test_windows_locks_same_byte_and_releases_after_failure(tmp_path,monkeypatch):
    calls=[]
    with (tmp_path/'lock').open('a+b') as handle:
        def locking(fd,mode,count):calls.append((mode,count,handle.tell()))
        monkeypatch.setitem(sys.modules,'msvcrt',types.SimpleNamespace(locking=locking,LK_LOCK=1,LK_UNLCK=0))
        monkeypatch.setattr(cut,'_WINDOWS',True)
        handle.write(b'123');handle.flush()
        with pytest.raises(ValueError):
            with cut._exclusive_file_lock(handle):
                handle.seek(2)
                raise ValueError('operation failed')
    assert calls==[(1,1,0),(0,1,0)]


def test_windows_acquisition_failure_does_not_enter_or_unlock(tmp_path,monkeypatch):
    calls=[]
    def locking(fd,mode,count):
        calls.append(mode)
        raise OSError('busy')
    monkeypatch.setitem(sys.modules,'msvcrt',types.SimpleNamespace(locking=locking,LK_LOCK=1,LK_UNLCK=0))
    monkeypatch.setattr(cut,'_WINDOWS',True)
    with (tmp_path/'lock').open('a+b') as handle,pytest.raises(OSError):
        with cut._exclusive_file_lock(handle):pytest.fail('entered without lock')
    assert calls==[1]


def test_atomic_unicode_write_without_directory_fsync_on_windows(tmp_path,monkeypatch):
    monkeypatch.setattr(cut,'_WINDOWS',True)
    path=tmp_path/'project.json';cut._atomic_write(path,{'title':'Vídeo 日本語 🎬'})
    import json
    assert json.loads(path.read_text(encoding='utf-8'))['title']=='Vídeo 日本語 🎬'
    assert not list(tmp_path.glob('*.tmp.*'))


def test_actual_processes_serialize_updates(tmp_path):
    counter=tmp_path/'counter';counter.write_text('0')
    program='''
from pathlib import Path
import sys,time
from media_lab_core.cut import _exclusive_file_lock
root=Path(sys.argv[1])
for _ in range(15):
 with (root/'lock').open('a+b') as handle:
  with _exclusive_file_lock(handle):
   value=int((root/'counter').read_text())
   time.sleep(.001)
   (root/'counter').write_text(str(value+1))
'''
    env={**os.environ,'PYTHONPATH':str(Path(__file__).parents[1])}
    children=[subprocess.Popen([sys.executable,'-c',program,str(tmp_path)],env=env) for _ in range(3)]
    try:
        for child in children:assert child.wait(timeout=10)==0
    finally:
        for child in children:
            if child.poll() is None:child.kill();child.wait(timeout=5)
    assert counter.read_text()=='45'


def test_unicode_journal_survives_reopen_undo_and_recovery(tmp_path):
    import json
    storyboard=tmp_path/'story.json'
    storyboard.write_text(json.dumps({'schema':'media_lab.storyboard.v1','project':'unicode-fixture','private_internal_only':True,'candidate_not_final_until_owner_approves':True,'publication_authorized':False,'title':'Vídeo 日本語 🎬',
        'format':{'fps':24,'resolution':'640x360','shot_duration_seconds':2},
        'shots':[{'id':'scene','narration':'Hello','visual':'Fixture'}]},ensure_ascii=False),encoding='utf-8')
    path=tmp_path/'project.json'
    store=cut.CutProjectStore(path,cut.import_storyboard_manifest(storyboard))
    command={'id':'caption','type':'caption.add','payload':{'text':'Olá 日本語 🎬','start_frame':0,'end_frame':24}}
    store.transact([command],actor='agent',transaction_id='unicode-add',expected_revision=0)
    reopened=cut.CutProjectStore(path)
    assert reopened.load()['timeline']['captions']['items'][0]['text']=='Olá 日本語 🎬'
    reopened.transact([{'id':'undo','type':'undo','payload':{}}],actor='agent',transaction_id='unicode-undo',expected_revision=1)
    assert reopened.load()['timeline']['captions']['items']==[]
    path.write_text('{corrupt',encoding='utf-8')
    reopened.recover()
    assert reopened.load()['title']=='Vídeo 日本語 🎬'
    assert reopened.load()['revision']==2
