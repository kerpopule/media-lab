#!/usr/bin/env python3
"""Candidate CPU wheel builder. Run in a bounded, network-denied build process.
Requires the reviewed build environment. Does not install or register its output.
"""
import argparse
import hashlib
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import platform
import shlex
import sys
import tarfile

REVISION='3381600ddc3d2e4d74222f8495866be5fafbace4'
ARCHIVE_SHA='a884dc17c4efafa575c4c167a00c61308429c56cc4d0572f04f03e520ce30ef5'
BUILD_VERSIONS={'torch':'2.11.0+cpu','scikit-build-core':'1.0.3','pybind11':'3.1.0','cmake':'4.4.3','ninja':'1.13.2'}


def build(archive, output):
    archive=archive.resolve(strict=True)
    if archive.stat().st_size!=206534 or hashlib.sha256(archive.read_bytes()).hexdigest()!=ARCHIVE_SHA:
        raise ValueError('The native source archive does not match its reviewed revision.')
    if (platform.system(),platform.machine(),sys.version_info[:3])!=('Linux','aarch64',(3,12,3)):
        raise ValueError('This native build profile is only reviewed on Linux ARM64 Python3.12.3.')
    for name,version in BUILD_VERSIONS.items():
        if metadata.version(name)!=version:raise ValueError('Build dependency version mismatch: '+name)
    output=output.absolute();output.mkdir(mode=0o700,exist_ok=False)
    with tarfile.open(archive) as source:
        members=source.getmembers()
        if len(members)>1000 or sum(m.size for m in members)>20*1024**2:raise ValueError('Source archive exceeds bounds.')
        prefix='torchmcubes-'+REVISION
        for member in members:
            path=Path(member.name)
            if path.is_absolute() or '..' in path.parts or not path.parts or path.parts[0]!=prefix or not (member.isfile() or member.isdir()):
                raise ValueError('Unexpected native source archive member.')
        epoch=int(members[0].mtime)
        source.extractall(output,filter='data')
    runtime=Path(sys.prefix)
    torch_cmake=Path(metadata.distribution('torch').locate_file('torch/share/cmake')).resolve()
    pybind_cmake=Path(metadata.distribution('pybind11').locate_file('pybind11/share/cmake/pybind11')).resolve()
    os.environ['PATH']=str(runtime/'bin')+':/usr/bin:/bin'
    mapped_flags=' '.join(shlex.quote(flag) for flag in [
        '-ffile-prefix-map='+str(output/prefix)+'=/vibex/torchmcubes',
        '-ffile-prefix-map='+str(runtime)+'=/vibex/build-runtime'])
    os.environ['CMAKE_ARGS']=' '.join(shlex.quote(value) for value in [
        '-DCMAKE_CUDA_COMPILER=NOTFOUND','-DCMAKE_CXX_FLAGS='+mapped_flags,'-DCMAKE_PREFIX_PATH='+str(torch_cmake),'-Dpybind11_DIR='+str(pybind_cmake)])
    os.environ['CMAKE_BUILD_PARALLEL_LEVEL']='2'
    os.environ['SOURCE_DATE_EPOCH']=str(epoch)
    wheels=output/'wheels';wheels.mkdir()
    os.chdir(output/prefix)
    from scikit_build_core.build import build_wheel
    filename=build_wheel(str(wheels))
    wheel=wheels/filename
    if wheel.parent!=wheels or not wheel.is_file():raise ValueError('Unexpected wheel output.')
    report={'source_revision':REVISION,'source_sha256':ARCHIVE_SHA,'source_date_epoch':epoch,
            'build_versions':BUILD_VERSIONS,'wheel':filename,'wheel_sha256':hashlib.sha256(wheel.read_bytes()).hexdigest(),
            'wheel_bytes':wheel.stat().st_size,'install_qualified':False}
    (output/'build-receipt.json').write_text(json.dumps(report,indent=2)+'\n')
    return report


if __name__=='__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    print(json.dumps(build(args.archive,args.output)),flush=True)
