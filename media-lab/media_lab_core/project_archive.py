"""Streaming preservation of native project directories; never activates a restore.

Preserves project.json, chat.json, files/ and media/. Existing URI references are
retained byte-for-byte and need application-level reconciliation before activation.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import shutil
import stat
import tempfile
import unicodedata
import zipfile

CHUNK = 1024 * 1024
MAX_FILE = 1024 * CHUNK
MAX_TOTAL = 4 * MAX_FILE
MAX_FILES = 10000
MAX_MANIFEST = 4 * CHUNK
FORMAT = 'vibex/project-directory-archive'


def valid_path(name):
    if not isinstance(name, str) or len(name) > 1000 or '\\' in name or '\x00' in name:
        raise ValueError('Invalid archive path')
    parts = PurePosixPath(name).parts
    if not parts or name != '/'.join(parts) or any(p in ('.', '..') or ':' in p for p in parts):
        raise ValueError('Invalid archive path')
    reserved = {'CON', 'PRN', 'AUX', 'NUL', *(f'COM{i}' for i in range(1, 10)), *(f'LPT{i}' for i in range(1, 10))}
    if any(p.endswith((' ', '.')) or any(ord(c) < 32 for c in p) or p.split('.')[0].upper() in reserved for p in parts):
        raise ValueError('Archive path is not portable across platforms')
    if name not in ('project.json', 'chat.json') and not (len(parts) > 1 and parts[0] in ('files', 'media')):
        raise ValueError('Archive contains a file outside the project layout')
    return name


def signature(info):
    return info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns


def inventory(root):
    if root.is_symlink() or not root.is_dir():
        raise ValueError('Choose a real project directory')
    rows = {}
    seen = set()
    for base, dirs, files in os.walk(root, followlinks=False):
        for name in dirs:
            path = Path(base)/name
            if path.is_symlink():
                raise ValueError('Project directories cannot be symbolic links')
        for name in files:
            path = Path(base)/name
            relative = valid_path(path.relative_to(root).as_posix())
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_FILE:
                raise ValueError('Unsupported or oversized project file')
            key = unicodedata.normalize('NFC', relative).casefold()
            if key in seen:
                raise ValueError('Project contains conflicting file names')
            seen.add(key)
            rows[relative] = signature(info)
    if 'project.json' not in rows or len(rows) > MAX_FILES or sum(v[2] for v in rows.values()) > MAX_TOTAL:
        raise ValueError('Project layout is missing metadata or exceeds archive limits')
    return rows


def pack(source, output):
    source, output = Path(source).absolute(), Path(output).absolute()
    if output.is_relative_to(source) or output.exists():
        raise ValueError('Choose a new archive outside the project')
    before = inventory(source)
    fd, temporary = tempfile.mkstemp(prefix='.vibex-archive-', dir=output.parent)
    os.close(fd)
    try:
        rows = []
        with zipfile.ZipFile(temporary, 'w', compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
            for name in sorted(before):
                digest = hashlib.sha256()
                fd = os.open(source/name, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
                with os.fdopen(fd, 'rb') as stream, archive.open(name, 'w', force_zip64=True) as target:
                    if signature(os.fstat(stream.fileno())) != before[name]:
                        raise ValueError('Project changed while being archived')
                    size = 0
                    while chunk := stream.read(CHUNK):
                        size += len(chunk)
                        if size > before[name][2]:
                            raise ValueError('Project changed while being archived')
                        digest.update(chunk)
                        target.write(chunk)
                    if signature(os.fstat(stream.fileno())) != before[name] or size != before[name][2]:
                        raise ValueError('Project changed while being archived')
                rows.append({'path': name, 'bytes': size, 'sha256': digest.hexdigest()})
            manifest = {'format': FORMAT, 'version': 1, 'files': rows}
            raw = json.dumps(manifest, ensure_ascii=False).encode()
            if len(raw) > MAX_MANIFEST:
                raise ValueError('Archive manifest exceeds its limit')
            archive.writestr('manifest.json', raw)
        if inventory(source) != before:
            raise ValueError('Project changed while being archived')
        with open(temporary, 'rb') as stream:
            os.fsync(stream.fileno())
        # Exclusive publication never replaces an existing backup.
        os.link(temporary, output)
        return {'files': len(rows), 'bytes': sum(r['bytes'] for r in rows), 'archive': str(output)}
    finally:
        Path(temporary).unlink(missing_ok=True)


def unpack(source, destination):
    destination = Path(destination).absolute()
    if destination.exists() or destination.is_symlink():
        raise ValueError('Restore requires a new directory; existing projects are never replaced')
    stage = Path(tempfile.mkdtemp(prefix='.vibex-restore-', dir=destination.parent))
    try:
        with zipfile.ZipFile(source) as archive:
            entries = archive.infolist()
            if len(entries) > MAX_FILES + 1 or len({e.filename for e in entries}) != len(entries):
                raise ValueError('Too many or duplicate archive entries')
            metadata = archive.getinfo('manifest.json')
            if metadata.file_size > MAX_MANIFEST or metadata.compress_type != zipfile.ZIP_STORED:
                raise ValueError('Unsupported archive manifest')
            manifest = json.loads(archive.read(metadata))
            if manifest.get('format') != FORMAT or manifest.get('version') != 1 or not isinstance(manifest.get('files'), list):
                raise ValueError('Unsupported project archive')
            rows = manifest['files']
            names, keys, total = set(), set(), 0
            for row in rows:
                name = valid_path(row['path'])
                key = unicodedata.normalize('NFC', name).casefold()
                size = row['bytes']
                if key in keys or type(size) is not int or not 0 <= size <= MAX_FILE:
                    raise ValueError('Conflicting or oversized archive file')
                keys.add(key); names.add(name); total += size
                if total > MAX_TOTAL:
                    raise ValueError('Archive exceeds storage limit')
                info = archive.getinfo(name)
                mode = info.external_attr >> 16
                if info.file_size != size or info.compress_type != zipfile.ZIP_STORED or (stat.S_IFMT(mode) not in (0, stat.S_IFREG)):
                    raise ValueError('Unsupported archive entry')
            if 'project.json' not in names or names | {'manifest.json'} != {e.filename for e in entries}:
                raise ValueError('Archive manifest does not match its files')
            for row in rows:
                target = stage/row['path']; target.parent.mkdir(parents=True, exist_ok=True)
                digest = hashlib.sha256(); size = 0
                with archive.open(row['path']) as stream, target.open('xb') as writer:
                    while chunk := stream.read(CHUNK):
                        size += len(chunk)
                        if size > row['bytes']:
                            raise ValueError('Archive file exceeds declared size')
                        digest.update(chunk); writer.write(chunk)
                    writer.flush(); os.fsync(writer.fileno())
                if size != row['bytes'] or digest.hexdigest() != row['sha256']:
                    raise ValueError('Archive checksum mismatch')
        # Reserve destination exclusively, then move verified files into it.
        destination.mkdir(mode=0o700)
        try:
            for child in stage.iterdir():
                child.rename(destination/child.name)
        except Exception:
            shutil.rmtree(destination)
            raise
        return {'files': len(rows), 'bytes': total, 'directory': str(destination), 'activated': False}
    finally:
        shutil.rmtree(stage)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('operation', choices=['pack', 'restore'])
    parser.add_argument('source', type=Path)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    print(json.dumps((pack if args.operation == 'pack' else unpack)(args.source, args.destination)))


if __name__ == '__main__':
    main()
