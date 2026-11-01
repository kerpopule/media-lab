"""Independent decode gate for the experimental English speech output contract.

Technical signal checks do not verify spoken words, voice quality or approval.
"""
from array import array
import hashlib
import io
import math
import struct
import sys
import wave

SAMPLE_RATE = 24000
MAX_SECONDS = 120
MAX_BYTES = SAMPLE_RATE * MAX_SECONDS * 2 + 65536


def inspect_wav(data):
    if type(data) is not bytes or not 44 <= len(data) <= MAX_BYTES:
        raise ValueError('Speech output has an invalid size.')
    if data[:4] != b'RIFF' or data[8:12] != b'WAVE' or struct.unpack('<I', data[4:8])[0] + 8 != len(data):
        raise ValueError('Speech output has an invalid WAV container.')
    offset = 12
    audio_chunks = format_chunks = chunks = 0
    while offset < len(data):
        if offset + 8 > len(data) or chunks >= 1024:
            raise ValueError('Speech output has an invalid WAV chunk table.')
        tag = data[offset:offset + 4]
        size = struct.unpack('<I', data[offset + 4:offset + 8])[0]
        offset += 8 + size + (size % 2)
        chunks += 1
        if offset > len(data):
            raise ValueError('Speech output contains incomplete WAV chunks.')
        if tag == b'data':
            audio_chunks += 1
            if size % 2:
                raise ValueError('Speech output contains incomplete audio frames.')
        if tag == b'fmt ':
            format_chunks += 1
    if audio_chunks != 1 or format_chunks != 1:
        raise ValueError('Speech output requires exactly one audio and format chunk.')
    try:
        with wave.open(io.BytesIO(data), 'rb') as audio:
            if (audio.getnchannels(), audio.getsampwidth(), audio.getframerate(), audio.getcomptype()) != (1, 2, SAMPLE_RATE, 'NONE'):
                raise ValueError('Speech output must be 24 kHz mono PCM16.')
            frames = audio.getnframes()
            if not SAMPLE_RATE // 10 <= frames <= SAMPLE_RATE * MAX_SECONDS:
                raise ValueError('Speech output duration is outside the supported range.')
            pcm = audio.readframes(frames)
            if len(pcm) != frames * 2:
                raise ValueError('Speech output contains incomplete audio frames.')
    except (wave.Error, EOFError, struct.error) as error:
        raise ValueError('Speech output could not be decoded.') from error
    samples = array('h')
    samples.frombytes(pcm)
    if sys.byteorder != 'little':
        samples.byteswap()
    peak = max(abs(value) for value in samples)
    rms = math.sqrt(sum(value * value for value in samples) / frames)
    if peak < 64 or rms < 8:
        raise ValueError('Speech output is silent or near-silent.')
    return {'mimeType': 'audio/wav', 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
            'sampleRate': SAMPLE_RATE, 'channels': 1, 'frames': frames,
            'durationSeconds': frames / SAMPLE_RATE, 'peak': peak, 'rms': rms}
