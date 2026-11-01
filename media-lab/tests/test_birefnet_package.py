import hashlib
import importlib.metadata

import pytest

from media_lab_core.birefnet_cpu import verify_files, verify_runtime


def test_pinned_files_reject_corruption_and_escape_before_model_loading(tmp_path):
    package = tmp_path / 'package'
    package.mkdir()
    data = b'synthetic weight fixture'
    path = package / 'model.safetensors'
    path.write_bytes(data)
    record = {'file': path.name, 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest()}
    manifest = {'files': [], 'weight': record}
    verify_files(package, manifest)
    path.write_bytes(b'x' * len(data))
    with pytest.raises(ValueError, match='SHA-256'):
        verify_files(package, manifest)
    path.write_bytes(b'x')
    with pytest.raises(ValueError, match='byte count'):
        verify_files(package, manifest)
    path.unlink()
    outside = tmp_path / 'outside.safetensors'
    outside.write_bytes(data)
    path.symlink_to(outside)
    with pytest.raises(ValueError, match='outside'):
        verify_files(package, manifest)


def test_runtime_requires_exact_pinned_dependencies(monkeypatch):
    manifest = {'runtime_versions': {'test-runtime': '1.2.3'}}
    monkeypatch.setattr(importlib.metadata, 'version', lambda name: '1.2.3')
    verify_runtime(manifest)
    monkeypatch.setattr(importlib.metadata, 'version', lambda name: '1.2.4')
    with pytest.raises(ValueError, match='requires test-runtime==1.2.3'):
        verify_runtime(manifest)
    def missing(name):
        raise importlib.metadata.PackageNotFoundError(name)
    monkeypatch.setattr(importlib.metadata, 'version', missing)
    with pytest.raises(ValueError, match='missing'):
        verify_runtime(manifest)
