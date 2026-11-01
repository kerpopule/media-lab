import fcntl
import os

import pytest
from media_lab_core.director_adapter import LocalDirectorAdapter
from media_lab_core.director_transport import LocalDirectorTransport, DirectorUncertain


def locked(path):
    fd=os.open(path,os.O_RDWR)
    try:
        try:fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:return True
        return False
    finally:os.close(fd)


def setup(tmp_path,idle=lambda:True):
    transport=LocalDirectorTransport(port=8004,model='exact-test-model')
    path=tmp_path.resolve()/'inference.lock'
    return LocalDirectorAdapter(transport=transport,lease_path=path,runtime_idle=idle),transport,path


def test_owns_canonical_lock_through_exchange_and_releases_on_success(tmp_path,monkeypatch):
    adapter,transport,path=setup(tmp_path)
    def exchange(messages):
        assert locked(path) and adapter.state=='running'
        assert adapter.poll_recovery() is False
        with pytest.raises(RuntimeError,match='busy'):adapter('other',messages)
        return 'Reply'
    monkeypatch.setattr(transport,'exchange_under_lease',exchange)
    assert adapter('owner',[])=='Reply'
    assert not locked(path) and adapter.state=='ready'


def test_other_owner_and_busy_runtime_never_submit(tmp_path,monkeypatch):
    adapter,transport,path=setup(tmp_path,lambda:False)
    calls=[]
    monkeypatch.setattr(transport,'exchange_under_lease',lambda messages:calls.append(messages))
    with pytest.raises(RuntimeError,match='not confirmed idle'):adapter('owner',[])
    assert not calls and not locked(path)
    fd=os.open(path,os.O_RDWR)
    try:
        fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
        with pytest.raises(BlockingIOError):adapter('owner',[])
        assert not calls
    finally:os.close(fd)


def test_uncertainty_retains_lock_until_stable_idle_and_rejects_new_work(tmp_path,monkeypatch):
    activity={'idle':True,'now':0}
    adapter,transport,path=setup(tmp_path,lambda:activity['idle'])
    monkeypatch.setattr('media_lab_core.director_adapter.time.monotonic',lambda:activity['now'])
    def exchange(messages):raise DirectorUncertain('uncertain')
    monkeypatch.setattr(transport,'exchange_under_lease',exchange)
    try:
        with pytest.raises(DirectorUncertain):adapter('owner',[])
        assert adapter.state=='draining' and locked(path)
        with pytest.raises(RuntimeError,match='previous'):adapter('owner',[])
        assert not adapter.poll_recovery()
        activity['now']=1
        assert not adapter.poll_recovery()
        activity['idle']=False
        activity['now']=2
        assert not adapter.poll_recovery() and locked(path)
        activity['idle']=True
        assert not adapter.poll_recovery()
        activity['now']=4
        assert adapter.poll_recovery() and not locked(path)
        assert adapter.state=='ready'
    finally:
        activity['idle']=True
        adapter.poll_recovery()
        activity['now']+=3
        adapter.poll_recovery()


def test_noncanonical_path_rejected_without_creating_lock(tmp_path):
    alias=tmp_path/'alias'
    alias.symlink_to(tmp_path,target_is_directory=True)
    with pytest.raises(ValueError):LocalDirectorAdapter(transport=LocalDirectorTransport(port=8004,model='exact'),lease_path=alias/'lock',runtime_idle=lambda:True)
    assert not (tmp_path/'lock').exists()


def test_probe_failure_never_releases_uncertain_work(tmp_path,monkeypatch):
    state={'fail':False,'now':0}
    def idle():
        if state['fail']:raise OSError('probe offline')
        return True
    adapter,transport,path=setup(tmp_path,idle)
    monkeypatch.setattr('media_lab_core.director_adapter.time.monotonic',lambda:state['now'])
    def exchange(messages):raise DirectorUncertain('uncertain')
    monkeypatch.setattr(transport,'exchange_under_lease',exchange)
    try:
        with pytest.raises(DirectorUncertain):adapter('owner',[])
        assert not adapter.poll_recovery()
        state.update(fail=True,now=3)
        assert not adapter.poll_recovery() and locked(path)
        state['fail']=False
        assert not adapter.poll_recovery() and locked(path)
        state['now']=6
        assert adapter.poll_recovery() and not locked(path)
    finally:
        state['fail']=False
        adapter.poll_recovery()
        state['now']+=3
        adapter.poll_recovery()
