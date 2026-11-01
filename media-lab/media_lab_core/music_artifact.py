"""Independent decode gate for generated music: 48 kHz stereo PCM16 WAV, bounded length, not silent."""
from array import array
import hashlib
import io
import math
import struct
import sys
import wave

SAMPLE_RATE = 48000
CHANNELS = 2
MAX_SECONDS = 130
MAX_BYTES = SAMPLE_RATE * CHANNELS * MAX_SECONDS * 2 + 65536


def inspect_wav(data):
    if type(data) is not bytes or not 44 <= len(data) <= MAX_BYTES:
        raise ValueError('Music output has an invalid size.')
    if data[:4] != b'RIFF' or data[8:12] != b'WAVE' or struct.unpack('<I', data[4:8])[0] + 8 != len(data):
        raise ValueError('Music output has an invalid WAV container.')
    try:
        with wave.open(io.BytesIO(data), 'rb') as audio:
            if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getcomptype()) != (CHANNELS, 2, SAMPLE_RATE, 'NONE'):
                raise ValueError('Music output must be 48 kHz stereo PCM16.')
            frames = audio.getnframes()
            if not SAMPLE_RATE * 5 <= frames <= SAMPLE_RATE * MAX_SECONDS:
                raise ValueError('Music output duration is outside the supported range.')
            pcm = audio.readframes(frames)
            if len(pcm) != frames * CHANNELS * 2:
                raise ValueError('Music output contains incomplete audio frames.')
    except (wave.Error, EOFError, struct.error) as error:
        raise ValueError('Music output could not be decoded.') from error
    samples = array('h'); samples.frombytes(pcm)
    if sys.byteorder != 'little':
        samples.byteswap()
    peak = max(abs(value) for value in samples)
    rms = math.sqrt(sum(value * value for value in samples) / len(samples))
    if peak < 64 or rms < 8:
        raise ValueError('Music output is silent or near-silent.')
    return {'mimeType': 'audio/wav', 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
            'sampleRate': SAMPLE_RATE, 'channels': CHANNELS, 'frames': frames,
            'durationSeconds': frames / SAMPLE_RATE, 'peak': peak, 'rms': rms}
