import threading
from concurrent.futures import ThreadPoolExecutor

import pytest
from fastapi.testclient import TestClient
from media_lab_core.director_host import DirectorHost
from media_lab_core.studio_server import create_app
from .test_director_adapter import setup,locked
from media_lab_core.director_transport import DirectorUncertain


def test_host_rejects_calls_outside_lifetime_and_restarts(tmp_path,monkeypatch):
    adapter,transport,path=setup(tmp_path)
    monkeypatch.setattr(transport,'exchange_under_lease',lambda messages:'Reply')
    host=DirectorHost(adapter)
    with pytest.raises(RuntimeError,match='not accepting'):host('owner',[])
    for _ in range(2):
        host.start()
        try:assert host('owner',[])=='Reply'
        finally:host.stop()
        assert not locked(path)
        with pytest.raises(RuntimeError,match='not accepting'):host('owner',[])


def test_shutdown_waits_for_uncertain_runtime_recovery(tmp_path,monkeypatch):
    idle=threading.Event();idle.set()
    adapter,transport,path=setup(tmp_path,idle.is_set)
    def fail(messages):
        idle.clear()
        raise DirectorUncertain('uncertain')
    monkeypatch.setattr(transport,'exchange_under_lease',fail)
    host=DirectorHost(adapter);host.start()
    stopped=threading.Event()
    def stop():host.stop();stopped.set()
    try:
        with pytest.raises(DirectorUncertain):host('owner',[])
        with ThreadPoolExecutor() as pool:
            future=pool.submit(stop)
            try:
                assert not stopped.wait(.1)
                assert locked(path)
                with pytest.raises(RuntimeError,match='not accepting'):host('owner',[])
            finally:idle.set()
            future.result(timeout=5)
        assert stopped.is_set() and not locked(path)
    finally:idle.set();host.stop()


def test_app_lifespan_owns_director_admission(tmp_path,monkeypatch):
    adapter,transport,path=setup(tmp_path)
    monkeypatch.setattr(transport,'exchange_under_lease',lambda messages:'A plan')
    app=create_app(state_root=tmp_path/'state',artifact_root=tmp_path/'artifacts',
                   authorize=lambda token:'a'*32 if token=='paired' else None,director_reply=adapter)
    headers={'Authorization':'Bearer paired'}
    body={'messages':[{'role':'user','content':'Help me plan'}]}
    client=TestClient(app)
    assert client.post('/api/studio/director',headers=headers,json=body).status_code==503
    with client:
        assert client.post('/api/studio/director',headers=headers,json=body).json()['message']=='A plan'
    assert client.post('/api/studio/director',headers=headers,json=body).status_code==503
    assert adapter.state=='ready' and not locked(path)


def test_shutdown_does_not_reuse_idle_observation_from_before_new_request(tmp_path,monkeypatch):
    idle=threading.Event();idle.set()
    entered=threading.Event();release=threading.Event();stopped=threading.Event()
    adapter,transport,path=setup(tmp_path,idle.is_set)
    original=adapter.poll_recovery
    first=[True]
    def delayed_poll():
        if first[0]:
            first[0]=False
            result=original()
            entered.set()
            assert release.wait(5)
            return result
        return original()
    monkeypatch.setattr(adapter,'poll_recovery',delayed_poll)
    def fail(messages):idle.clear();raise DirectorUncertain('uncertain')
    monkeypatch.setattr(transport,'exchange_under_lease',fail)
    host=DirectorHost(adapter);host.start()
    try:
        assert entered.wait(2)
        with pytest.raises(DirectorUncertain):host('owner',[])
        with ThreadPoolExecutor() as pool:
            future=pool.submit(lambda:(host.stop(),stopped.set()))
            try:
                release.set()
                assert not stopped.wait(.2)
                assert locked(path)
            finally:idle.set()
            future.result(timeout=5)
        assert not locked(path)
    finally:release.set();idle.set();host.stop()
