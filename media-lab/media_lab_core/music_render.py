"""Isolated ACE-Step renderer, invoked by the owned worker inside the pinned GPU runtime."""
import argparse
import hashlib
import json
import os
from pathlib import Path
from .music_request import decode_request
from .music_artifact import inspect_wav


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


def load_pipeline(checkpoints):
    drop_page_cache(checkpoints)
    import torch, torchaudio, soundfile as sf
    # torchaudio 2.14 delegates save() to torchcodec, unavailable on this platform; write PCM16 ourselves.
    def save(path, tensor, sample_rate, **kwargs):
        sf.write(path, tensor.detach().cpu().float().T.numpy(), sample_rate, subtype='PCM_16')
    torchaudio.save = save
    from acestep.pipeline_ace_step import ACEStepPipeline
    return ACEStepPipeline(checkpoint_dir=str(checkpoints), dtype='bfloat16', torch_compile=False,
                           cpu_offload=False, overlapped_decode=False)


def render(pipe, input_path, output_dir, revision):
    with Path(input_path).open('rb') as stream:
        data = stream.read(65537)
    request = decode_request(data)
    output_dir = Path(output_dir)
    output = output_dir / 'output.wav'
    pipe(prompt=request['prompt'], lyrics=request['lyrics'] or '[inst]', audio_duration=request['seconds'], infer_step=27,
         guidance_scale=15.0, scheduler_type='euler', cfg_type='apg', omega_scale=10.0, manual_seeds=[request['seed']],
         save_path=str(output), format='wav')
    metadata = inspect_wav(output.read_bytes())
    receipt = {'version': 1, 'revision': revision, 'inputSha256': hashlib.sha256(data).hexdigest(), 'audio': metadata}
    with (output_dir / 'receipt.json').open('x') as stream:
        json.dump(receipt, stream, sort_keys=True)


def serve(checkpoints, revision):
    """Warm mode: keep the loaded model and render one request per JSON line on stdin.

    The protocol channel is a private duplicate of the original stdout; everything the
    model libraries print goes to stderr so it can never corrupt a reply. Each reply is a
    single line: {"ok": true} or {"error": "..."}; the parent enforces budgets and cancels
    by terminating this process, which drops the model with it."""
    import sys
    channel = os.fdopen(os.dup(1), 'w', buffering=1)
    os.dup2(2, 1)
    sys.stdout = sys.stderr
    pipe = None
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        stage = 'request'
        try:
            job = json.loads(line)
            if type(job) is not dict or set(job) != {'input', 'output'}:
                raise ValueError('Invalid resident render request.')
            if pipe is None:
                stage = 'load'
                pipe = load_pipeline(checkpoints)
                stage = 'render'
            render(pipe, job['input'], job['output'], revision)
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
    parser.add_argument('--checkpoints', type=Path, required=True)
    parser.add_argument('--revision', required=True)
    parser.add_argument('--serve', action='store_true', help='stay resident and render JSON-line requests from stdin')
    args = parser.parse_args()
    if args.serve:
        serve(args.checkpoints, args.revision)
        return
    if args.input is None or args.output is None:
        parser.error('--input and --output are required without --serve')
    render(load_pipeline(args.checkpoints), args.input, args.output, args.revision)


if __name__ == '__main__':
    main()
