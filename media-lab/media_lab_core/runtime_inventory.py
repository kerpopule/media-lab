"""Collect installed-wheel license evidence without importing model packages."""
import argparse
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import tempfile


NOTICE_ROOT = Path(__file__).with_name('data')/'runtime-notices'


def supplemental_notices(versions, root=NOTICE_ROOT):
    manifest=json.loads((root/'manifest.json').read_text())
    if manifest.get('version') != 1:
        raise ValueError('Unsupported supplemental notice manifest.')
    result={}
    for item in manifest['notices']:
        if versions.get(item['package']) != item['version']:
            continue
        path=root/item['file']
        if Path(item['file']).name != item['file'] or path.is_symlink() or path.stat().st_size > 1024**2:
            raise ValueError('Invalid supplemental notice file.')
        data=path.read_bytes()
        if hashlib.sha256(data).hexdigest() != item['sha256']:
            raise ValueError('Supplemental notice hash changed.')
        result.setdefault(item['package'],[]).append({'path':'bundled-notice/'+item['file'],
            'sha256':item['sha256'],'text':data.decode('utf-8'),'source':item['source'],
            'revision':item['revision'],'license':item['license']})
    return result


def collect(versions, distribution=importlib.metadata.distribution):
    rows=[]
    total=0
    supplements=supplemental_notices(versions)
    missing_wheel=[]
    for name, expected in versions.items():
        package=distribution(name)
        if package.version != expected:
            raise ValueError('Installed version does not match the pinned inventory: '+name)
        files=[]
        base=Path(package.locate_file('')).resolve()
        for entry in package.files or []:
            basename=Path(str(entry)).name.lower()
            if not basename.startswith(('license','licence','copying','notice')):
                continue
            path=Path(package.locate_file(entry))
            if not path.resolve().is_relative_to(base):
                raise ValueError('Notice file escapes the installed distribution root.')
            if not path.is_file():
                continue
            size=path.stat().st_size
            if size > 5*1024**2 or total+size > 25*1024**2:
                raise ValueError('License evidence exceeds the inventory size limit.')
            data=path.read_bytes()
            if len(data)!=size:
                raise ValueError('License evidence changed during inventory.')
            total+=size
            files.append({'path':str(entry),'sha256':hashlib.sha256(data).hexdigest(),
                          'text':data.decode('utf-8',errors='replace')})
        if not files:
            missing_wheel.append(name)
        files.extend(supplements.get(name,[]))
        rows.append({'name':name,'version':package.version,
                     'license_expression':package.metadata.get('License-Expression'),
                     'license_metadata':package.metadata.get('License'),
                     'classifiers':[x for x in package.metadata.get_all('Classifier',[]) if x.startswith('License ::')],
                     'project_urls':package.metadata.get_all('Project-URL',[]), 'files':files})
    return {'version':1,'scope':'installed wheel metadata and discovered notice files; not compatibility approval',
            'packages':rows,'missing_wheel_notice_files':missing_wheel,'missing_notice_files':[row['name'] for row in rows if not row['files']]}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--manifest',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    value=collect(json.loads(args.manifest.read_text())['runtime_versions'])
    with tempfile.NamedTemporaryFile(mode='w',dir=args.output.parent,prefix='.inventory-',delete=False) as stream:
        temporary=Path(stream.name)
        json.dump(value,stream,indent=2)
        stream.flush();os.fsync(stream.fileno())
    try:
        os.replace(temporary,args.output)
    finally:
        temporary.unlink(missing_ok=True)


if __name__=='__main__':
    main()
