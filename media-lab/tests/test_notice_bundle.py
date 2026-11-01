import hashlib
import zipfile
import pytest
from media_lab_core.notice_bundle import build_bundle


def entry(data=b'notice',source='notice.txt',path='licenses/notice.txt'):
    return {'source':source,'path':path,'bytes':len(data),'sha256':hashlib.sha256(data).hexdigest()}


def test_deterministic_and_verified(tmp_path):
    (tmp_path/'notice.txt').write_bytes(b'notice')
    first=build_bundle(tmp_path,[entry()],tmp_path/'a.zip')
    second=build_bundle(tmp_path,[entry()],tmp_path/'b.zip')
    assert first==second
    with zipfile.ZipFile(tmp_path/'a.zip') as z:
        assert z.read('licenses/notice.txt')==b'notice'
        assert 'manifest.json' in z.namelist()
    with pytest.raises(FileExistsError):build_bundle(tmp_path,[entry()],tmp_path/'a.zip')


def test_tamper_refused_without_partial_output(tmp_path):
    (tmp_path/'notice.txt').write_bytes(b'changed')
    with pytest.raises(ValueError):build_bundle(tmp_path,[entry()],tmp_path/'out.zip')
    assert not (tmp_path/'out.zip').exists()
    assert not list(tmp_path.glob('.notice-bundle-*'))


@pytest.mark.parametrize('path',['../escape','/absolute','a//b','a/./b','a\\b','manifest.json'])
def test_unsafe_destination_refused(tmp_path,path):
    with pytest.raises(ValueError):build_bundle(tmp_path,[entry(path=path)],tmp_path/'out.zip')


def test_symlink_and_duplicate_refused(tmp_path):
    (tmp_path/'real').write_bytes(b'notice');(tmp_path/'notice.txt').symlink_to(tmp_path/'real')
    with pytest.raises(ValueError):build_bundle(tmp_path,[entry()],tmp_path/'out.zip')
    with pytest.raises(ValueError):build_bundle(tmp_path,[entry(),entry()],tmp_path/'out.zip')
