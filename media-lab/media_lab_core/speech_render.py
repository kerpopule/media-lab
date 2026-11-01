"""Isolated experimental CPU speech renderer. Invoked by the owned worker."""
import argparse
import hashlib
import json
from pathlib import Path
from .speech_request import decode_request
from .speech_artifact import inspect_wav


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('input', 'output', 'models', 'watermark', 'voice'):
        parser.add_argument('--' + name, type=Path, required=True)
    parser.add_argument('--revision', required=True)
    args = parser.parse_args()
    with args.input.open('rb') as stream:
        data = stream.read(65537)
    request = decode_request(data)
    import torch
    import numpy as np
    import soundfile as sf
    from .chatterbox_cpu import load_cpu, use_upstream_default_voice
    torch.set_num_threads(2)
    torch.manual_seed(request['seed'])
    np.random.seed(request['seed'])
    model = load_cpu(args.models, args.watermark)
    use_upstream_default_voice(model, args.voice)
    with torch.inference_mode():
        audio = model.generate(request['text']).detach().cpu().numpy().squeeze()
    if audio.ndim != 1 or not np.isfinite(audio).all() or np.max(np.abs(audio), initial=0) > 1:
        raise ValueError('Speech model returned invalid or clipping audio.')
    output = args.output / 'output.wav'
    with output.open('xb') as stream:
        sf.write(stream, audio, model.sr, format='WAV', subtype='PCM_16')
    metadata = inspect_wav(output.read_bytes())
    receipt = {'version':1, 'revision':args.revision,
               'inputSha256':hashlib.sha256(data).hexdigest(), 'audio':metadata}
    with (args.output/'receipt.json').open('x') as stream:
        json.dump(receipt, stream, sort_keys=True)


if __name__ == '__main__':
    main()
