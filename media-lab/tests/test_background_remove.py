import json
from pathlib import Path
import pytest
from media_lab_core import background_remove as removal
from media_lab_core.background_lifecycle import lifecycle_slot
from media_lab_core.cpu_worker import cpu_slot, WorkerBusy


@pytest.fixture
def installed(tmp_path,monkeypatch):
    root=tmp_path/'install';root.mkdir()
    artifacts=tmp_path/'artifacts';artifacts.mkdir()
    (artifacts/'generated.png').write_bytes(b'preserve user creation')
    (root/'.install.lock').touch()
    (root/'install.json').write_text(json.dumps({'identity':{'artifact_root':str(artifacts)},'status':'qualified'}))
    for name in removal.MANAGED:
        (root/name).mkdir();(root/name/'fixture').write_text('managed fixture')
    (root/'keep.log').write_text('installation log')
    monkeypatch.setattr(removal,'verify_receipt',lambda p:{'runtime':str(root/'runtime/bin/python'),
        'package':str(root/'package'),'cache':str(root/'model-cache'),'artifact_root':str(artifacts)})
    return root,artifacts


def test_plan_then_remove_preserves_artifacts_and_replays(installed):
    root,artifacts=installed
    assert removal.remove(root)['status']=='planned'
    assert (root/'package/fixture').exists()
    assert not (artifacts/'runtime-disabled.json').exists()
    assert removal.remove(root,execute=True)['status']=='removed'
    assert all(not (root/name).exists() for name in removal.MANAGED)
    assert (artifacts/'generated.png').read_bytes()==b'preserve user creation'
    assert (root/'keep.log').read_text()=='installation log'
    assert removal.remove(root,execute=True)['status']=='removed'


def test_active_host_or_orphan_job_prevents_removal(installed):
    root,artifacts=installed
    with lifecycle_slot(artifacts):
        with pytest.raises(RuntimeError,match='in use'):
            removal.remove(root,execute=True)
    with cpu_slot(artifacts):
        with pytest.raises(WorkerBusy):
            removal.remove(root,execute=True)
    assert (root/'runtime/fixture').exists()
    assert not (artifacts/'runtime-disabled.json').exists()


def test_interrupted_removal_resumes_without_requiring_deleted_model(installed,monkeypatch):
    root,artifacts=installed
    real=removal.shutil.rmtree
    count=0
    def interrupted(path):
        nonlocal count
        count+=1
        if count==2:raise OSError('simulated interruption')
        real(path)
    monkeypatch.setattr(removal.shutil,'rmtree',interrupted)
    with pytest.raises(OSError):removal.remove(root,execute=True)
    assert json.loads((root/'install.json').read_text())['status']=='removing'
    monkeypatch.setattr(removal,'verify_receipt',lambda p:pytest.fail('must use owned removal journal after deletion began'))
    assert removal.remove(root,execute=True)['status']=='removed'
    assert (artifacts/'generated.png').exists()
