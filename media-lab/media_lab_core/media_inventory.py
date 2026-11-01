"""Read-only preservation inventory; does not migrate or change a running library.

Run with --output outside the source tree. Reports contain private filenames and
must stay with the user's storage, never in a public repository.
"""
import argparse
from collections import Counter
import hashlib
import json
import os
from pathlib import Path
import stat
from urllib.parse import unquote, urlsplit

DATA_DIRS = ('media', 'voices', 'screenshot-songs', 'jobs', 'archive', 'inbox',
             'cut', 'productions', 'uploads', 'uploads-tmp', 'workspace',
             'reference-sources', 'masks', 'director', 'h3-smoke-exports')
CATALOGS = ('gallery.json', 'characters.json', 'storyboards.json', 'voices.json', 'jobs.json')


def inventory(source):
    root = Path(source).resolve(strict=True)
    files, issues, references, catalogs = [], [], [], {}

    def inspect_file(path):
        relative = path.relative_to(root).as_posix()
        before = path.lstat()
        if not stat.S_ISREG(before.st_mode):
            issues.append({'path': relative, 'reason': 'not_regular_file'})
            return
        # Refuse links at open time too, and detect writes during hashing.
        fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0))
        with os.fdopen(fd, 'rb') as stream:
            opened = os.fstat(stream.fileno())
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            after = os.fstat(stream.fileno())
        current = path.lstat()
        signature = lambda s: (s.st_dev, s.st_ino, s.st_size, s.st_mtime_ns, s.st_ctime_ns)
        stable = signature(before) == signature(opened) == signature(after) == signature(current)
        files.append({'path': relative, 'bytes': after.st_size, 'sha256': digest, 'stable': stable})
        if not stable:
            issues.append({'path': relative, 'reason': 'changed_during_inventory'})

    def walk_error(error):
        try:
            relative = Path(error.filename).relative_to(root).as_posix()
        except (TypeError, ValueError):
            relative = '.'
        issues.append({'path': relative, 'reason': 'directory_unreadable',
                       'errorType': type(error).__name__})

    for name in (*CATALOGS, *DATA_DIRS):
        path = root / name
        if path.is_symlink():
            issues.append({'path': name, 'reason': 'symlink_not_followed'})
        elif path.is_file():
            inspect_file(path)
        elif path.is_dir():
            for base, dirs, names in os.walk(path, followlinks=False, onerror=walk_error):
                for directory in list(dirs):
                    entry = Path(base) / directory
                    if entry.is_symlink():
                        dirs.remove(directory)
                        issues.append({'path': entry.relative_to(root).as_posix(), 'reason': 'symlink_not_followed'})
                for filename in sorted(names):
                    inspect_file(Path(base) / filename)

    def walk(value, catalog, pointer=''):
        if isinstance(value, dict):
            for key, child in value.items():
                walk(child, catalog, pointer + '/' + str(key).replace('~', '~0').replace('/', '~1'))
        elif isinstance(value, list):
            for i, child in enumerate(value):
                walk(child, catalog, pointer + '/' + str(i))
        elif isinstance(value, str):
            # Only explicit local data references: never fetch URLs or expose prompts.
            try:
                parsed = urlsplit(value) if '\n' not in value else None
            except ValueError:
                return
            if parsed is None or parsed.scheme or parsed.netloc:
                return
            decoded = unquote(parsed.path)
            if decoded.startswith('/') and not decoded.startswith('/media/'):
                references.append({'catalog': catalog, 'pointer': pointer, 'path': decoded,
                                   'exists': None, 'insideRoot': False, 'reason': 'external_path_needs_mapping'})
                return
            candidate = decoded.lstrip('/')
            if not any(candidate.startswith(d + '/') for d in DATA_DIRS):
                return
            target = root / candidate
            inside = target.resolve().is_relative_to(root)
            references.append({'catalog': catalog, 'pointer': pointer, 'path': candidate,
                               'exists': inside and target.is_file(), 'insideRoot': inside})

    for name in CATALOGS:
        path = root / name
        if not path.is_file() or path.is_symlink():
            continue
        try:
            raw = path.read_bytes()
            recorded = next(x for x in files if x['path'] == name)
            if hashlib.sha256(raw).hexdigest() != recorded['sha256']:
                issues.append({'path': name, 'reason': 'catalog_changed_before_reference_scan'})
            value = json.loads(raw)
            catalogs[name] = {'entries': len(value.get('jobs', {})) if name == 'jobs.json' and isinstance(value, dict) else len(value)}
            walk(value, name)
        except (ValueError, TypeError) as exc:
            issues.append({'path': name, 'reason': 'invalid_catalog', 'errorType': type(exc).__name__})
    counts = Counter()
    for row in files:
        counts[row['path'].split('/')[0]] += 1
    return {'version': 1, 'source': str(root), 'scope': list(DATA_DIRS),
            'catalogs': catalogs, 'files': sorted(files, key=lambda r: r['path']),
            'references': references, 'issues': issues,
            'summary': {'files': len(files), 'bytes': sum(x['bytes'] for x in files),
                        'byRoot': dict(counts), 'missingReferences': sum(x['exists'] is False for x in references),
                        'externalReferences': sum(x['exists'] is None for x in references),
                        'issues': len(issues)}, 'migrationComplete': False}


def compare_preservation(report, receipt):
    """Describe local source drift; never delete or overwrite the preserved copy."""
    if report.get('source') != receipt.get('source'):
        raise ValueError('inventory and preservation receipt describe different sources')
    if not receipt.get('stagedCopyComplete'):
        raise ValueError('preservation receipt is incomplete')
    before = {row['originalPath']: row for row in receipt['files']}
    after = {row['path']: row for row in report['files']}
    if len(before) != len(receipt['files']) or len(after) != len(report['files']):
        raise ValueError('snapshot contains duplicate paths')
    added = sorted(after.keys() - before.keys())
    removed = sorted(before.keys() - after.keys())
    changed = sorted(path for path in before.keys() & after.keys()
                     if any(before[path][key] != after[path][key] for key in ('bytes', 'sha256')))
    stable = not report['issues'] and all(row.get('stable') is True for row in after.values())
    return {'scope': 'local source files only; external references require a separate recheck',
            'added': added, 'changed': changed, 'removedFromSource': removed,
            'sourceStableDuringScan': stable,
            'localSnapshotMatches': stable and not (added or changed or removed),
            'unchangedFiles': len(before.keys() & after.keys()) - len(changed)}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--compare-receipt', type=Path, help='Report changes since a completed preservation copy')
    args = parser.parse_args()
    if args.output.resolve().is_relative_to(args.source.resolve()):
        parser.error('output must be outside the source library')
    report = inventory(args.source)
    if args.compare_receipt:
        report['snapshotComparison'] = compare_preservation(report, json.loads(args.compare_receipt.read_text()))
    fd = os.open(args.output, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(report, stream, indent=2)
        stream.write('\n')
    print(json.dumps(report['summary']))


if __name__ == '__main__':
    main()
