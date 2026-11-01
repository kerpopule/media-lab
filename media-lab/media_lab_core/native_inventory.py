"""Read ELF dependency metadata without importing or loading native libraries.
This is discovery evidence, not ABI compatibility or license approval.
"""
import argparse
import hashlib
import importlib.metadata
import json
from pathlib import Path
import re
import subprocess
import sys


def parse_dynamic(text):
    values = {'needed': [], 'soname': [], 'rpath': [], 'runpath': []}
    for line in text.splitlines():
        match = re.search(r'\((NEEDED|SONAME|RPATH|RUNPATH)\).*?\[([^\]]*)\]', line)
        if match:
            values[match[1].lower()].append(match[2])
    return values


def collect(packages, root, distribution=importlib.metadata.distribution):
    root = Path(root).resolve()
    rows = []
    seen = set()
    for package in packages:
        installed = distribution(package['name'])
        if installed.version != package['version']:
            raise ValueError('Native inventory package version mismatch.')
        for entry in installed.files or []:
            path = Path(installed.locate_file(entry))
            if not path.is_file():
                continue
            resolved = path.resolve()
            if not resolved.is_relative_to(root):
                raise ValueError('Installed file escapes the selected runtime.')
            if resolved in seen:
                continue
            seen.add(resolved)
            with resolved.open('rb') as stream:
                if stream.read(4) != b'\x7fELF':
                    continue
            if len(rows) >= 1000 or resolved.stat().st_size > 2 * 1024**3:
                raise ValueError('Native inventory exceeds bounds.')
            result = subprocess.run(['/usr/bin/readelf', '--dynamic', str(resolved)],
                                    capture_output=True, text=True, timeout=10, check=True)
            with resolved.open('rb') as stream:
                digest = hashlib.file_digest(stream, 'sha256').hexdigest()
            rows.append({'package': package['name'], 'version': package['version'],
                         'path': str(resolved.relative_to(root)), 'bytes': resolved.stat().st_size,
                         'sha256': digest, **parse_dynamic(result.stdout)})
    names = {Path(row['path']).name for row in rows}
    names.update(name for row in rows for name in row['soname'])
    external = sorted({name for row in rows for name in row['needed'] if name not in names})
    return {'version': 1, 'scope': 'ELF metadata only; no library execution or linkage/license approval',
            'files': rows, 'needed_names_not_in_inventory': external}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--inventory', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    result = collect(json.loads(args.inventory.read_text())['packages'], sys.prefix)
    with args.output.open('x') as stream:
        json.dump(result, stream, indent=2)
