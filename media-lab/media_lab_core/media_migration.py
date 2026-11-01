"""Stage an organized, verified copy. Never replaces the live library.

The receipt maps every original path to its new path. Original metadata is kept
byte-for-byte; activation requires a separate reference-rewrite/import phase.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import stat
from urllib.parse import unquote, urlsplit

IMAGE = {'.png', '.jpg', '.jpeg', '.webp', '.gif', '.avif'}
VIDEO = {'.mp4', '.mov', '.mkv', '.webm'}
AUDIO = {'.mp3', '.wav', '.m4a', '.aac', '.flac', '.ogg'}


def safe_name(value):
    text = re.sub(r'[^\w .()-]+', '-', str(value), flags=re.UNICODE).strip(' .-')
    return (text[:70].rstrip(' .') or 'Untitled')


def relative_path(raw):
    path = PurePosixPath(raw)
    if path.is_absolute() or not path.parts or '..' in path.parts or '\\' in raw:
        raise ValueError('unsafe inventory path')
    return path


def destination_for(relative, gallery):
    path = relative_path(relative)
    root = path.parts[0]
    remainder = PurePosixPath(*path.parts[1:])
    if root == 'media':
        row = gallery.get(relative, {})
        kind = row.get('kind', '')
        suffix = path.suffix.lower()
        if suffix in VIDEO:
            category = 'Videos/' + ('Music Videos' if kind in ('musicvideo', 'screenshotmusicvideo') else
                                    'Talking Heads' if kind == 'say' else
                                    'Storyboards' if path.name.startswith('board_') else 'Clips')
        elif suffix in AUDIO:
            category = 'Audio/' + ('Music' if suffix == '.mp3' or kind in ('music', 'screenshotsong') else 'Voice and Recordings')
        elif suffix in IMAGE:
            category = 'Images/' + ('Character References' if path.name.startswith(('char_', 'charlik_')) else
                                    'Generated' if row else 'Supporting Images')
        elif suffix in {'.glb', '.gltf', '.obj', '.fbx'}:
            category = '3D Assets'
        else:
            category = 'Projects/Supporting Files'
        # Keep the original basename available while avoiding case collisions and
        # preserving distinct files with identical titles or content.
        label = safe_name(row.get('title') or path.stem)
        identity = hashlib.sha256(relative.encode()).hexdigest()[:16]
        return f'{category}/{label}--{identity}{suffix}'
    folders = {'voices': 'Audio/Voices', 'cut': 'Projects/Video Edits',
               'productions': 'Projects/Productions', 'jobs': 'Projects/Job History',
               'screenshot-songs': 'Projects/Screenshot Songs', 'inbox': 'Projects/Imported',
               'uploads': 'Images/Uploads', 'uploads-tmp': 'Projects/Unfinished Uploads',
               'reference-sources': 'Images/Reference Sources', 'masks': 'Images/Masks',
               'director': 'Projects/Director', 'workspace': 'Projects/Workspace',
               'archive': 'Projects/Archive', 'h3-smoke-exports': 'Videos/Test Exports'}
    if root in folders:
        return str(PurePosixPath(folders[root]) / remainder)
    return str(PurePosixPath('.studio/original-metadata') / path)


def stage(report, destination):
    source = Path(report['source']).resolve(strict=True)
    destination = Path(destination).absolute()
    if destination.resolve().is_relative_to(source) or source.is_relative_to(destination.resolve()):
        raise ValueError('destination must be separate from source')
    if report.get('issues') or any(not row.get('stable') for row in report['files']):
        raise ValueError('inventory has unresolved file changes or issues')
    gallery = {}
    gallery_entry = next((x for x in report['files'] if x['path'] == 'gallery.json'), None)
    if gallery_entry:
        raw = (source / 'gallery.json').read_bytes()
        if hashlib.sha256(raw).hexdigest() != gallery_entry['sha256']:
            raise ValueError('gallery changed; refresh inventory')
        for row in json.loads(raw):
            parsed = urlsplit(str(row.get('url', '')))
            if not parsed.scheme and not parsed.netloc and parsed.path.startswith('/media/'):
                gallery[unquote(parsed.path).lstrip('/')] = row
    plan, seen = [], set()
    for row in report['files']:
        old = relative_path(row['path'])
        new = destination_for(row['path'], gallery)
        key = new.casefold()
        if key in seen:
            raise ValueError('destination name collision')
        seen.add(key)
        candidate = source / old
        # Never follow an ancestor link outside or within the live data tree.
        current = source
        for part in old.parts:
            current = current / part
            if current.is_symlink():
                raise ValueError('source contains symlink')
        if not candidate.resolve().is_relative_to(source):
            raise ValueError('source escapes root')
        plan.append((row, candidate, new))
    if shutil.disk_usage(destination.parent).free < sum(x['bytes'] for x in report['files']) + 16*1024*1024:
        raise ValueError('not enough free space for verified copy')
    destination.mkdir(mode=0o700)  # Exclusive: never merge into an existing library.
    receipt = {'version': 1, 'source': str(source), 'files': [], 'stagedCopyComplete': False,
               'migrationComplete': False, 'references': report['references']}
    try:
        for row, candidate, new in plan:
            target = destination / new
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            source_fd = os.open(candidate, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
            with os.fdopen(source_fd, 'rb') as original:
                before = os.fstat(original.fileno())
                if not stat.S_ISREG(before.st_mode):
                    raise ValueError('source is not a regular file')
                digest = hashlib.sha256()
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                with os.fdopen(fd, 'wb') as copy:
                    while block := original.read(1024*1024):
                        digest.update(block)
                        copy.write(block)
                    copy.flush()
                    os.fsync(copy.fileno())
                after = os.fstat(original.fileno())
            if (before.st_size, before.st_mtime_ns, before.st_ctime_ns) != (after.st_size, after.st_mtime_ns, after.st_ctime_ns):
                raise ValueError('source changed during copy')
            with target.open('rb') as copied:
                verified = hashlib.file_digest(copied, 'sha256').hexdigest()
            if digest.hexdigest() != row['sha256'] or verified != row['sha256'] or target.stat().st_size != row['bytes']:
                raise ValueError('copy does not match inventory')
            receipt['files'].append({'originalPath': row['path'], 'path': new,
                                     'bytes': row['bytes'], 'sha256': verified})
        receipt['stagedCopyComplete'] = True
    finally:
        fd = os.open(destination/'migration-receipt.json', os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(fd, 'w') as out:
            json.dump(receipt, out, indent=2)
            out.flush()
            os.fsync(out.fileno())
    return receipt


def verify_copy(receipt, destination):
    """Recheck recorded bytes without changing the copy or claiming a cutover.

    Extra files are allowed: the new library can contain derived exports. Only
    receipt entries are checked, so callers must also check source drift.
    """
    if not (receipt.get('stagedCopyComplete') is True or receipt.get('complete') is True):
        raise ValueError('preservation receipt is incomplete')
    root = Path(destination).resolve(strict=True)
    rows = receipt['files']
    seen = set()
    for row in rows:
        name = str(relative_path(row['path']))
        if name.casefold() in seen:
            raise ValueError('duplicate preservation path')
        seen.add(name.casefold())
    issues = []
    checked = 0
    for row in rows:
        try:
            path = root
            for part in relative_path(row['path']).parts:
                path = path / part
                if path.is_symlink():
                    raise ValueError('symlink_not_followed')
            before = path.lstat()
            if not stat.S_ISREG(before.st_mode):
                raise ValueError('not_regular_file')
            fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
            with os.fdopen(fd, 'rb') as stream:
                opened = os.fstat(stream.fileno())
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
                after = os.fstat(stream.fileno())
            current = path.lstat()
            signature = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
            if not signature(before) == signature(opened) == signature(after) == signature(current):
                raise ValueError('changed_during_verification')
            if digest != row['sha256'] or after.st_size != row['bytes']:
                raise ValueError('content_mismatch')
            checked += 1
        except (OSError, ValueError) as exc:
            issues.append({'path': row['path'], 'reason': str(exc) if isinstance(exc, ValueError) else type(exc).__name__})
    return {'recordedFiles': len(rows), 'verifiedFiles': checked, 'issues': issues,
            'copyMatchesReceipt': not issues, 'migrationComplete': False}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('inventory', type=Path)
    parser.add_argument('destination', type=Path)
    parser.add_argument('--verify-copy', action='store_true',
                        help='Treat inventory as a preservation receipt and check the existing copy, without changing it')
    args = parser.parse_args()
    if args.verify_copy:
        result = verify_copy(json.loads(args.inventory.read_text()), args.destination)
        print(json.dumps(result))
        if not result['copyMatchesReceipt']:
            raise SystemExit(1)
        return
    receipt = stage(json.loads(args.inventory.read_text()), args.destination)
    print(json.dumps({'verifiedFiles': len(receipt['files']), 'stagedCopyComplete': receipt['stagedCopyComplete'],
                      'migrationComplete': False}))


if __name__ == '__main__':
    main()
