"""Opt-in development CPU host with machine-bound qualification evidence."""
import argparse
import hashlib
import io
import json
import os
from pathlib import Path
import platform
import subprocess
import tempfile
import threading
import uuid

from PIL import Image, ImageDraw

from . import background_jobs
from .background_lifecycle import lifecycle_slot
from .birefnet_cpu import MANIFEST, CPU_PLATFORMS, verify_files
from .cpu_worker import WorkerBusy, run_background_job, verify_result


def sha(path):
    with Path(path).open('rb') as handle:
        return hashlib.file_digest(handle, 'sha256').hexdigest()


def binding(runtime, package, cache):
    return {'host': hashlib.sha256(json.dumps(list(platform.uname())).encode()).hexdigest(),
            'system': platform.system(), 'machine': platform.machine(),
            'runtime': str(Path(runtime).absolute()), 'runtime_sha256': sha(runtime),
            'package': str(Path(package).resolve()), 'cache': str(Path(cache).resolve()),
            'manifest_sha256': sha(MANIFEST),
            'adapter_sha256': {name: sha(Path(__file__).with_name(name)) for name in
                              ('birefnet_cpu.py', 'cpu_worker.py', 'background_jobs.py')}}


def verify_installed_runtime(runtime, manifest):
    names = list(manifest['runtime_versions'])
    code = ('import importlib.metadata,json,sys; '
            'print(json.dumps({"versions":{name:importlib.metadata.version(name) for name in json.loads(sys.argv[1])},'
            '"abi":f"cp{sys.version_info.major}{sys.version_info.minor}"}))')
    environment = {key: os.environ[key] for key in ('PATH', 'LANG', 'LC_ALL') if key in os.environ}
    checked = subprocess.run([str(runtime), '-I', '-c', code, json.dumps(names)], env=environment,
                             check=True, capture_output=True, text=True, timeout=30)
    actual = json.loads(checked.stdout)
    if actual['versions'] != manifest['runtime_versions'] or (manifest.get('python_abi') and actual['abi'] != manifest['python_abi']):
        raise ValueError('The qualified dependency versions changed. Requalify the isolated runtime.')


def fixture():
    image = Image.new('RGB', (512, 512), '#f4f0e7')
    mask = Image.new('L', image.size, 0)
    for target, foreground, background in [(image, '#246aa8', '#f4f0e7'), (mask, 255, 0)]:
        draw = ImageDraw.Draw(target)
        draw.ellipse((302, 171, 408, 297), fill=foreground)
        draw.ellipse((326, 194, 384, 274), fill=background)
        draw.rounded_rectangle((132, 142, 338, 354), radius=28, fill=foreground)
    output = io.BytesIO()
    image.save(output, format='PNG')
    return output.getvalue(), mask


def qualify(*, root, runtime, package, cache, execute=run_background_job):
    if (platform.system(), platform.machine()) not in CPU_PLATFORMS:
        raise ValueError('This development CPU package supports macOS arm64 and Linux aarch64 qualification only.')
    root = Path(root).resolve()
    before = binding(runtime, package, cache)
    verify_files(Path(package), json.loads(MANIFEST.read_text()))
    data, expected = fixture()
    runs = []
    for _ in range(2):
        result = execute(job_id=uuid.uuid4().hex, data=data, root=root, runtime=Path(runtime),
                         package=Path(package), cache=Path(cache))
        with Image.open(root/result['path']) as image:
            if image.mode != 'RGBA' or image.size != expected.size:
                raise ValueError('Qualification output dimensions or format changed.')
            alpha = image.getchannel('A')
            actual = [value >= 128 for value in alpha.tobytes()]
            reference = [value >= 128 for value in expected.tobytes()]
            union = sum(a or b for a, b in zip(actual, reference))
            iou = sum(a and b for a, b in zip(actual, reference)) / max(1, union)
            if iou < .98:
                raise ValueError('The qualification fixture did not meet its outline check.')
            runs.append({'artifact': result, 'alpha_sha256': hashlib.sha256(alpha.tobytes()).hexdigest(), 'iou': iou})
    if runs[0]['alpha_sha256'] != runs[1]['alpha_sha256'] or binding(runtime, package, cache) != before:
        raise ValueError('Qualification was nondeterministic or its runtime changed.')
    receipt = {'version': 1, 'scope': 'experimental-background-cpu', 'binding': before, 'runs': runs,
               'root': str(root), 'fixture_sha256': hashlib.sha256(data).hexdigest()}
    with tempfile.NamedTemporaryFile(mode='w', dir=root, prefix='.qualification-', delete=False) as handle:
        temporary = Path(handle.name)
        json.dump(receipt, handle)
        handle.flush()
        os.fsync(handle.fileno())
    try:
        os.replace(temporary, root/'qualification.json')
    finally:
        temporary.unlink(missing_ok=True)
    return root/'qualification.json'


def verify_receipt(path):
    path = Path(path)
    if path.is_symlink() or path.stat().st_size > 32768:
        raise ValueError('Invalid CPU qualification receipt.')
    receipt = json.loads(path.read_text())
    saved = receipt['binding']
    if (receipt.get('version'), receipt.get('scope')) != (1, 'experimental-background-cpu') or (
            saved.get('system'), saved.get('machine')) not in CPU_PLATFORMS:
        raise ValueError('This host does not have an accepted development qualification.')
    if binding(saved['runtime'], saved['package'], saved['cache']) != saved:
        raise ValueError('The qualified host, adapter or runtime changed. Requalify before activation.')
    verify_files(Path(saved['package']), json.loads(MANIFEST.read_text()))
    verify_installed_runtime(saved['runtime'], json.loads(MANIFEST.read_text()))
    data, expected = fixture()
    runs = receipt.get('runs', [])
    if (receipt.get('fixture_sha256') != hashlib.sha256(data).hexdigest() or len(runs) != 2
            or any(run.get('iou', 0) < .98 for run in runs)
            or runs[0].get('alpha_sha256') != runs[1].get('alpha_sha256')):
        raise ValueError('The deterministic qualification evidence is missing.')
    # Verify retained tracer output rather than treating a JSON flag as proof.
    root = Path(receipt['root']).resolve()
    for run in runs:
        artifact = run['artifact']
        output = (root/artifact['path']).resolve()
        if not output.is_relative_to(root) or output.stat().st_size != artifact['bytes'] or sha(output) != artifact['sha256']:
            raise ValueError('A retained qualification artifact is missing or changed.')
        verify_result(output.parent, hashlib.sha256(data).hexdigest(), expected.size)
        with Image.open(output) as image:
            alpha = image.getchannel('A').tobytes()
            actual = [value >= 128 for value in alpha]
            reference = [value >= 128 for value in expected.tobytes()]
            iou = sum(a and b for a, b in zip(actual, reference)) / max(1, sum(a or b for a, b in zip(actual, reference)))
            if iou < .98 or abs(iou-run['iou']) > 1e-9 or hashlib.sha256(alpha).hexdigest() != run['alpha_sha256']:
                raise ValueError('The retained qualification mask changed.')
    return {**saved, 'artifact_root': str(root)}


class BackgroundHost:
    def __init__(self, get_store, root, receipt=None):
        self.get_store = get_store
        self.root = Path(root)
        self.receipt = receipt
        self.thread = None
        self.stop_requested = threading.Event()
        self.ready = False
        self.error = None

    def start(self):
        if not self.receipt or (self.thread and self.thread.is_alive()):
            return
        self.stop_requested.clear()
        self.ready = False
        self.error = None
        self.thread = threading.Thread(target=self._run, name='studio-background-cpu', daemon=True)
        self.thread.start()

    def _run(self):
        try:
            with lifecycle_slot(self.root):
                if (self.root/'runtime-disabled.json').exists():
                    raise ValueError('This runtime was disabled for removal.')
                config = verify_receipt(self.receipt)
                if self.root.resolve() != Path(config['artifact_root']):
                    raise ValueError('The API must use the qualified canonical artifact root.')
                self.ready = True
                while not self.stop_requested.is_set():
                    try:
                        background_jobs.run_next(self.get_store(), root=self.root, runtime=Path(config['runtime']),
                                                 package=Path(config['package']), cache=Path(config['cache']))
                    except WorkerBusy:
                        pass
                    self.stop_requested.wait(1)
        except Exception:
            self.error = 'The background-removal host needs qualification or repair.'
        finally:
            self.ready = False

    def engines(self):
        if not self.ready or not self.thread or not self.thread.is_alive() or self.stop_requested.is_set():
            return []
        return [{'id': background_jobs.ENGINE, 'revision': json.loads(MANIFEST.read_text())['revision'],
                 'operation': 'remove-background'}]

    def admit(self, payload):
        from fastapi import HTTPException
        if not self.engines():
            raise HTTPException(503, self.error or 'No independently qualified generation engine is connected yet.')
        try:
            background_jobs.validate_payload(payload)
        except ValueError as error:
            raise HTTPException(409, str(error)) from None

    def stop(self):
        self.stop_requested.set()
        # Finish healthy owned work; do not cancel it merely because the API is
        # shutting down. The worker already has its own bounded deadline.
        if self.thread:
            self.thread.join(timeout=630)


def main():
    parser = argparse.ArgumentParser(description='Qualify an already installed pinned CPU adapter on this host.')
    for name in ('root', 'runtime', 'package', 'cache'):
        parser.add_argument('--'+name, type=Path, required=True)
    args = parser.parse_args()
    with lifecycle_slot(args.root):
        print(qualify(**vars(args)))


if __name__ == '__main__':
    main()
