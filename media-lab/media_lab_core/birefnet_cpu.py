"""Pinned, CPU-only BiRefNet adapter for isolated qualification workers.

No downloads or registration happen here. Heavy dependencies belong to the
separate locked runtime, never the Media Lab API environment. The caller owns
memory admission, single-flight execution and the worker deadline.
"""
from __future__ import annotations

import hashlib
import importlib.metadata
import io
import json
import os
import platform
import sys
import time
from pathlib import Path

CPU_PLATFORMS = {('Darwin', 'arm64'), ('Linux', 'aarch64')}


def platform_manifest(system=None, machine=None):
    pair = (system or platform.system(), machine or platform.machine())
    name = 'birefnet-cpu-linux-arm64.json' if pair == ('Linux', 'aarch64') else 'birefnet-cpu.json'
    # Unsupported hosts can still inspect metadata. Host activation has a
    # separate explicit platform gate and never treats this default as support.
    return Path(__file__).with_name('data') / name


MANIFEST = platform_manifest()


def verify_files(root: Path, manifest: dict) -> None:
    root = Path(root).resolve()
    for entry in manifest['files'] + [manifest['weight']]:
        path = (root / entry['file']).resolve()
        if not path.is_relative_to(root) or not path.is_file():
            raise ValueError('A required pinned model file is missing or outside the package.')
        if path.stat().st_size != entry['bytes']:
            raise ValueError('A pinned model file has the wrong byte count.')
        with path.open('rb') as handle:
            digest = hashlib.file_digest(handle, 'sha256').hexdigest()
        if digest != entry['sha256']:
            raise ValueError('A pinned model file failed SHA-256 verification.')


def verify_runtime(manifest: dict) -> None:
    if manifest.get('python_abi') and manifest['python_abi'] != f'cp{sys.version_info.major}{sys.version_info.minor}':
        raise ValueError('The pinned runtime requires its specified Python ABI.')
    for name, expected in manifest['runtime_versions'].items():
        try:
            actual = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            raise ValueError(f'The isolated runtime is missing {name}.') from None
        if actual != expected:
            raise ValueError(f'The isolated runtime requires {name}=={expected}; found {actual}.')


class BiRefNetCPU:
    def __init__(self, package: Path, cache: Path):
        self.manifest = json.loads(MANIFEST.read_text())
        verify_files(package, self.manifest)
        verify_runtime(self.manifest)
        # Set these before importing Transformers. This adapter must be hosted
        # in its own process, so settings cannot affect another model runtime.
        os.environ.update(HF_HOME=str(Path(cache).resolve()), HF_HUB_OFFLINE='1',
                          TRANSFORMERS_OFFLINE='1', CUDA_VISIBLE_DEVICES='')
        import torch
        from transformers import AutoModelForImageSegmentation
        from torchvision import transforms
        from safetensors import safe_open
        with safe_open(Path(package) / 'model.safetensors', framework='pt', device='cpu') as weights:
            if not weights.keys():
                raise ValueError('The pinned SafeTensor package is empty.')
        torch.set_num_threads(4)
        torch.manual_seed(7)
        torch.use_deterministic_algorithms(True)
        self.model = AutoModelForImageSegmentation.from_pretrained(
            str(Path(package).resolve()), trust_remote_code=True, local_files_only=True,
            use_safetensors=True, dtype=torch.float32).eval().to('cpu')
        if any(parameter.device.type != 'cpu' or parameter.dtype != torch.float32 for parameter in self.model.parameters()):
            raise RuntimeError('This adapter requires CPU float32 weights without substitution.')
        self.transform = transforms.Compose([
            transforms.Resize((1024, 1024)), transforms.ToTensor(),
            transforms.Normalize([.485, .456, .406], [.229, .224, .225]),
        ])

    def remove_background(self, data: bytes) -> tuple[bytes, dict]:
        import torch
        from torchvision import transforms
        from PIL import Image, ImageOps, ImageChops
        if not data or len(data) > 20 * 1024 * 1024:
            raise ValueError('Choose an image no larger than 20 MiB.')
        try:
            with Image.open(io.BytesIO(data)) as source:
                if source.format not in ('PNG', 'JPEG', 'WEBP') or getattr(source, 'n_frames', 1) != 1:
                    raise ValueError('Choose a single PNG, JPEG or WebP image.')
                if source.width * source.height > 16_000_000:
                    raise ValueError('The image exceeds the 16-megapixel input limit.')
                original = ImageOps.exif_transpose(source).convert('RGBA')
        except (OSError, Image.DecompressionBombError):
            raise ValueError('The image could not be decoded safely.') from None
        started = time.monotonic()
        tensor = self.transform(original.convert('RGB')).unsqueeze(0).to('cpu')
        with torch.inference_mode():
            prediction = self.model(tensor)[-1].sigmoid().cpu()
        if not torch.isfinite(prediction).all():
            raise RuntimeError('Background removal produced an invalid mask.')
        alpha = transforms.ToPILImage()(prediction[0].squeeze()).resize(original.size, Image.Resampling.BILINEAR)
        # Never restore pixels the user already made transparent.
        alpha = ImageChops.multiply(alpha, original.getchannel('A'))
        clean = Image.new('RGBA', original.size)
        clean.paste(original)
        clean.putalpha(alpha)
        output = io.BytesIO()
        clean.save(output, format='PNG')
        png = output.getvalue()
        return png, {'engine': 'birefnet-cpu', 'revision': self.manifest['revision'],
                     'device': 'cpu', 'dtype': 'float32', 'seconds': time.monotonic() - started,
                     'width': clean.width, 'height': clean.height,
                     'input_sha256': hashlib.sha256(data).hexdigest(),
                     'output_sha256': hashlib.sha256(png).hexdigest()}


def main():
    import argparse
    parser = argparse.ArgumentParser(description='Run one isolated, pinned CPU background-removal job.')
    for name in ('package', 'cache', 'input', 'output'):
        parser.add_argument('--' + name, type=Path, required=True)
    args = parser.parse_args()
    if not args.input.is_file() or not 0 < args.input.stat().st_size <= 20 * 1024 * 1024:
        raise ValueError('Choose an image no larger than 20 MiB.')
    model = BiRefNetCPU(args.package, args.cache)
    png, receipt = model.remove_background(args.input.read_bytes())
    for name, content in [('output.png', png), ('receipt.json', json.dumps(receipt).encode())]:
        with (args.output / name).open('xb') as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())


if __name__ == '__main__':
    main()
