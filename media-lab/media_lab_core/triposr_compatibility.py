"""Refuse unreviewed TripoSR runtime combinations before model imports.
Version checks are not installed-file integrity or hardware qualification.
"""
import hashlib
import importlib.metadata
import json
from pathlib import Path
import platform
import re
import sys

MANIFEST = Path(__file__).with_name('data') / 'triposr-runtime.json'


def runtime_spec(profile='original'):
    if profile == 'original':
        path = MANIFEST
    elif profile in ('without-vision-v1', 'rebuilt-rust-v1'):
        path = MANIFEST.with_name('triposr-runtime-without-vision.json')
    else:
        raise ValueError('Choose an explicit reviewed runtime profile.')
    return json.loads(path.read_text())


def validate_runtime(system, machine, python_version, packages, *, profile='original'):
    spec = runtime_spec(profile)
    if spec.get('schema') != 1:
        raise ValueError('Unsupported 3D runtime specification.')
    if (system, machine, list(python_version)) != (spec['system'], spec['machine'], spec['python']):
        raise ValueError('This 3D runtime requires the reviewed Linux ARM64 Python 3.12.3 environment.')
    normalize = lambda name: re.sub('[-_.]+', '-', name).lower()
    actual = {}
    for name, version in packages:
        key = normalize(name)
        if key in actual:
            raise ValueError('Duplicate installed distribution in the 3D runtime.')
        actual[key] = version
    expected = {normalize(name): version for name, version in spec['runtime_versions'].items()}
    if actual != expected:
        raise ValueError('The installed 3D packages differ from the reviewed runtime. Verify its installation.')
    digest = hashlib.sha256(json.dumps(actual, sort_keys=True, separators=(',', ':')).encode()).hexdigest()
    integrity = {}
    if profile == 'rebuilt-rust-v1':
        from .triposr_integrity import MANIFEST_SHA
        integrity = {'rebuilt_files_manifest_sha256': MANIFEST_SHA}
    return {**integrity, **({'profile':profile} if profile != 'original' else {}), 'system':system, 'machine':machine, 'python':list(python_version),
            'package_versions_sha256':digest}


def verify_runtime(*, profile='original'):
    receipt = validate_runtime(platform.system(), platform.machine(), sys.version_info[:3],
                            [(item.metadata['Name'], item.version) for item in importlib.metadata.distributions()], profile=profile)
    if profile == 'rebuilt-rust-v1':
        from .triposr_integrity import verify_files
        import sysconfig
        verify_files(Path(sysconfig.get_path('platlib')))
    return receipt


def expected_runtime_receipt(*, profile='original'):
    """Controller-side receipt contract; never inspect the controller's packages."""
    spec = runtime_spec(profile)
    return validate_runtime(spec['system'], spec['machine'], spec['python'],
                            spec['runtime_versions'].items(), profile=profile)
