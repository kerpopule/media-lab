import json
import pytest
from media_lab_core.triposr_compatibility import MANIFEST,validate_runtime


def packages():return list(json.loads(MANIFEST.read_text())['runtime_versions'].items())


def test_reviewed_version_set_is_stable_under_ordering():
    forward=validate_runtime('Linux','aarch64',(3,12,3),packages())
    assert forward==validate_runtime('Linux','aarch64',(3,12,3),list(reversed(packages())))


@pytest.mark.parametrize('system,machine,version',[('Darwin','arm64',(3,12,3)),('Linux','x86_64',(3,12,3)),('Linux','aarch64',(3,13,0))])
def test_unreviewed_platform_refused(system,machine,version):
    with pytest.raises(ValueError,match='requires'):validate_runtime(system,machine,version,packages())


@pytest.mark.parametrize('change',['missing','extra','version','duplicate'])
def test_runtime_drift_refused(change):
    values=packages()
    if change=='missing':values.pop()
    elif change=='extra':values.append(('unreviewed','1'))
    elif change=='version':values[0]=(values[0][0],'wrong')
    else:values.append(values[0])
    with pytest.raises(ValueError):validate_runtime('Linux','aarch64',(3,12,3),values)


def test_reduced_profile_requires_its_exact_package_set_and_explicit_identity():
    from media_lab_core.triposr_compatibility import expected_runtime_receipt
    reduced=[(name,version) for name,version in packages() if name!='torchvision']
    receipt=validate_runtime('Linux','aarch64',(3,12,3),reduced,profile='without-vision-v1')
    assert receipt['profile']=='without-vision-v1'
    assert receipt==expected_runtime_receipt(profile='without-vision-v1')
    assert receipt!=expected_runtime_receipt()
    with pytest.raises(ValueError):validate_runtime('Linux','aarch64',(3,12,3),reduced)
    with pytest.raises(ValueError):validate_runtime('Linux','aarch64',(3,12,3),packages(),profile='without-vision-v1')
    with pytest.raises(ValueError):expected_runtime_receipt(profile='auto')
