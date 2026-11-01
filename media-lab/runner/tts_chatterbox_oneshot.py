#!/usr/bin/env python3
"""Media Lab Chatterbox oneshot — hermetic container render.
Stubs `gradio` (missing in the image) so the
models.TTS package import chain survives. Reads /work/copy.txt (one chunk per line),
writes /work/out/vo-N.wav + vo-full.wav."""
import os, sys, types
from pathlib import Path

# --- stub modules missing from the container image (import-time only) ---
class _StubModule(types.ModuleType):
    def __getattr__(self, name):
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        v = _StubModule(self.__name__ + "." + name)
        setattr(self, name, v)
        return v
    def __call__(self, *a, **k):
        return _StubModule(self.__name__ + "()")
    def __mro_entries__(self, bases):
        return (object,)
APP = Path('/opt/maestro/app/app')
import glob as _glob
for _w in _glob.glob('/work/wheels/*.whl'):  # pure-py wheels, zip-import
    sys.path.insert(0, _w)
sys.path.insert(0, str(APP))
os.chdir(APP)

import numpy as np
import soundfile as sf
import torch

for missing in ("gradio", "gradio.themes"):
    if missing not in sys.modules:
        sys.modules[missing] = _StubModule(missing)
from shared.utils import files_locator as fl

fl.set_checkpoints_paths(['/models'])
from models.TTS.chatterbox.pipeline import ChatterboxPipeline

# The alignment stream analyzer must be FULLY disabled: its attention spy
# corrupts the KV cache on transformers 4.57, which makes cloned-voice
# generations babble and never emit the stop token. Merely try/excepting
# step() is NOT enough — the spy must never attach. (This is the fix the
# production redo shipped with.)
from models.TTS.chatterbox.models.t3.inference import alignment_stream_analyzer as _asa
_asa.AlignmentStreamAnalyzer._add_attention_spy = lambda self, *a, **k: None
_asa.AlignmentStreamAnalyzer.step = lambda self, logits, next_token=None: logits
print('ALIGNMENT_ANALYZER_DISABLED', flush=True)

device = torch.device('cuda' if torch.cuda.is_available() else 'cpu')
print(f'TTS_LOADING device={device}', flush=True)
pipe = ChatterboxPipeline(ckpt_root=Path('/models'), device=device)
print(f'TTS_READY sr={pipe.sr}', flush=True)

# The image's mmgp loader casts the F32 checkpoints to bf16 and leaves modules on
# CPU, which breaks generation (babble) and every dtype/device pairing. Reload the
# original float32 weights directly and put the whole stack on the GPU.
import safetensors.torch as _st
_m = pipe.model
_sd = _st.load_file('/models/chatterbox/t3_mtl23ls_v2.safetensors')
print('t3 reload:', _m.t3.load_state_dict(_sd, strict=False), flush=True)
_sd = _st.load_file('/models/chatterbox/ve.safetensors')
print('ve reload:', _m.ve.load_state_dict(_sd, strict=False), flush=True)
_sd = torch.load('/models/chatterbox/s3gen.pt', map_location='cpu', weights_only=True)
if not any(torch.is_tensor(v) for v in _sd.values()):
    _sd = next(v for v in _sd.values() if isinstance(v, dict))
_r = _m.s3gen.load_state_dict(_sd, strict=False)
print('s3gen reload: missing', len(_r.missing_keys), 'unexpected', len(_r.unexpected_keys), flush=True)
for _mod in (_m.t3, _m.ve, _m.s3gen):
    _mod.to(device='cuda', dtype=torch.float32)
    _mod.eval()
print('STACK_F32_CUDA', flush=True)

AUDIO_GUIDE = os.environ.get('TTS_AUDIO_GUIDE') or None
EXAG = float(os.environ.get('TTS_EXAGGERATION', '0.45'))
PACE = float(os.environ.get('TTS_PACE', '0.35'))
LANG = os.environ.get('TTS_LANG', 'en')

def say(text: str):
    r = pipe.generate(text, LANG, AUDIO_GUIDE, temperature=0.7,
                      custom_settings={'exaggeration': EXAG, 'pace': PACE})
    wav = r['x']
    sr = int(r['audio_sampling_rate'])
    w = wav.detach().float().cpu().numpy() if torch.is_tensor(wav) else np.asarray(wav, dtype=np.float32)
    w = np.squeeze(w)
    if w.ndim == 2:
        w = w[0] if w.shape[0] <= 2 else w[:, 0]
    return w.astype(np.float32), sr

lines = [l.strip() for l in Path('/work/copy.txt').read_text().splitlines() if l.strip()]
out = Path('/work/out')
out.mkdir(parents=True, exist_ok=True)
pieces, sr = [], 24000
for i, line in enumerate(lines, 1):
    w, sr = say(line)
    sf.write(str(out / f'vo-{i}.wav'), w, sr, subtype='PCM_16')
    pieces.append(w)
    print(f'VO {i}: {len(w)/sr:.2f}s | {line}', flush=True)
gap = np.zeros(int(sr * 0.6), dtype=np.float32)
full = np.concatenate(sum(([p, gap] for p in pieces[:-1]), []) + [pieces[-1]])
sf.write(str(out / 'vo-full.wav'), full, sr, subtype='PCM_16')
print(f'VO full: {len(full)/sr:.2f}s', flush=True)
print('TTS_DONE', flush=True)
