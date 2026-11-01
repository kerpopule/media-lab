#!/usr/bin/env python3
"""Build the pinned candidate ANTLR wheel in a bounded, network-denied process.
Requires the reviewed runtime. Does not install or register its output.
"""
import argparse
import hashlib
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import platform
import sys
import tarfile

ARCHIVE_SHA = 'f224469b4168294902bb1efa80a8bf7855f24c99aef99cbefc1bcd3cce77881b'
PREFIX = 'antlr4-python3-runtime-4.9.3'


def build(archive, output):
    archive = archive.resolve(strict=True)
    if archive.stat().st_size != 117034 or hashlib.sha256(archive.read_bytes()).hexdigest() != ARCHIVE_SHA:
        raise ValueError('ANTLR source does not match the reviewed archive.')
    if (platform.system(), platform.machine(), sys.version_info[:3]) != ('Linux', 'aarch64', (3, 12, 3)):
        raise ValueError('This build profile is only reviewed on Linux ARM64 Python 3.12.3.')
    if metadata.version('setuptools') != '81.0.0':
        raise ValueError('Build requires reviewed setuptools 81.0.0.')
    output = output.absolute()
    output.mkdir(mode=0o700, exist_ok=False)
    with tarfile.open(archive) as source:
        members = source.getmembers()
        if len(members) > 1000 or sum(m.size for m in members) > 20 * 1024**2:
            raise ValueError('Source archive exceeds bounds.')
        for member in members:
            path = Path(member.name)
            if path.is_absolute() or '..' in path.parts or not path.parts or path.parts[0] != PREFIX or not (member.isfile() or member.isdir()):
                raise ValueError('Unexpected source archive member.')
        source.extractall(output, filter='data')
    os.environ['SOURCE_DATE_EPOCH'] = '1636221141'
    os.environ['PYTHONHASHSEED'] = '0'
    os.environ['PATH'] = str(Path(sys.prefix) / 'bin') + ':/usr/bin:/bin'
    wheels = output / 'wheels'
    wheels.mkdir()
    os.chdir(output / PREFIX)
    from setuptools.build_meta import build_wheel
    filename = build_wheel(str(wheels))
    if filename != 'antlr4_python3_runtime-4.9.3-py3-none-any.whl':
        raise ValueError('Unexpected wheel output.')
    wheel = wheels / filename
    report = {'source_sha256': ARCHIVE_SHA, 'source_date_epoch': 1636221141,
              'build_versions': {'setuptools': '81.0.0'}, 'wheel': filename,
              'wheel_sha256': hashlib.sha256(wheel.read_bytes()).hexdigest(),
              'wheel_bytes': wheel.stat().st_size, 'install_qualified': False}
    (output / 'build-receipt.json').write_text(json.dumps(report, indent=2) + '\n')
    return report


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.archive, args.output)), flush=True)
