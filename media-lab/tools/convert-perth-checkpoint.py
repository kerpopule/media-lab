"""Development-only conversion of one pinned Perth watermark checkpoint.
Run in an externally memory/time/network-constrained process. Not an installer.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import tempfile

SIZE = 37429684
SHA256 = 'a15bce457ebc53ce5e6c9c3f11df78cf7ee2bf9cdab0a798902135b4c4027670'


def digest(path):
    h = hashlib.sha256()
    with Path(path).open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024**2), b''):
            h.update(chunk)
    return h.hexdigest()


def convert(source, output):
    source, output = Path(source), Path(output)
    if source.is_symlink() or source.stat().st_size != SIZE or digest(source) != SHA256:
        raise ValueError('Checkpoint must match the reviewed immutable source.')
    if output.exists() or output.is_symlink():
        raise ValueError('Choose a new output path; existing files are retained.')
    import torch
    from safetensors import safe_open
    from safetensors.torch import save_file
    # Never permit environment settings or extra globals to broaden this load.
    torch.serialization.clear_safe_globals()
    if torch.serialization.get_safe_globals():
        raise ValueError('Conversion requires an empty user-defined globals allowlist.')
    torch.set_num_threads(4)
    state = torch.load(source, map_location='cpu', weights_only=True, mmap=True)
    if type(state) is not dict or set(state) != {'model', 'step'} or type(state['step']) is not int or state['step'] != 250000:
        raise ValueError('Expected the reviewed Perth training checkpoint envelope.')
    state = state['model']
    if type(state) not in (dict, __import__('collections').OrderedDict) or not 1 <= len(state) <= 10000:
        raise ValueError('Expected a bounded plain state dictionary.')
    tensors = {}
    total = 0
    inventory = []
    for key, value in sorted(state.items()):
        if type(key) is not str or not key or len(key) > 512:
            raise ValueError('Invalid tensor name.')
        if type(value) is not torch.Tensor or value.layout != torch.strided or value.device.type != 'cpu':
            raise ValueError('Expected plain dense CPU tensors.')
        if value.dtype != torch.float32 or value.ndim > 8 or not value.numel():
            raise ValueError('Unexpected tensor dtype or shape.')
        total += value.numel()*value.element_size()
        if total > SIZE or not torch.isfinite(value).all().item():
            raise ValueError('Tensor data exceeds bounds or contains nonfinite values.')
        tensors[key] = value.contiguous()
        inventory.append({'name':key,'shape':list(value.shape),'dtype':str(value.dtype)})
    with tempfile.NamedTemporaryFile(dir=output.parent,prefix='.perth-',delete=False) as stream:
        temporary = Path(stream.name)
    try:
        save_file(tensors,str(temporary))
        # Compare every tensor after decoding the exact prospective artifact.
        with safe_open(temporary,framework='pt',device='cpu') as decoded:
            if set(decoded.keys()) != set(tensors):raise ValueError('Converted keys changed.')
            for key,value in tensors.items():
                if not torch.equal(value,decoded.get_tensor(key)):raise ValueError('Converted tensor changed.')
        with temporary.open('rb') as stream:os.fsync(stream.fileno())
        report = {'version':1,'source_sha256':SHA256,'output_sha256':digest(temporary),
                  'output_bytes':temporary.stat().st_size,'tensor_bytes':total,'tensors':inventory,
                  'torch':torch.__version__,'restricted_load':True,'tensor_roundtrip_verified':True,
                  'model_qualified':False}
        os.link(temporary,output)
        return report
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == '__main__':
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('source',type=Path)
    parser.add_argument('output',type=Path)
    args=parser.parse_args()
    print(json.dumps(convert(args.source,args.output),indent=2))
