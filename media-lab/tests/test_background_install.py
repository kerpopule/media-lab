import hashlib
import io
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from media_lab_core import background_install as install


def specification(data=b'pinned bytes'):
    return {'url':'https://example.invalid/pinned','bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}


def test_download_is_atomic_verified_and_reusable(tmp_path):
    target = tmp_path/'model.safetensors'
    calls = []
    def fetch(*args, **kwargs):
        calls.append(args)
        return io.BytesIO(b'pinned bytes')
    install.fetch_file(specification(),target,open_url=fetch)
    install.fetch_file(specification(),target,open_url=fetch)
    assert len(calls) == 1 and target.read_bytes() == b'pinned bytes'
    assert list(tmp_path.iterdir()) == [target]


@pytest.mark.parametrize('data',[b'short',b'wrong bytes!',b'too many bytes for the pinned artifact'])
def test_failed_download_never_becomes_loadable(tmp_path,data):
    target = tmp_path/'model.safetensors'
    with pytest.raises(ValueError):
        install.fetch_file(specification(),target,open_url=lambda *args,**kwargs:io.BytesIO(data))
    assert list(tmp_path.iterdir()) == []


def test_existing_corruption_and_symlinks_are_not_overwritten(tmp_path):
    target = tmp_path/'weight'
    target.write_bytes(b'changed')
    with pytest.raises(ValueError,match='changed'):
        install.fetch_file(specification(),target)
    assert target.read_bytes() == b'changed'
    link = tmp_path/'link'
    link.symlink_to(target)
    with pytest.raises(ValueError,match='symbolic'):
        install.fetch_file(specification(),link)


def test_failed_preflight_has_a_stage_receipt_and_never_downloads(tmp_path,monkeypatch):
    import psutil
    monkeypatch.setattr(psutil,'virtual_memory',lambda:SimpleNamespace(available=1))
    monkeypatch.setattr(install.platform,'system',lambda:'Darwin')
    monkeypatch.setattr(install.platform,'machine',lambda:'arm64')
    def unexpected(*args,**kwargs):
        raise AssertionError('must not download')
    monkeypatch.setattr(install,'fetch_file',unexpected)
    root=tmp_path/'install'
    with pytest.raises(ValueError,match='12 GiB'):
        install.install(root=root,artifact_root=tmp_path/'artifacts',python=Path('/python'),uv=Path('/uv'))
    receipt=json.loads((root/'install.json').read_text())
    assert receipt['stage']=='preflight' and receipt['status']=='failed'
    assert not (root/'package').exists()


def test_unrelated_directory_is_never_adopted(tmp_path):
    root=tmp_path/'install';root.mkdir()
    (root/'keep.txt').write_text('user data')
    with pytest.raises(ValueError,match='empty install'):
        install.install(root=root,artifact_root=tmp_path/'artifacts',python=Path('/python'),uv=Path('/uv'))
    assert (root/'keep.txt').read_text()=='user data'
    assert not (root/'install.json').exists()


def test_transfer_exception_cleans_unpublished_partial_file(tmp_path):
    class Interrupted(io.BytesIO):
        def read(self, size=-1):
            raise ConnectionError('disconnected')
    with pytest.raises(ConnectionError):
        install.fetch_file(specification(),tmp_path/'weight',open_url=lambda *a,**k:Interrupted())
    assert list(tmp_path.iterdir()) == []


def test_active_worker_blocks_runtime_mutation(tmp_path,monkeypatch):
    import psutil
    from media_lab_core.cpu_worker import cpu_slot, WorkerBusy
    monkeypatch.setattr(psutil,'virtual_memory',lambda:SimpleNamespace(available=32*1024**3))
    monkeypatch.setattr(install.shutil,'disk_usage',lambda root:SimpleNamespace(free=32*1024**3))
    monkeypatch.setattr(install,'fetch_file',lambda *a,**k:None)
    monkeypatch.setattr(install,'verify_files',lambda *a,**k:None)
    def run(command, **kwargs):
        assert command[1] == '-I', 'runtime installation must not execute while a worker owns its slot'
        return SimpleNamespace(stdout='cp312\n')
    monkeypatch.setattr(install.subprocess,'run',run)
    artifacts=tmp_path/'artifacts'
    with cpu_slot(artifacts):
        with pytest.raises(WorkerBusy):
            install.install(root=tmp_path/'install',artifact_root=artifacts,python=Path('/python'),uv=Path('/uv'))
    receipt=json.loads((tmp_path/'install/install.json').read_text())
    assert receipt['stage']=='runtime' and receipt['status']=='failed'


def test_resume_after_final_receipt_failure_does_not_repeat_runtime_or_tracers(tmp_path,monkeypatch):
    import psutil
    monkeypatch.setattr(psutil,'virtual_memory',lambda:SimpleNamespace(available=32*1024**3))
    monkeypatch.setattr(install.shutil,'disk_usage',lambda root:SimpleNamespace(free=32*1024**3))
    monkeypatch.setattr(install.platform,'system',lambda:'Darwin')
    monkeypatch.setattr(install.platform,'machine',lambda:'arm64')
    monkeypatch.setattr(install,'fetch_file',lambda *a,**k:None)
    monkeypatch.setattr(install,'verify_files',lambda *a,**k:None)
    monkeypatch.setattr(install,'verify_installed_runtime',lambda *a,**k:None)
    root=tmp_path/'install';artifacts=tmp_path/'artifacts'
    commands=[];tracers=[]
    def run(command,**kwargs):
        commands.append(command)
        if '--output' in command:
            Path(command[command.index('--output')+1]).write_text('{}')
        if command[1] != '-I':
            assert len(kwargs['pass_fds']) == 3
            (root/'runtime/bin').mkdir(parents=True,exist_ok=True)
            (root/'runtime/bin/python').write_text('fixture')
        return SimpleNamespace(stdout='cp312\n')
    monkeypatch.setattr(install.subprocess,'run',run)
    def qualify(**kwargs):
        tracers.append(kwargs)
        p=artifacts/'qualification.json';p.write_text('{}');return p
    monkeypatch.setattr(install,'qualify',qualify)
    monkeypatch.setattr(install,'verify_receipt',lambda path:{'runtime':str(root/'runtime/bin/python'),
        'package':str(root/'package'),'cache':str(root/'model-cache'),'artifact_root':str(artifacts)})
    write=install.write_receipt
    failed=False
    def flaky(path,value):
        nonlocal failed
        if value.get('status')=='qualified' and not failed:
            failed=True;raise OSError('simulated disk write failure')
        write(path,value)
    monkeypatch.setattr(install,'write_receipt',flaky)
    args=dict(root=root,artifact_root=artifacts,python=Path('/python'),uv=Path('/uv'))
    with pytest.raises(OSError):
        install.install(**args)
    assert json.loads((root/'install.json').read_text())['status']=='failed'
    before=len(commands)
    assert install.install(**args)['status']=='qualified'
    assert len(commands)==before and len(tracers)==1
    (artifacts/'qualification.json').unlink()
    with pytest.raises(ValueError,match='evidence is missing'):
        install.install(**args)
    assert len(commands)==before


def test_reinstall_retains_old_outputs_and_stays_disabled_after_failure(tmp_path,monkeypatch):
    import psutil
    monkeypatch.setattr(psutil,'virtual_memory',lambda:SimpleNamespace(available=1))
    root=tmp_path/'install';artifacts=tmp_path/'artifacts'
    args=dict(root=root,artifact_root=artifacts,python=Path('/python'),uv=Path('/uv'))
    with pytest.raises(ValueError,match='12 GiB'):install.install(**args)
    receipt=json.loads((root/'install.json').read_text());receipt['status']='removed'
    (root/'install.json').write_text(json.dumps(receipt))
    (artifacts/'runtime-disabled.json').write_text(json.dumps({'installation':str(root)}))
    (artifacts/'qualification.json').write_text('{"old":"evidence"}')
    (artifacts/'user-image.png').write_bytes(b'preserved creation')
    with pytest.raises(ValueError,match='--reinstall'):install.install(**args)
    with pytest.raises(ValueError,match='12 GiB'):install.install(**args,reinstall=True)
    assert (artifacts/'runtime-disabled.json').exists()
    assert (artifacts/'user-image.png').read_bytes()==b'preserved creation'
    assert len(list(artifacts.glob('qualification-retired-*.json')))==1
    assert not (artifacts/'qualification.json').exists()
    with pytest.raises(ValueError,match='12 GiB'):install.install(**args,reinstall=True)
    assert len(list(artifacts.glob('qualification-retired-*.json')))==1


def test_reinstall_cannot_adopt_another_installations_disabled_root(tmp_path):
    artifacts=tmp_path/'artifacts';artifacts.mkdir()
    (artifacts/'runtime-disabled.json').write_text(json.dumps({'installation':str(tmp_path/'other')}))
    with pytest.raises(ValueError,match='different installation'):
        install.install(root=tmp_path/'install',artifact_root=artifacts,python=Path('/python'),uv=Path('/uv'),reinstall=True)
    assert not (tmp_path/'install').exists()
