"""Render a portable candidate runtime lock after verifying local build artifacts.
Does not download, build, install, or register an engine.
"""
import argparse
import hashlib
import json
from pathlib import Path

TEMPLATE = Path(__file__).with_name('data') / 'triposr-cpu-linux-arm64.lock.in'
BUILD = json.loads((TEMPLATE.parent / 'triposr-build.json').read_text())
WHEELS = {name: row['sha256'] for name, row in BUILD['wheels'].items()}
TEMPLATE_SHA = BUILD['recipe_files']['media_lab_core/data/triposr-cpu-linux-arm64.lock.in']['sha256']


def _profile(profile):
    if profile == 'original':
        return BUILD, TEMPLATE, WHEELS, TEMPLATE_SHA
    suffixes = {'without-vision-v1': 'without-vision', 'rebuilt-rust-v1': 'rebuilt-rust'}
    if profile not in suffixes:
        raise ValueError('Choose an explicit reviewed runtime profile.')
    suffix = suffixes[profile]
    template = Path(__file__).with_name('data') / f'triposr-cpu-linux-arm64-{suffix}.lock.in'
    build = json.loads((template.parent / f'triposr-build-{suffix}.json').read_text())
    return (build, template, {name: row['sha256'] for name, row in build['wheels'].items()},
            build['recipe_files']['media_lab_core/data/' + template.name]['sha256'])


def verify_recipes(repository: Path, *, profile='original') -> dict:
    """Check the reviewed build inputs without executing any build scripts."""
    repository = Path(repository).resolve(strict=True)
    build, _, _, _ = _profile(profile)
    for name, expected in build['recipe_files'].items():
        path = repository / name
        if (path.is_symlink() or not path.is_file()
                or not path.resolve().is_relative_to(repository)
                or path.stat().st_size != expected['bytes']):
            raise ValueError('Build recipe is unavailable or changed: ' + name)
        if hashlib.sha256(path.read_bytes()).hexdigest() != expected['sha256']:
            raise ValueError('Build recipe hash changed: ' + name)
    return {'verified_recipes': list(build['recipe_files']), 'install_qualified': False}



def prepare_lock(wheel_root: Path, output: Path, *, profile='original') -> dict:
    _, template_path, wheels, template_sha = _profile(profile)
    wheel_root = Path(wheel_root).resolve(strict=True)
    if not wheel_root.is_dir():
        raise ValueError('Choose the verified wheel directory.')
    for name, expected in wheels.items():
        path = wheel_root / name
        if path.is_symlink() or not path.is_file() or not 0 < path.stat().st_size <= 32 * 1024**2:
            raise ValueError('A required verified wheel is unavailable: ' + name)
        with path.open('rb') as stream:
            digest = hashlib.file_digest(stream, 'sha256').hexdigest()
        if digest != expected:
            raise ValueError('Built wheel hash does not match the reviewed artifact: ' + name)
    template = template_path.read_text()
    if hashlib.sha256(template.encode()).hexdigest() != template_sha:
        raise ValueError('Runtime lock template differs from the reviewed build manifest.')
    token = '${VIBEX_TRIPOSR_WHEEL_URL}'
    if template.count(token) != len(wheels):
        raise ValueError('Unexpected runtime lock template.')
    # as_uri encodes spaces/non-ASCII; no shell interpolation or environment lookup.
    rendered = template.replace(token, wheel_root.as_uri())
    with Path(output).open('x') as stream:
        stream.write(rendered)
    return {'lock_sha256': hashlib.sha256(rendered.encode()).hexdigest(),
            'template_sha256': hashlib.sha256(template.encode()).hexdigest(),
            'verified_wheels': wheels.copy(), 'runtime_profile': profile, 'install_qualified': False}


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', choices=['original', 'without-vision-v1', 'rebuilt-rust-v1'], default='original',
                        help='Explicit candidate recipe; does not activate a model or rewrite old receipts.')
    parser.add_argument('--recipes', type=Path, help='Verify build recipes under this Media Lab repository before rendering.')
    parser.add_argument('--wheels', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    if args.recipes:
        verify_recipes(args.recipes, profile=args.profile)
    print(json.dumps(prepare_lock(args.wheels, args.output, profile=args.profile)))
