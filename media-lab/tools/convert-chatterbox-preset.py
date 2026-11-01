"""Convert only the pinned upstream English voice preset in a restricted process."""
import hashlib,json,os
from pathlib import Path
import tempfile
SCHEMA = {'t3': {'speaker_emb': {'shape': [1, 256], 'dtype': 'torch.float32', 'finite': True},
        'clap_emb': None,
        'cond_prompt_speech_tokens': {'shape': [1, 150],
                                      'dtype': 'torch.int64',
                                      'finite': True},
        'cond_prompt_speech_emb': None,
        'emotion_adv': {'shape': [1, 1, 1], 'dtype': 'torch.float32', 'finite': True}},
 'gen': {'prompt_token': {'shape': [1, 157], 'dtype': 'torch.int64', 'finite': True},
         'prompt_token_len': {'shape': [1], 'dtype': 'torch.int64', 'finite': True},
         'prompt_feat': {'shape': [1, 314, 80], 'dtype': 'torch.float32', 'finite': True},
         'prompt_feat_len': None,
         'embedding': {'shape': [1, 192], 'dtype': 'torch.float32', 'finite': True}}}
SIZE = 107374
SHA256 = '6552d70568833628ba019c6b03459e77fe71ca197d5c560cef9411bee9d87f4e'


def convert(source, output):
    source, output = Path(source), Path(output)
    if source.is_symlink() or source.stat().st_size != SIZE or hashlib.sha256(source.read_bytes()).hexdigest() != SHA256:
        raise ValueError('Expected pinned upstream preset.')
    if output.exists() or output.is_symlink():
        raise ValueError('Existing output must be retained.')
    import torch
    from safetensors.torch import save_file, load_file
    torch.serialization.clear_safe_globals()
    state = torch.load(source, map_location='cpu', weights_only=True)
    if type(state) is not dict or set(state) != set(SCHEMA):
        raise ValueError('Unexpected preset structure.')
    tensors = {}
    for group, fields in SCHEMA.items():
        if type(state[group]) is not dict or set(state[group]) != set(fields):
            raise ValueError('Unexpected preset fields.')
        for name, expected in fields.items():
            value = state[group][name]
            if expected is None:
                if value is not None: raise ValueError('Expected empty conditioning field.')
                continue
            if type(value) is not torch.Tensor or value.layout != torch.strided or value.device.type != 'cpu':
                raise ValueError('Expected plain CPU tensor.')
            if list(value.shape) != expected['shape'] or str(value.dtype) != expected['dtype'] or not torch.isfinite(value).all():
                raise ValueError('Unexpected preset tensor.')
            tensors[group + '.' + name] = value.contiguous()
    with tempfile.NamedTemporaryFile(dir=output.parent, prefix='.voice-', delete=False) as stream:
        temp = Path(stream.name)
    try:
        save_file(tensors, str(temp))
        decoded = load_file(str(temp), device='cpu')
        if set(decoded) != set(tensors) or any(not torch.equal(value,decoded[key]) for key,value in tensors.items()):
            raise ValueError('Preset conversion changed tensors.')
        digest = hashlib.sha256(temp.read_bytes()).hexdigest()
        with temp.open('rb') as stream: os.fsync(stream.fileno())
        os.link(temp,output)
        return {'bytes':output.stat().st_size,'sha256':digest,'tensor_count':len(tensors),'roundtrip':True,'speech_qualified':False}
    finally: temp.unlink(missing_ok=True)


if __name__ == '__main__':
    import sys
    print(json.dumps(convert(sys.argv[1],sys.argv[2]),indent=2))
