"""Synthetic shutdown acknowledgements; no systemd or Docker daemon used."""
from pathlib import Path
import subprocess
from types import SimpleNamespace
import pytest
from media_lab_core.engine_shutdown import stop_runtime, ShutdownUnverified


def result(text='', rc=0):
    return SimpleNamespace(stdout=text, stderr='', returncode=rc)


def test_stop_command_failure_does_not_claim_release(tmp_path):
    def run(cmd, **kw):
        if 'show' in cmd:
            return result('ActiveState=active\nMainPID=55\nControlGroup=/fixture\n')
        return result(rc=1)
    with pytest.raises(ShutdownUnverified):
        stop_runtime({'kind':'unit', 'unit':'fixture.service'}, run=run, cgroup_root=tmp_path)


def test_inactive_service_with_populated_cgroup_remains_excluded(tmp_path):
    group = tmp_path/'fixture'; group.mkdir()
    (group/'cgroup.events').write_text('populated 1\nfrozen 0\n')
    replies = iter([result('ControlGroup=/fixture\nActiveState=active\nMainPID=55'),
                    result(), result('ControlGroup=\nActiveState=inactive\nMainPID=0')])
    with pytest.raises(ShutdownUnverified, match='populated'):
        stop_runtime({'kind':'unit','unit':'fixture.service'},
                     run=lambda *a,**k:next(replies), cgroup_root=tmp_path)


def test_empty_cgroup_and_inactive_unit_are_acknowledged(tmp_path):
    group = tmp_path/'fixture'; group.mkdir()
    (group/'cgroup.events').write_text('populated 0\nfrozen 0\n')
    replies = iter([result('ControlGroup=/fixture\nActiveState=active\nMainPID=55'),
                    result(), result('ControlGroup=\nActiveState=inactive\nMainPID=0')])
    receipt = stop_runtime({'kind':'unit','unit':'fixture.service'},
                          run=lambda *a,**k:next(replies), cgroup_root=tmp_path)
    assert receipt['stopped'] is True


@pytest.mark.parametrize('listing,rc', [('fixture\n',0), ('',1)])
def test_docker_failure_or_surviving_container_is_not_release(listing, rc):
    replies = iter([result(), result(listing,rc)])
    with pytest.raises(ShutdownUnverified):
        stop_runtime({'kind':'docker','container':'fixture'},run=lambda *a,**k:next(replies))


def test_absent_container_with_successful_inventory_is_acknowledged():
    commands=[]
    def run(cmd, **kw):
        commands.append((cmd,kw))
        return result('other-container\n' if 'ps' in cmd else '')
    assert stop_runtime({'kind':'docker','container':'fixture'},run=run)['stopped']
    assert all(0 < kw['timeout'] <= 45 for _,kw in commands)


def test_timeout_is_unverified():
    def run(cmd, **kw):
        raise subprocess.TimeoutExpired(cmd, kw['timeout'])
    with pytest.raises(ShutdownUnverified):
        stop_runtime({'kind':'docker','container':'fixture'},run=run)
