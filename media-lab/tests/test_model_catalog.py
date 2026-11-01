from dataclasses import replace
import pytest
from media_lab_core.model_catalog import ModelOption,resolve_selection

BASE=ModelOption(id='a',name='A',category='image',status='qualified',recommended=False,quality='test',speed='test',summary='fixture',disk_gb=1,memory_floor_gb=1,license_name='MIT',license_url='https://example.com/LICENSE',terms_acceptance_required=False,source_url='https://example.com/source',immutable_revision='pinned-fixture',sha256='a'*64,requires=(),exclusive_group='',hardware=('any',))

@pytest.mark.parametrize('digest',['z'*64,'a'*63,'a'*65,' '*64])
def test_invalid_digest_never_selectable(digest):
    model=replace(BASE,sha256=digest)
    assert not model.selectable
    with pytest.raises(ValueError,match='SHA-256'):resolve_selection({'a':model},['a'])


def test_cycle_is_reported_with_dependency_path():
    a=replace(BASE,requires=('b',));b=replace(BASE,id='b',requires=('a',))
    with pytest.raises(ValueError,match='a -> b -> a'):resolve_selection({'a':a,'b':b},['a'])


def test_shared_dependencies_are_resolved_once():
    a=replace(BASE,requires=('c',));b=replace(BASE,id='b',requires=('c',));c=replace(BASE,id='c',sha256='F'*64)
    assert [m.id for m in resolve_selection({'a':a,'b':b,'c':c},['a','b','a'])]==['a','b','c']
