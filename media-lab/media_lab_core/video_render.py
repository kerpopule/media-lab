"""Isolated Wan2.2 TI2V-5B renderer, invoked by the owned worker inside the pinned GPU runtime.

Platform notes baked in from the Spark review (20fo): flash_attn has no wheel here, so the DiT runs through Wan's
own scaled_dot_product_attention fallback; decord has no aarch64 wheel and is only used by input-video paths, so it
is stubbed; Wan builds umt5-xxl with random init before load_state_dict, which the owner skips; the owner sets
THP_MEM_ALLOC_ENABLE=1 because first-touch page faults on the GB10 make CPU tensor materialisation ~500x slower.
"""
import argparse
import hashlib
import importlib.machinery
import json
import os
from pathlib import Path
import sys
import types

from .video_request import decode_request
from .video_artifact import inspect_mp4


def _prepare_imports(source):
    sys.path.insert(0, str(source))
    if 'decord' not in sys.modules:
        stub = types.ModuleType('decord'); stub.__spec__ = importlib.machinery.ModuleSpec('decord', None)

        class VideoReader:
            def __init__(self, *a, **k):
                raise RuntimeError('decord is not available in this runtime')
        stub.VideoReader = VideoReader; stub.cpu = lambda *a, **k: None; sys.modules['decord'] = stub
    import torch
    import torch.nn.init as init
    for name in ('kaiming_uniform_', 'kaiming_normal_', 'xavier_uniform_', 'xavier_normal_', 'uniform_', 'normal_', 'trunc_normal_'):
        setattr(init, name, (lambda tensor, *a, **k: tensor))
    from safetensors.torch import load_file
    original = torch.load

    def fast_load(path, *args, **kwargs):
        if isinstance(path, str) and path.endswith('.pth') and os.path.exists(path[:-4] + '.safetensors'):
            return load_file(path[:-4] + '.safetensors')
        return original(path, *args, **kwargs)
    torch.load = fast_load
    import wan
    import wan.modules.model as model_module, wan.modules.attention as attention_module
    if not attention_module.FLASH_ATTN_2_AVAILABLE:
        model_module.flash_attention = attention_module.attention
    return wan


def drop_page_cache(root):
    """Unified-memory hosts: page cache left by reading the weights competes with the model's own memory and
    starves CUDA context creation. Advise it away before loading (no privilege needed)."""
    dropped = 0
    for base, _, files in os.walk(str(root)):
        for file in files:
            try:
                fd = os.open(os.path.join(base, file), os.O_RDONLY)
                try:
                    os.posix_fadvise(fd, 0, 0, os.POSIX_FADV_DONTNEED); dropped += os.fstat(fd).st_size
                finally:
                    os.close(fd)
            except (OSError, AttributeError):
                pass
    return dropped


def load_pipeline(source, checkpoints):
    drop_page_cache(checkpoints)
    wan = _prepare_imports(Path(source))
    from wan.configs import WAN_CONFIGS
    config = WAN_CONFIGS['ti2v-5B']
    pipe = wan.WanTI2V(config=config, checkpoint_dir=str(checkpoints), device_id=0, rank=0, t5_fsdp=False, dit_fsdp=False,
                       use_sp=False, t5_cpu=False, init_on_cpu=True, convert_model_dtype=True)
    return pipe, config


def render(loaded, input_path, output_dir, revision):
    pipe, config = loaded
    from wan.configs import SIZE_CONFIGS
    from wan.utils.utils import save_video
    with Path(input_path).open('rb') as stream:
        data = stream.read(65537)
    request = decode_request(data)
    size = SIZE_CONFIGS[request['size']]
    video = pipe.generate(request['prompt'], img=None, size=size, max_area=size[0] * size[1], frame_num=request['frames'],
                          shift=config.sample_shift, sample_solver='unipc', sampling_steps=request['steps'],
                          guide_scale=config.sample_guide_scale, seed=request['seed'], offload_model=False)
    output = Path(output_dir) / 'output.mp4'
    save_video(tensor=video[None], save_file=str(output), fps=config.sample_fps, nrow=1, normalize=True, value_range=(-1, 1))
    metadata = inspect_mp4(output.read_bytes())
    receipt = {'version': 1, 'revision': revision, 'inputSha256': hashlib.sha256(data).hexdigest(), 'video': metadata}
    with (Path(output_dir) / 'receipt.json').open('x') as stream:
        json.dump(receipt, stream, sort_keys=True)


def serve(source, checkpoints, revision):
    """Warm mode: keep the loaded pipeline and render one request per JSON line on stdin (see music_render.serve)."""
    channel = os.fdopen(os.dup(1), 'w', buffering=1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    loaded = None
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        stage = 'request'
        try:
            job = json.loads(line)
            if type(job) is not dict or set(job) != {'input', 'output'}:
                raise ValueError('Invalid resident render request.')
            if loaded is None:
                stage = 'load'
                loaded = load_pipeline(source, checkpoints)
                stage = 'render'
            render(loaded, job['input'], job['output'], revision)
            reply = {'ok': True}
        except Exception as error:  # noqa: BLE001 - reported to the owner, which verifies outputs independently
            import traceback
            traceback.print_exc()  # stderr is the owner's per-process resident.log; the reply carries no paths
            reply = {'error': type(error).__name__, 'stage': stage}
        channel.write(json.dumps(reply) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('input', 'output'):
        parser.add_argument('--' + name, type=Path)
    parser.add_argument('--source', type=Path, required=True, help='pinned Wan2.2 checkout')
    parser.add_argument('--checkpoints', type=Path, required=True)
    parser.add_argument('--revision', required=True)
    parser.add_argument('--serve', action='store_true')
    args = parser.parse_args()
    if args.serve:
        serve(args.source, args.checkpoints, args.revision)
        return
    if args.input is None or args.output is None:
        parser.error('--input and --output are required without --serve')
    render(load_pipeline(args.source, args.checkpoints), args.input, args.output, args.revision)


if __name__ == '__main__':
    main()
