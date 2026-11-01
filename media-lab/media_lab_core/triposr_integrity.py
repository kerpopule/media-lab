"""Check reviewed rebuilt wheel entries before importing model dependencies.
This covers pinned entries, not unlisted files or other runtime packages.
"""
import hashlib
import json
from pathlib import Path

MANIFEST = Path(__file__).with_name('data') / 'triposr-rebuilt-files.json'
MANIFEST_SHA = '41df95697eaec7fb8cdd2e76d23deac62e76a1baa012255b9db55d3ae78ea3ca'


def verify_files(root):
    root = Path(root).resolve(strict=True)
    raw = MANIFEST.read_bytes()
    if hashlib.sha256(raw).hexdigest() != MANIFEST_SHA:
        raise ValueError('Rebuilt runtime file manifest changed.')
    count = 0
    for package in json.loads(raw)['packages']:
        for item in package['files']:
            parts = item['path'].split('/')
            if any(part in ('', '.', '..') for part in parts) or '\\' in item['path']:
                raise ValueError('Invalid rebuilt runtime file path.')
            path = root
            for part in parts:
                path = path / part
                if path.is_symlink():
                    raise ValueError('Rebuilt runtime file path is a symlink.')
            if not path.is_file() or path.stat().st_size != item['bytes']:
                raise ValueError('Rebuilt runtime file is missing or changed: ' + item['path'])
            with path.open('rb') as stream:
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            if digest != item['sha256']:
                raise ValueError('Rebuilt runtime file hash changed: ' + item['path'])
            count += 1
    return {'rebuilt_files_manifest_sha256': MANIFEST_SHA, 'verified_rebuilt_files': count}
