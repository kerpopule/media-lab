"""Independent decode gate for generated images: a single-frame PNG of exactly the requested supported size."""
import hashlib
import io

from PIL import Image

MAX_BYTES = 20 * 1024**2
SIZES = {(1024, 1024), (1280, 768), (768, 1280)}
ALPHA_MODES = ('RGBA', 'LA', 'PA')


def inspect_png(data, require_alpha=False):
    if type(data) is not bytes or not 64 <= len(data) <= MAX_BYTES:
        raise ValueError('Image output has an invalid size.')
    if data[:8] != b'\x89PNG\r\n\x1a\n':
        raise ValueError('Image output is not a PNG.')
    try:
        with Image.open(io.BytesIO(data)) as image:
            if image.format != 'PNG' or getattr(image, 'n_frames', 1) != 1 or image.size not in SIZES:
                raise ValueError('Image output must be a single-frame PNG of a supported size.')
            image.load()
            # A transparent request promises an alpha channel. Decoding it here is the gate: a request that
            # asked for transparency must not be satisfied by an opaque image.
            if require_alpha and image.mode not in ALPHA_MODES:
                raise ValueError('Image output was asked to be transparent but has no alpha channel.')
            width, height = image.size
            mode = image.mode
    except (OSError, Image.DecompressionBombError) as error:
        raise ValueError('Image output could not be decoded.') from error
    return {'mimeType': 'image/png', 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
            'width': width, 'height': height, 'mode': mode}
