import hashlib
import json
import pytest
from media_lab_core import triposr_integrity as integrity


def fixture_manifest(tmp_path, monkeypatch):
    root = tmp_path / 'runtime'; root.mkdir()
    (root / 'pkg').mkdir(); (root / 'pkg/native.so').write_bytes(b'reviewed')
    raw = json.dumps({'packages':[{'files':[{'path':'pkg/native.so','bytes':8,
        'sha256':hashlib.sha256(b'reviewed').hexdigest()}]}]}).encode()
    manifest = tmp_path / 'manifest.json'; manifest.write_bytes(raw)
    monkeypatch.setattr(integrity, 'MANIFEST', manifest)
    monkeypatch.setattr(integrity, 'MANIFEST_SHA', hashlib.sha256(raw).hexdigest())
    return root, manifest


@pytest.mark.parametrize('change', ['none','same-size','missing','symlink-file','symlink-parent','manifest'])
def test_checks_bytes_and_each_path_component(tmp_path, monkeypatch, change):
    root, manifest = fixture_manifest(tmp_path, monkeypatch)
    target = root / 'pkg/native.so'
    if change == 'same-size': target.write_bytes(b'changed!')
    elif change == 'missing': target.unlink()
    elif change == 'symlink-file':
        other = root / 'other'; target.rename(other); target.symlink_to(other)
    elif change == 'symlink-parent':
        other = root / 'other'; target.parent.rename(other); (root / 'pkg').symlink_to(other)
    elif change == 'manifest': manifest.write_bytes(b'{}')
    if change == 'none': assert integrity.verify_files(root)['verified_rebuilt_files'] == 1
    else:
        with pytest.raises(ValueError): integrity.verify_files(root)


def test_rebuilt_runtime_runs_integrity_check_and_keeps_receipts_distinct(monkeypatch):
    from media_lab_core import triposr_compatibility as compatibility
    monkeypatch.setattr(compatibility.platform, 'system', lambda:'Linux')
    monkeypatch.setattr(compatibility.platform, 'machine', lambda:'aarch64')
    monkeypatch.setattr(compatibility.sys, 'version_info', (3,12,3))
    class Distribution:
        def __init__(self,name,version): self.metadata={'Name':name};self.version=version
    monkeypatch.setattr(compatibility.importlib.metadata, 'distributions', lambda:[Distribution(n,v) for n,v in compatibility.runtime_spec('without-vision-v1')['runtime_versions'].items()])
    calls=[]
    monkeypatch.setattr(integrity,'verify_files',lambda root:calls.append(root))
    rebuilt=compatibility.verify_runtime(profile='rebuilt-rust-v1')
    assert len(calls)==1
    assert rebuilt==compatibility.expected_runtime_receipt(profile='rebuilt-rust-v1')
    assert rebuilt!=compatibility.expected_runtime_receipt(profile='without-vision-v1')
    def reject(root): raise ValueError('Changed file')
    monkeypatch.setattr(integrity,'verify_files',reject)
    with pytest.raises(ValueError,match='Changed file'): compatibility.verify_runtime(profile='rebuilt-rust-v1')
    compatibility.verify_runtime(profile='without-vision-v1')
