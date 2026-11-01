"""Explicit development installation; never activates a host or changes services."""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import tempfile
import time
import urllib.request

from .birefnet_cpu import CPU_PLATFORMS, MANIFEST, verify_files
from .background_host import qualify, verify_receipt, verify_installed_runtime
from .cpu_worker import cpu_slot
from .background_lifecycle import lifecycle_slot


def digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def fetch_file(spec, destination, *, open_url=urllib.request.urlopen):
    """Publish only a complete pinned file; interrupted transfers are disposable."""
    destination = Path(destination)
    if destination.is_symlink():
        raise ValueError('A package file cannot be a symbolic link.')
    if destination.exists():
        if destination.stat().st_size != spec['bytes'] or digest(destination) != spec['sha256']:
            raise ValueError('An existing package file changed; choose a fresh install directory.')
        return
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix='.download-', delete=False) as output:
            temporary = Path(output.name)
            total = 0
            hasher = hashlib.sha256()
            with open_url(spec['url'], timeout=60) as response:
                while chunk := response.read(min(1024**2, spec['bytes'] - total + 1)):
                    total += len(chunk)
                    if total > spec['bytes']:
                        raise ValueError('The download exceeds its pinned size.')
                    hasher.update(chunk)
                    output.write(chunk)
            if total != spec['bytes'] or hasher.hexdigest() != spec['sha256']:
                raise ValueError('The download failed its pinned size or SHA-256 check.')
            output.flush()
            os.fsync(output.fileno())
        # No replacement: a concurrent writer must not overwrite accepted bytes.
        os.link(temporary, destination)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def write_receipt(path, value):
    with tempfile.NamedTemporaryFile(mode='w', dir=path.parent, prefix='.install-', delete=False) as stream:
        temporary = Path(stream.name)
        json.dump(value, stream, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    try:
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def inventory_runtime(runtime, root, lock_fds):
    inventory = root/'runtime-inventory.json'
    subprocess.run([str(runtime), '-I', str(Path(__file__).with_name('runtime_inventory.py')),
                    '--manifest', str(MANIFEST), '--output', str(inventory)],
                   check=True, timeout=60, pass_fds=lock_fds)
    return digest(inventory)


def install(*, root, artifact_root, python, uv, reinstall=False):
    with lifecycle_slot(artifact_root) as lifecycle_fd:
        disabled = Path(artifact_root)/'runtime-disabled.json'
        if disabled.exists():
            if not reinstall:
                raise ValueError('This runtime was removed. Use --reinstall to restore it while retaining creations.')
            if disabled.is_symlink() or json.loads(disabled.read_text()).get('installation') != str(Path(root).absolute()):
                raise ValueError('The disabled runtime belongs to a different installation.')
        return _install(root=root, artifact_root=artifact_root, python=python, uv=uv, lifecycle_fd=lifecycle_fd, reinstall=reinstall)


def _install(*, root, artifact_root, python, uv, lifecycle_fd, reinstall=False):
    import psutil
    if (platform.system(), platform.machine()) not in CPU_PLATFORMS:
        raise ValueError('This package supports macOS arm64 and Linux aarch64 only.')
    manifest = json.loads(MANIFEST.read_text())
    lock = MANIFEST.with_suffix('.requirements.lock')
    root = Path(root).absolute()
    artifact_root = Path(artifact_root).absolute()
    if root.is_symlink() or root.resolve() != root or artifact_root.resolve() != artifact_root:
        raise ValueError('Use explicit installation and artifact paths without symbolic links.')
    if root == artifact_root or root.is_relative_to(artifact_root) or artifact_root.is_relative_to(root):
        raise ValueError('Keep runtime installation and job artifacts in separate directories.')
    identity = {'manifest_sha256': digest(MANIFEST), 'lock_sha256': digest(lock),
                'artifact_root': str(artifact_root), 'python': str(Path(python).absolute())}
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    lock_fd = os.open(root/'.install.lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        receipt_path = root/'install.json'
        receipt = {'version': 1, 'identity': identity, 'stage': 'preflight', 'status': 'running'}
        if receipt_path.is_symlink():
            raise ValueError('Invalid installation receipt path.')
        if receipt_path.exists():
            previous = json.loads(receipt_path.read_text())
            if previous.get('identity') != identity:
                raise ValueError('This directory belongs to a different install plan. Choose a fresh directory.')
            if previous.get('status') == 'removing':
                raise ValueError('Finish the interrupted removal before reinstalling.')
            if previous.get('status') == 'removed':
                if not reinstall or not (artifact_root/'runtime-disabled.json').exists():
                    raise ValueError('Reinstallation requires matching removal evidence and --reinstall.')
                if any((root/name).exists() for name in ('package','runtime','download-cache','model-cache')):
                    raise ValueError('Removed runtime directories changed; inspect them before reinstalling.')
                old = artifact_root/'qualification.json'
                if old.is_symlink():
                    raise ValueError('Invalid retired qualification path.')
                if old.exists():
                    retired = artifact_root/('qualification-retired-'+digest(old)+'.json')
                    if retired.exists():
                        if retired.is_symlink() or digest(retired) != digest(old):
                            raise ValueError('Retired qualification evidence changed.')
                    else:
                        os.link(old, retired)
                    old.unlink()
            if previous.get('status') == 'qualified' and not (artifact_root/'qualification.json').exists():
                raise ValueError('Qualified installation evidence is missing; refusing to modify its runtime.')
            if (artifact_root/'qualification.json').exists():
                verified = verify_receipt(artifact_root/'qualification.json')
                if (verified['runtime'], verified['package'], verified['cache'], verified['artifact_root']) != (
                        str(root/'runtime/bin/python'), str(root/'package'), str(root/'model-cache'), str(artifact_root)):
                    raise ValueError('The qualification belongs to a different installation.')
                inventory = root/'runtime-inventory.json'
                if previous.get('inventory_sha256'):
                    if inventory.is_symlink() or digest(inventory) != previous['inventory_sha256']:
                        raise ValueError('Dependency notice inventory changed.')
                else:
                    previous['inventory_sha256'] = inventory_runtime(Path(verified['runtime']), root, (lock_fd, lifecycle_fd))
                previous.update(status='qualified', stage='complete', qualification=str(artifact_root/'qualification.json'),
                                activation='disabled; no app or service settings changed', updated_at=time.time())
                write_receipt(receipt_path, previous)
                if reinstall:
                    (artifact_root/'runtime-disabled.json').unlink(missing_ok=True)
                return previous
        elif any(path.name != '.install.lock' for path in root.iterdir()):
            raise ValueError('Use an empty install directory or resume its matching receipt.')
        def stage(name):
            receipt.update(stage=name, status='running', updated_at=time.time())
            write_receipt(receipt_path, receipt)
            print('Installation stage: '+name, flush=True)
        try:
            stage('preflight')
            if psutil.virtual_memory().available < 12 * 1024**3:
                raise ValueError('Free at least 12 GiB of memory before installing and qualifying this model.')
            # Workspace reserve, not a claimed measured download/runtime size.
            if shutil.disk_usage(root).free < 8 * 1024**3:
                raise ValueError('This install requires an 8 GiB free workspace reserve.')
            if (artifact_root/'qualification.json').exists():
                raise ValueError('That artifact root already has a qualification. Use a separate development root.')
            checked = subprocess.run([str(python), '-I', '-c',
                'import sys; print(f"cp{sys.version_info.major}{sys.version_info.minor}")'],
                check=True, capture_output=True, text=True, timeout=30)
            if checked.stdout.strip() != manifest['python_abi']:
                raise ValueError('Install Python 3.12 and pass its interpreter path.')
            package = root/'package'
            package.mkdir(exist_ok=True)
            if package.is_symlink():
                raise ValueError('Invalid package directory.')
            stage('download-and-verify')
            for spec in [*manifest['files'], manifest['weight']]:
                fetch_file(spec, package/spec['file'])
            verify_files(package, manifest)
            for directory in ('download-cache', 'model-cache'):
                if (root/directory).is_symlink():
                    raise ValueError('Installation caches cannot be symbolic links.')
            stage('runtime')
            runtime_dir = root/'runtime'
            if runtime_dir.is_symlink():
                raise ValueError('Invalid runtime directory.')
            runtime = runtime_dir/'bin/python'
            env = {key: value for key, value in os.environ.items()
                   if not key.startswith(('UV_', 'PIP_', 'PYTHON'))}
            env['UV_CACHE_DIR'] = str(root/'download-cache')
            commands = []
            if not runtime.exists():
                commands.append([str(uv), '--no-config', 'venv', '--python', str(python), str(runtime_dir)])
            commands.append([str(uv), '--no-config', 'pip', 'sync', '--python', str(runtime),
                '--require-hashes', '--only-binary', ':all:', '--default-index', 'https://pypi.org/simple', str(lock)])
            with cpu_slot(artifact_root) as cpu_fd:
                for command in commands:
                    with tempfile.NamedTemporaryFile(mode='w', dir=root, prefix='runtime-', suffix='.log', delete=False) as log:
                        receipt['runtime_log'] = Path(log.name).name
                        subprocess.run(command, env=env, check=True, timeout=1800,
                                       stdout=log, stderr=subprocess.STDOUT, pass_fds=(lock_fd, cpu_fd, lifecycle_fd))
                verify_installed_runtime(runtime, manifest)
            stage('dependency-inventory')
            receipt['inventory_sha256'] = inventory_runtime(runtime, root, (lock_fd, lifecycle_fd))
            stage('qualification')
            artifact_root.mkdir(parents=True, exist_ok=True)
            qualification = qualify(root=artifact_root, runtime=runtime, package=package, cache=root/'model-cache')
            verify_receipt(qualification)
            receipt.update(status='qualified', stage='complete', qualification=str(qualification),
                           activation='disabled; no app or service settings changed', updated_at=time.time())
            write_receipt(receipt_path, receipt)
            if reinstall:
                (artifact_root/'runtime-disabled.json').unlink(missing_ok=True)
            return receipt
        except Exception as error:
            receipt.update(status='failed', error_type=type(error).__name__, updated_at=time.time())
            write_receipt(receipt_path, receipt)
            raise
    finally:
        os.close(lock_fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('root', 'artifact-root', 'python', 'uv'):
        parser.add_argument('--'+name, type=Path, required=True)
    parser.add_argument('--reinstall', action='store_true', help='Restore a removed runtime in place, retaining its creations.')
    args = parser.parse_args()
    print(json.dumps(install(root=args.root, artifact_root=args.artifact_root, python=args.python, uv=args.uv, reinstall=args.reinstall), indent=2))


if __name__ == '__main__':
    main()
