"""Offline TripoSR CPU candidate. Caller must enforce process/resource isolation.
Not an installable engine: runtime/license/platform qualification remains external.
"""
from __future__ import annotations

import argparse
import hashlib
import io
import json
from pathlib import Path
import sys
import time

SOURCE_RECEIPT_SHA = '0e48cf9dfa1a587d504e2467f6ceef7e5130aaf24c25fb7e7dfa9627707d9d98'
MODEL_SHA = 'f72bb520b8b1a5639600ac818496f22d6ccb3b42d3942412bd1e2375ef780a2b'
CONFIG_SHA = '74ca708ce086bf68e97709ea6b3d91f14717921c04691e84043f0eb8fcc68e62'
DINO_CONFIG_SHA = 'b87c0270b97db085fd82cf114a761fd0f62ae7914fbd407c752a2260646b689c'
VARIANT = 'triposr-cpu-f32-seed7-res128-vertex-color-v1'


def verify_file(path: Path, digest: str, max_bytes: int) -> None:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > max_bytes:
        raise ValueError(f'Invalid model component: {path.name}')
    h = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1024**2), b''):
            h.update(block)
    if h.hexdigest() != digest:
        raise ValueError(f'Model component hash mismatch: {path.name}')


def verify_package(root: Path) -> Path:
    source = root / 'offline-source'
    receipt = source / 'vibex-source-modifications.json'
    verify_file(receipt, SOURCE_RECEIPT_SHA, 65536)
    for name, digest in json.loads(receipt.read_text())['files'].items():
        path = source / name
        if not path.resolve().is_relative_to(source.resolve()):
            raise ValueError('Source component is outside the package.')
        verify_file(path, digest, 4 * 1024**2)
    # Preparation adds this local file separately from the upstream source list.
    verify_file(source / 'dino-config.json', DINO_CONFIG_SHA, 454)
    verify_file(root / 'config.yaml', CONFIG_SHA, 16384)
    verify_file(root / 'model.safetensors', MODEL_SHA, 1677170936)
    return source


def generate(root: Path, input_path: Path, output: Path, *, runtime_profile='original') -> dict:
    # Reserve a new private result directory. Never overwrite previous work.
    output.mkdir(mode=0o700, parents=False, exist_ok=False)
    from PIL import Image
    if input_path.is_symlink() or not input_path.is_file() or input_path.stat().st_size > 20 * 1024**2:
        raise ValueError('Choose a cutout PNG no larger than 20 MiB.')
    data = input_path.read_bytes()
    with Image.open(io.BytesIO(data)) as opened:
        if (opened.format != 'PNG' or opened.mode != 'RGBA' or
                getattr(opened, 'n_frames', 1) != 1 or opened.width * opened.height > 16_000_000):
            raise ValueError('Use a single RGBA cutout PNG up to 16 megapixels.')
        opened.load()
        image = opened.copy()
    alpha = image.getchannel('A').getextrema()
    if alpha[0] == 255 or alpha[1] == 0:
        raise ValueError('Remove the background first; the image needs visible foreground and transparency.')
    from .triposr_compatibility import verify_runtime
    runtime_receipt = verify_runtime(profile=runtime_profile)
    source = verify_package(root)
    sys.path.insert(0, str(source))
    import numpy as np
    import torch
    import trimesh
    from omegaconf import OmegaConf
    from safetensors.torch import load_file
    from tsr.system import TSR
    from tsr.utils import resize_foreground
    from .mesh_order import canonical_mesh_arrays
    from .glb_contract import inspect_generated_glb

    started = time.monotonic()
    torch.set_num_threads(4)
    torch.manual_seed(7)
    torch.use_deterministic_algorithms(True)
    cfg = OmegaConf.load(root / 'config.yaml')
    OmegaConf.resolve(cfg)
    model = TSR(cfg).eval()
    model.load_state_dict(load_file(root / 'model.safetensors', device='cpu'), strict=True)
    pixels = np.array(resize_foreground(image, 0.85)).astype(np.float32) / 255
    image = Image.fromarray(((pixels[:, :, :3] * pixels[:, :, 3:4] +
                             (1 - pixels[:, :, 3:4]) * 0.5) * 255).astype(np.uint8))
    model.renderer.set_chunk_size(8192)
    with torch.inference_mode():
        codes = model([image], device='cpu')
        mesh = model.extract_mesh(codes, True, resolution=128)[0]
    vertices, faces, colors = canonical_mesh_arrays(mesh.vertices, mesh.faces, mesh.visual.vertex_colors)
    mesh = trimesh.Trimesh(vertices=vertices, faces=faces, vertex_colors=colors, process=False)
    blob = mesh.export(file_type='glb')
    inspect_generated_glb(blob)
    decoded = trimesh.load(io.BytesIO(blob), file_type='glb', force='mesh', process=False)
    canonical_mesh_arrays(decoded.vertices, decoded.faces, decoded.visual.vertex_colors)
    if len(decoded.vertices) != len(vertices) or len(decoded.faces) != len(faces):
        raise ValueError('GLB geometry changed during export.')
    receipt = {
        'variant': VARIANT, 'device': 'cpu', 'dtype': 'float32', 'seed': 7, 'resolution': 128,
        'input_sha256': hashlib.sha256(data).hexdigest(), 'model_sha256': MODEL_SHA,
        'source_receipt_sha256': SOURCE_RECEIPT_SHA,
        'output_sha256': hashlib.sha256(blob).hexdigest(), 'bytes': len(blob),
        'vertices': len(vertices), 'triangles': len(faces), 'seconds': time.monotonic() - started,
        'creative_status': 'draft', 'install_qualified': False, 'runtime': runtime_receipt,
    }
    (output / 'output.glb').write_bytes(blob)
    # Receipt is the completion marker; an interrupted worker has no valid result.
    pending = output / 'receipt.pending'
    pending.write_text(json.dumps(receipt, indent=2) + '\n')
    pending.replace(output / 'receipt.json')
    return receipt


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--runtime-profile', choices=['original', 'without-vision-v1', 'rebuilt-rust-v1'], default='original')
    parser.add_argument('--package', type=Path, required=True)
    parser.add_argument('--input', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(generate(args.package.resolve(), args.input, args.output, runtime_profile=args.runtime_profile)), flush=True)
