import threading
from types import SimpleNamespace
import pytest
from fastapi.testclient import TestClient
from media_lab_core import background_setup as setup
from .test_cut_api import media_app


def test_real_routes_require_admin_session_and_host_opt_in(media_app,monkeypatch):
    client=TestClient(media_app.app,base_url='https://setup-test.invalid')
    url='/api/setup/background/plan'
    assert client.get(url).status_code==401
    assert client.post('/api/gate',json={'code':media_app.ACCESS_CODE}).status_code==200
    assert client.get(url).status_code==403
    assert client.post('/api/setup/background/activation',json={'planId':'a'*64,'enabled':True}).status_code==403
    assert client.post('/api/setup/background/install',json={'planId':'a'*64}).status_code==403
    client.cookies.clear()
    paired=client.post('/api/gate',json={'code':media_app.ADMIN_CODE,'studio_render':True,'studio_device':'a'*32}).json()
    assert client.get(url,headers={'Authorization':'Bearer '+paired['token']}).status_code==401
    assert client.post('/api/gate',json={'code':media_app.ADMIN_CODE}).status_code==200
    monkeypatch.delenv('MEDIA_LAB_BACKGROUND_SETUP',raising=False)
    assert client.get(url).status_code==409
    monkeypatch.setenv('MEDIA_LAB_BACKGROUND_SETUP','1')
    # Inspect the real deterministic plan only after explicit admin login and opt-in.
    plan=client.get(url)
    assert plan.status_code==200 and plan.json()['developmentOnly']
    assert plan.headers['Cache-Control']=='private, no-store'
    assert client.post('/api/setup/background/install',json={'planId':'a'*64,'root':'/arbitrary'}).status_code==422


def test_plan_binding_prevents_changed_tools_and_duplicate_running_actions(tmp_path,monkeypatch):
    import psutil
    monkeypatch.setattr(psutil,'virtual_memory',lambda:SimpleNamespace(available=32*1024**3,total=64*1024**3))
    monkeypatch.setattr(setup.shutil,'disk_usage',lambda path:SimpleNamespace(free=32*1024**3))
    monkeypatch.setattr(setup.platform,'system',lambda:'Darwin')
    monkeypatch.setattr(setup.platform,'machine',lambda:'arm64')
    monkeypatch.setattr(setup,'python_abi',lambda path:'cp312')
    python=tmp_path/'python';python.write_bytes(b'fixture interpreter')
    uv=tmp_path/'uv';uv.write_bytes(b'fixture installer')
    entered=threading.Event();release=threading.Event();calls=[]
    def execute(**kwargs):
        calls.append(kwargs);entered.set();assert release.wait(3)
    controller=setup.Setup(tmp_path/'app',python=python,uv=uv,execute=execute)
    plan=controller.plan()
    assert plan['availableMemoryBytes']==32*1024**3
    assert plan['totalMemoryBytes']==64*1024**3
    uv.write_bytes(b'changed tool')
    with pytest.raises(ValueError,match='plan changed'):controller.start(plan['planId'])
    plan=controller.plan()
    assert controller.start(plan['planId'])['accepted']
    try:
        assert entered.wait(2)
        assert controller.status()['runningHere']
        with pytest.raises(ValueError,match='already running'):controller.start(plan['planId'])
    finally:
        release.set();controller.thread.join(2)
    assert not controller.status()['runningHere']
    assert len(calls)==1 and calls[0]['root'].is_relative_to(tmp_path/'app/studio-runtimes')
    assert calls[0]['artifact_root']==tmp_path/'app/studio-artifacts'


def test_plan_blocks_unverified_python_before_dispatch(tmp_path, monkeypatch):
    import sys
    calls=[]
    controller=setup.Setup(tmp_path,python=sys.executable,execute=lambda **kw:calls.append(kw))
    for abi in ['cp311', None]:
        monkeypatch.setattr(setup,'python_abi',lambda path:abi)
        plan=controller.plan()
        assert plan['pythonAbi']==abi
        assert any('configured Python' in reason for reason in plan['blockedReasons'])
        with pytest.raises(ValueError,match='Python'):
            controller.start(plan['planId'])
    assert not calls


def test_interpreter_probe_isolated_and_reports_actual_abi():
    import sys
    assert setup.python_abi(sys.executable)==f'cp{sys.version_info.major}{sys.version_info.minor}'
    assert setup.python_abi('/nonexistent/vibex-python') is None


def test_install_route_only_dispatches_exact_bounded_action():
    from fastapi import FastAPI
    calls=[]
    class Controller:
        def start(self,plan_id,reinstall):
            calls.append((plan_id,reinstall));return {'accepted':True}
    app=FastAPI()
    app.include_router(setup.router(lambda:Controller(),lambda r:r.headers.get('x-test-admin')=='yes',lambda:True))
    client=TestClient(app)
    path='/api/setup/background/install'
    assert client.post(path,json={'planId':'a'*64}).status_code==403
    headers={'x-test-admin':'yes'}
    assert client.post(path,headers=headers,json={'planId':'a'*64,'python':'/arbitrary'}).status_code==422
    assert calls==[]
    assert client.post(path,headers=headers,json={'planId':'a'*64,'reinstall':True}).status_code==202
    assert calls==[('a'*64,True)]


def test_enable_verifies_managed_paths_and_persists_only_a_fixed_receipt(tmp_path,monkeypatch):
    import json
    controller=setup.Setup(tmp_path)
    root=controller.installation_root();root.mkdir(parents=True)
    (root/'runtime-inventory.json').write_text('{}')
    (root/'install.json').write_text(json.dumps({'status':'qualified','inventory_sha256':setup.digest(root/'runtime-inventory.json')}))
    artifacts=tmp_path/'studio-artifacts'
    config={'runtime':str(root/'runtime/bin/python'),'package':str(root/'package'),
            'cache':str(root/'model-cache'),'artifact_root':str(artifacts)}
    monkeypatch.setattr(setup,'verify_receipt',lambda p:config)
    host=SimpleNamespace(thread=None,receipt=None,start=lambda:None)
    controller._activation(host,True)
    assert setup.managed_receipt(tmp_path)==str(artifacts/'qualification.json')
    assert host.receipt==setup.managed_receipt(tmp_path)
    controller._activation(host,False)
    assert setup.managed_receipt(tmp_path) is None and host.receipt is None
    config['runtime']='/another/runtime'
    with pytest.raises(ValueError,match='does not match'):
        controller._activation(host,True)
    assert setup.managed_receipt(tmp_path) is None


def test_disable_persists_before_waiting_for_healthy_work(tmp_path):
    import json
    controller=setup.Setup(tmp_path)
    alive=True
    def stop():
        nonlocal alive
        assert json.loads((tmp_path/'studio-background-enabled.json').read_text())['enabled'] is False
        alive=False
    host=SimpleNamespace(thread=SimpleNamespace(is_alive=lambda:alive),receipt='existing',stop=stop)
    controller._activation(host,False)
    assert not alive and host.receipt is None


def test_untrusted_or_malformed_activation_preferences_never_choose_paths(tmp_path):
    import json
    file=tmp_path/'studio-background-enabled.json'
    file.write_text(json.dumps({'version':1,'enabled':True,'receipt':'/private/arbitrary'}))
    assert setup.managed_receipt(tmp_path)==str(tmp_path/'studio-artifacts/qualification.json')
    file.write_text('[]');assert setup.managed_receipt(tmp_path) is None
    file.unlink();file.symlink_to(tmp_path/'other')
    assert setup.managed_receipt(tmp_path) is None


def test_activation_refuses_server_overrides_and_stale_plans(tmp_path,monkeypatch):
    controller=setup.Setup(tmp_path,get_host=lambda:None,externally_controlled=lambda:True)
    with pytest.raises(ValueError,match='server configuration'):
        controller.activation('a'*64,True)
    controller.externally_controlled=lambda:False
    monkeypatch.setattr(controller,'plan',lambda:{'planId':'b'*64})
    with pytest.raises(ValueError,match='plan changed'):
        controller.activation('a'*64,False)
    assert controller.thread is None


def test_activation_cannot_reverse_a_still_draining_host(tmp_path):
    controller=setup.Setup(tmp_path)
    event=threading.Event();event.set()
    host=SimpleNamespace(thread=SimpleNamespace(is_alive=lambda:True),stop_requested=event)
    with pytest.raises(ValueError,match='finish disabling'):
        controller._activation(host,True)
    assert setup.managed_receipt(tmp_path) is None


def test_removal_requires_disabled_host_and_uses_only_managed_root(tmp_path,monkeypatch):
    host=SimpleNamespace(thread=SimpleNamespace(is_alive=lambda:True))
    controller=setup.Setup(tmp_path,get_host=lambda:host)
    monkeypatch.setattr(controller,'plan',lambda:{'planId':'a'*64})
    calls=[]
    monkeypatch.setattr(setup,'remove',lambda root,execute:calls.append((root,execute)))
    with pytest.raises(ValueError,match='Disable the model'):
        controller.remove('a'*64)
    host.thread=None
    setup.write_receipt(tmp_path/'studio-background-enabled.json',{'version':1,'enabled':True})
    with pytest.raises(ValueError,match='Disable the model'):
        controller.remove('a'*64)
    setup.write_receipt(tmp_path/'studio-background-enabled.json',{'version':1,'enabled':False})
    with pytest.raises(ValueError,match='plan changed'):
        controller.remove('b'*64)
    assert calls==[]
    assert controller.remove('a'*64)['accepted']
    controller.thread.join(2)
    assert calls==[(controller.installation_root(),True)]


def test_removal_route_rejects_nonadmin_and_arbitrary_scope():
    from fastapi import FastAPI
    calls=[]
    controller=SimpleNamespace(remove=lambda plan_id:calls.append(plan_id) or {'accepted':True})
    app=FastAPI()
    app.include_router(setup.router(lambda:controller,lambda r:r.headers.get('x-test-admin')=='yes',lambda:True))
    client=TestClient(app);path='/api/setup/background/remove'
    assert client.post(path,json={'planId':'a'*64}).status_code==403
    headers={'x-test-admin':'yes'}
    assert client.post(path,headers=headers,json={'planId':'a'*64,'root':'/arbitrary'}).status_code==422
    assert calls==[]
    assert client.post(path,headers=headers,json={'planId':'a'*64}).status_code==202
    assert calls==['a'*64]


def test_memory_snapshot_reports_capacity_and_refuses_insufficient_available_ram(tmp_path, monkeypatch):
    import psutil
    monkeypatch.setattr(psutil, 'virtual_memory', lambda: SimpleNamespace(total=64*1024**3, available=10*1024**3))
    monkeypatch.setattr(setup.shutil, 'disk_usage', lambda path: SimpleNamespace(free=32*1024**3))
    calls=[]
    controller=setup.Setup(tmp_path, execute=lambda **kwargs: calls.append(kwargs))
    plan=controller.plan()
    assert plan['totalMemoryBytes']==64*1024**3
    assert plan['availableMemoryBytes']==10*1024**3
    assert plan['memoryRequiredBytes']==12*1024**3
    assert 'Free 12 GiB of memory before qualification.' in plan['blockedReasons']
    with pytest.raises(ValueError, match='12 GiB'):
        controller.start(plan['planId'])
    assert calls==[]
