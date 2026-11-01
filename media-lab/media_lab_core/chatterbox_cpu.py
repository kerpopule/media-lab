"""Development-only exact English Chatterbox CPU loader; no implicit downloads."""
import hashlib
import os
from pathlib import Path
import stat

FILES = {'s3gen.safetensors': (1056484620,
                       '2b78103c654207393955e4900aac14a12de8ef25f4b09424f1ef91941f161d4e'),
 't3_cfg.safetensors': (2129653744,
                        '914cb1696f47527fe8852ca8f1fe1fa63cb34f76f9c715e84e067b744dd0da81'),
 'tokenizer.json': (25470, 'd71e3a44eabb1784df9a68e9f95b251ecbf1a7af6a9f50835856b2ca9d8c14a5'),
 've.safetensors': (5695784,
                    'f0921cab452fa278bc25cd23ffd59d36f816d7dc5181dd1bef9751a7fb61f63c')}


def verified_bytes(root, name):
    size, digest = FILES[name]
    return _verified_file(Path(root) / name, size, digest)


def _verified_file(path, size, digest):
    path = Path(path)
    if not path.is_absolute():
        raise ValueError('Use an absolute model directory.')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size != size:
            raise ValueError('Speech model size does not match the pinned artifact.')
        payload = stream.read(size + 1)
    if len(payload) != size or hashlib.sha256(payload).hexdigest() != digest:
        raise ValueError('Speech model hash does not match the pinned artifact.')
    return payload


def load_cpu(model_root, watermark_path):
    """Explicit model identity, CPU only, no voice preset selected by this loader.

    Requires the separately pinned runtime. A user-authorized reference or a
    reviewed voice preset is still needed before speech generation.
    """
    import torch
    from safetensors.torch import load
    from tokenizers import Tokenizer
    from chatterbox.tts import ChatterboxTTS
    from chatterbox.models.t3 import T3
    from chatterbox.models.s3gen import S3Gen, S3GEN_SR
    from chatterbox.models.voice_encoder import VoiceEncoder
    from chatterbox.models.tokenizers import EnTokenizer
    from media_lab_core.perth_cpu import load_watermarker

    models = []
    for filename, constructor in [('t3_cfg.safetensors', T3), ('s3gen.safetensors', S3Gen), ('ve.safetensors', VoiceEncoder)]:
        # Construct on CPU so nonpersistent positional buffers are initialized too.
        with torch.device('cpu'):
            model = constructor()
        tensors = load(verified_bytes(model_root, filename))
        if filename == 's3gen.safetensors':
            # Exact missing buffer established by the pinned source/header review.
            if 'tokenizer.window' in tensors:
                raise ValueError('Unexpected tokenizer window in checkpoint.')
            tensors['tokenizer.window'] = torch.hann_window(400, device='cpu')
        for value in tensors.values():
            if not torch.isfinite(value).all().item():
                raise ValueError('Speech model contains nonfinite tensors.')
        model.load_state_dict(tensors, strict=True, assign=True)
        model.to('cpu').eval()
        models.append(model)
        del tensors
    tokenizer = EnTokenizer.__new__(EnTokenizer)
    tokenizer.tokenizer = Tokenizer.from_str(verified_bytes(model_root, 'tokenizer.json').decode('utf-8'))
    tokenizer.check_vocabset_sot_eot()
    marker = load_watermarker(watermark_path)

    class OfflineSpeech(ChatterboxTTS):
        def __init__(self):
            self.sr = S3GEN_SR
            self.t3, self.s3gen, self.ve = models
            self.tokenizer = tokenizer
            self.device = 'cpu'
            self.conds = None
            self.watermarker = marker

        @classmethod
        def from_pretrained(cls, *args, **kwargs):
            raise ValueError('Use the verified offline loader.')

        @classmethod
        def from_local(cls, *args, **kwargs):
            raise ValueError('Use the verified offline loader.')

    return OfflineSpeech()


def use_upstream_default_voice(model, preset_path):
    """Explicitly select the reviewed upstream English preset; no personal audio."""
    from safetensors.torch import load
    from chatterbox.tts import Conditionals
    from chatterbox.models.t3.modules.cond_enc import T3Cond
    payload = _verified_file(preset_path, 105316,
        '709e5a7fa80e010a011c8244f553853aed7a49c106fff54008fbd89a0f5a6148')
    values = load(payload)
    model.conds = Conditionals(T3Cond(
        speaker_emb=values['t3.speaker_emb'], clap_emb=None,
        cond_prompt_speech_tokens=values['t3.cond_prompt_speech_tokens'],
        cond_prompt_speech_emb=None, emotion_adv=values['t3.emotion_adv']),
        {'prompt_token':values['gen.prompt_token'],
         'prompt_token_len':values['gen.prompt_token_len'],
         'prompt_feat':values['gen.prompt_feat'], 'prompt_feat_len':None,
         'embedding':values['gen.embedding']})
