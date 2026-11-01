from types import SimpleNamespace
import pytest
from media_lab_core.host_resources import assess_resources,inspect_host_resources

def model(disk=2,memory=4):return SimpleNamespace(id='fixture',disk_gb=disk,memory_floor_gb=memory)

def test_resources_fit_is_distinct_from_qualification():
    result=assess_resources([model()],4*10**9,2*10**9)
    assert result['catalog_resources_fit'] is True
    assert result['hardware_verified'] is False

@pytest.mark.parametrize('models,ram,disk',[([model()],3*10**9,9*10**9),([model()],9*10**9,10**9),([model(0)],9*10**9,9*10**9),([],9*10**9,9*10**9)])
def test_insufficient_or_unknown_resources_block(models,ram,disk):
    assert assess_resources(models,ram,disk)['blocked_reasons']


def test_nonexistent_target_uses_parent_without_creating_directories(tmp_path):
    target=tmp_path/'future'/'runtime'
    result=inspect_host_resources([],target)
    assert result['measured_filesystem_path']==str(tmp_path.resolve())
    assert not target.exists()
    assert result['available_memory_bytes']>=0
