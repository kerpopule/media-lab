"""Development CPU loader; requires a separately pinned isolated Perth runtime."""
import hashlib
import os
from pathlib import Path
import stat

MODEL_BYTES = 37413196
MODEL_SHA256 = '9ef795010cfbec43e9eff58c84a2733f25de41f6e824d050f6865b2713465233'
# Pinned hparams.yaml SHA c66e8f8ce5b09d100e719843a064d37a97c5aec13510824343bf4e096b3184db.
CONFIG = dict(batch_size=16, hidden_size=256, hop_size=320,
    loss_type='psychoacoustic', max_lr=0.0001, max_wmark_freq=2000,
    min_lr=1e-5, n_fft=2048, sample_rate=32000, stft_magnitude_min=1e-9,
    use_lr_scheduler=False, use_wandb=True, window_fn='hann', window_size=2048)


def load_watermarker(model_path):
    """Require exact converted bytes, strict state matching and CPU evaluation."""
    path = Path(model_path)
    if not path.is_absolute():
        raise ValueError('Use an absolute path to the reviewed watermark model.')
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_NOFOLLOW', 0) | getattr(os, 'O_NONBLOCK', 0))
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size != MODEL_BYTES:
            raise ValueError('Watermark model does not match the reviewed artifact.')
        payload = stream.read(MODEL_BYTES + 1)
    if len(payload) != MODEL_BYTES or hashlib.sha256(payload).hexdigest() != MODEL_SHA256:
        raise ValueError('Watermark model does not match the reviewed artifact.')
    from safetensors.torch import load
    from perth.perth_net.perth_net_implicit.config import PerthConfig
    from perth.perth_net.perth_net_implicit.model.perth_net import PerthNet
    from perth.perth_net.perth_net_implicit.perth_watermarker import PerthImplicitWatermarker
    model = PerthNet(PerthConfig(**CONFIG))
    model.load_state_dict(load(payload), strict=True)
    model.to('cpu').eval()
    return PerthImplicitWatermarker(run_name=None, perth_net=model)
