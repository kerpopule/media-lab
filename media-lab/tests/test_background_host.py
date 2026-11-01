import hashlib
import io
import json
import threading
import importlib.metadata
import sys

import pytest

from media_lab_core import background_host as host


@pytest.fixture
def qualified(tmp_path, monkeypatch):
    monkeypatch.setattr(host.platform, 'system', lambda: 'Darwin')
    monkeypatch.setattr(host.platform, 'machine', lambda: 'arm64')
    monkeypatch.setattr(host, 'verify_files', lambda *args: None)
    monkeypatch.setattr(host, 'verify_installed_runtime', lambda *args: None)
    runtime = tmp_path/'python'
    runtime.write_bytes(b'fake runtime for controller tests')
    arguments = dict(root=tmp_path/'artifacts', runtime=runtime, package=tmp_path, cache=tmp_path/'cache')
    def execute(**kwargs):
        from PIL import Image
        _, mask = host.fixture()
        image = Image.new('RGBA', mask.size, (10,20,30,255))
        image.putalpha(mask)
        output = io.BytesIO()
        image.save(output,format='PNG')
        png = output.getvalue()
        folder = kwargs['root']/kwargs['job_id']
        folder.mkdir(parents=True)
        (folder/'output.png').write_bytes(png)
        digest = hashlib.sha256(png).hexdigest()
        receipt = {'engine':'birefnet-cpu','revision':json.loads(host.MANIFEST.read_text())['revision'],
                   'device':'cpu','dtype':'float32','input_sha256':hashlib.sha256(kwargs['data']).hexdigest(),
                   'output_sha256':digest}
        (folder/'receipt.json').write_text(json.dumps(receipt))
        return {'path':kwargs['job_id']+'/output.png','bytes':len(png),'sha256':digest,'recovered':False}
    path = host.qualify(**arguments,execute=execute)
    return path, arguments


def test_qualification_binds_runtime_host_and_retained_outputs(qualified):
    receipt, arguments = qualified
    assert host.verify_receipt(receipt)['runtime'] == str(arguments['runtime'])
    arguments['runtime'].write_bytes(b'changed runtime')
    with pytest.raises(ValueError,match='changed'):
        host.verify_receipt(receipt)


def test_qualification_does_not_trust_a_json_success_flag(qualified):
    receipt, arguments = qualified
    value = json.loads(receipt.read_text())
    output = arguments['root']/value['runs'][0]['artifact']['path']
    output.write_bytes(b'changed image')
    with pytest.raises(ValueError,match='artifact'):
        host.verify_receipt(receipt)


def test_host_only_advertises_a_live_verified_worker(tmp_path, monkeypatch):
    entered = threading.Event()
    config = {'runtime':str(tmp_path/'python'),'package':str(tmp_path),'cache':str(tmp_path/'cache'),
              'artifact_root':str(tmp_path.resolve())}
    monkeypatch.setattr(host,'verify_receipt',lambda path:config)
    monkeypatch.setattr(host.background_jobs,'run_next',lambda *args,**kwargs:entered.set())
    instance = host.BackgroundHost(lambda:object(),tmp_path,'receipt.json')
    assert instance.engines() == []
    instance.start()
    try:
        assert entered.wait(2)
        worker = instance.thread
        instance.start()
        assert instance.thread is worker
        assert instance.engines()[0]['operation'] == 'remove-background'
    finally:
        instance.stop()
    assert instance.engines() == [] and not instance.thread.is_alive()


def test_invalid_receipt_and_wrong_artifact_root_never_activate(tmp_path, monkeypatch):
    def invalid(path):
        raise ValueError('unqualified')
    monkeypatch.setattr(host,'verify_receipt',invalid)
    instance = host.BackgroundHost(lambda:None,tmp_path,'receipt.json')
    instance.start()
    instance.thread.join(2)
    assert instance.engines() == [] and instance.error
    monkeypatch.setattr(host,'verify_receipt',lambda path:{'artifact_root':str(tmp_path/'different')})
    instance.start()
    instance.thread.join(2)
    assert instance.engines() == []


def test_shutdown_waits_for_healthy_owned_work(tmp_path,monkeypatch):
    entered, release = threading.Event(), threading.Event()
    config = {'runtime':str(tmp_path/'python'),'package':str(tmp_path),'cache':str(tmp_path/'cache'),
              'artifact_root':str(tmp_path.resolve())}
    monkeypatch.setattr(host,'verify_receipt',lambda path:config)
    def execute(*args,**kwargs):
        entered.set()
        assert release.wait(3)
    monkeypatch.setattr(host.background_jobs,'run_next',execute)
    instance = host.BackgroundHost(lambda:None,tmp_path,'receipt.json')
    instance.start()
    assert entered.wait(2)
    stopper = threading.Thread(target=instance.stop)
    stopper.start()
    try:
        assert instance.stop_requested.wait(1)
        assert stopper.is_alive() and instance.engines() == []
    finally:
        release.set()
        stopper.join(2)
    assert not instance.thread.is_alive()


def test_unsupported_platform_is_not_qualified(tmp_path,monkeypatch):
    monkeypatch.setattr(host.platform,'system',lambda:'Windows')
    with pytest.raises(ValueError,match='macOS arm64'):
        host.qualify(root=tmp_path,runtime=tmp_path,package=tmp_path,cache=tmp_path)


def test_runtime_dependency_changes_are_checked_in_the_target_interpreter():
    host.verify_installed_runtime(sys.executable, {'runtime_versions': {'pytest': importlib.metadata.version('pytest')}})
    with pytest.raises(ValueError,match='dependency versions changed'):
        host.verify_installed_runtime(sys.executable, {'runtime_versions': {'pytest': 'unqualified'}})


def test_target_python_abi_must_match_even_when_dependencies_match():
    manifest = {'runtime_versions': {'pytest': importlib.metadata.version('pytest')},
                'python_abi': 'cp00'}
    with pytest.raises(ValueError, match='dependency versions changed'):
        host.verify_installed_runtime(sys.executable, manifest)


def test_linux_uses_cpu_only_pins_and_the_same_verified_model():
    from media_lab_core.birefnet_cpu import platform_manifest, verify_runtime
    linux = json.loads(platform_manifest('Linux', 'aarch64').read_text())
    mac = json.loads(platform_manifest('Darwin', 'arm64').read_text())
    assert linux['runtime_versions']['torch'] == '2.11.0+cpu'
    assert linux['runtime_versions']['torchvision'] == '0.26.0+cpu'
    assert linux['files'] == mac['files'] and linux['weight'] == mac['weight']
    assert linux['python_abi'] == mac['python_abi'] == 'cp312'
    with pytest.raises(ValueError, match='Python ABI'):
        verify_runtime({'python_abi': 'cp00', 'runtime_versions': {}})


def test_host_holds_lifecycle_slot_and_disabled_runtime_stays_off(tmp_path,monkeypatch):
    from media_lab_core.background_lifecycle import lifecycle_slot
    config={'runtime':str(tmp_path/'python'),'package':str(tmp_path),'cache':str(tmp_path/'cache'),
            'artifact_root':str(tmp_path.resolve())}
    entered=threading.Event()
    monkeypatch.setattr(host,'verify_receipt',lambda path:config)
    monkeypatch.setattr(host.background_jobs,'run_next',lambda *a,**k:entered.set())
    instance=host.BackgroundHost(lambda:None,tmp_path,'receipt.json')
    instance.start()
    try:
        assert entered.wait(2)
        with pytest.raises(RuntimeError,match='in use'):
            with lifecycle_slot(tmp_path):pass
    finally:
        instance.stop()
    (tmp_path/'runtime-disabled.json').write_text('{}')
    instance.start();instance.thread.join(2)
    assert instance.engines()==[] and instance.error
