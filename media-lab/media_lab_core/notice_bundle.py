"""Build a deterministic, hash-checked notice/source ZIP; no license approval implied."""
from __future__ import annotations
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import tempfile
import zipfile


def _relative(value: str) -> Path:
    if not isinstance(value, str) or not value or '\\' in value:
        raise ValueError('Invalid bundle path')
    parts=value.split('/')
    if any(part in ('', '.', '..') for part in parts) or PurePosixPath(value).is_absolute():
        raise ValueError('Invalid bundle path')
    return Path(*parts)


def build_bundle(root: Path, entries: list[dict], output: Path) -> dict:
    """Entries supply relative source/path, exact bytes and SHA-256. Never overwrite."""
    if not entries or len(entries)>4096:
        raise ValueError('Invalid bundle entry count')
    root=root.resolve(strict=True)
    names=set(); total=0
    for entry in entries:
        _relative(entry['source']); _relative(entry['path'])
        if entry['path'] in names or entry['path']=='manifest.json':
            raise ValueError('Duplicate or reserved bundle path')
        names.add(entry['path'])
        size=entry['bytes']
        if type(size) is not int or not 0<=size<=64*1024*1024 or not re.fullmatch('[0-9a-f]{64}',entry['sha256']):
            raise ValueError('Invalid bundle file identity')
        total+=size
    if total>128*1024*1024:
        raise ValueError('Bundle exceeds size limit')
    if output.exists() or output.is_symlink():
        raise FileExistsError(output)
    fd,temp=tempfile.mkstemp(prefix='.notice-bundle-',dir=output.parent)
    os.close(fd)
    try:
        with zipfile.ZipFile(temp,'w',compression=zipfile.ZIP_STORED) as archive:
            def write(name, data):
                info=zipfile.ZipInfo(name,date_time=(1980,1,1,0,0,0))
                info.create_system=3;info.external_attr=0o100644<<16
                archive.writestr(info,data)
            manifest=[]
            for entry in sorted(entries,key=lambda item:item['path']):
                relative=_relative(entry['source']); path=root
                for part in relative.parts:
                    path=path/part
                    if path.is_symlink():raise ValueError('Symlink in bundle source')
                if not path.resolve(strict=True).is_relative_to(root) or not path.is_file():
                    raise ValueError('Invalid bundle source')
                with path.open('rb') as stream:data=stream.read(entry['bytes']+1)
                if len(data)!=entry['bytes'] or hashlib.sha256(data).hexdigest()!=entry['sha256']:
                    raise ValueError('Bundle source identity mismatch')
                write(entry['path'],data)
                manifest.append({k:entry[k] for k in ('path','bytes','sha256')})
            write('manifest.json',(json.dumps({'schema':1,'files':manifest},sort_keys=True,indent=2)+'\n').encode())
        with open(temp,'rb') as stream:digest=hashlib.file_digest(stream,'sha256').hexdigest()
        size=os.stat(temp).st_size
        os.link(temp,output)  # Exclusive publication; an existing output wins the race.
        return {'sha256':digest,'bytes':size,'files':len(entries),'license_review_complete':False}
    finally:
        os.unlink(temp)
