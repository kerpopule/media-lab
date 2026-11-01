"""Independent container gate for generated video: an ISO BMFF MP4 with one H.264 track of a known size and bounded length.

No decoder is invoked here; the walk reads box headers only, so a hostile file cannot reach a codec through this gate.
"""
import hashlib
import struct

MAX_BYTES = 64 * 1024**2
MIN_SECONDS, MAX_SECONDS = 0.3, 15.0
SIZES = {(704, 1280), (1280, 704)}
CONTAINERS = {b'moov', b'trak', b'mdia', b'minf', b'stbl'}


def _boxes(data, start, end):
    offset = start
    while offset + 8 <= end:
        size, kind = struct.unpack('>I4s', data[offset:offset + 8]); header = 8
        if size == 1:
            if offset + 16 > end:
                raise ValueError('truncated box')
            size = struct.unpack('>Q', data[offset + 8:offset + 16])[0]; header = 16
        elif size == 0:
            size = end - offset
        if size < header or offset + size > end:
            raise ValueError('box escapes its parent')
        yield kind, offset + header, offset + size
        offset += size
    if offset != end:
        raise ValueError('trailing bytes inside a box')


def inspect_mp4(data):
    if type(data) is not bytes or not 64 <= len(data) <= MAX_BYTES:
        raise ValueError('Video output has an invalid size.')
    try:
        top = list(_boxes(data, 0, len(data)))
        if not top or top[0][0] != b'ftyp' or sum(1 for kind, _, _ in top if kind == b'moov') != 1:
            raise ValueError('not an MP4 with one movie header')
        found = {'timescale': None, 'duration': None, 'tracks': [], 'codecs': []}

        def walk(start, end, depth=0):
            if depth > 8:
                raise ValueError('box nesting too deep')
            for kind, body, stop in _boxes(data, start, end):
                if kind == b'mvhd':
                    version = data[body]
                    if version == 1:
                        found['timescale'], found['duration'] = struct.unpack('>IQ', data[body + 20:body + 32])
                    else:
                        found['timescale'], found['duration'] = struct.unpack('>II', data[body + 12:body + 20])
                elif kind == b'tkhd':
                    width, height = struct.unpack('>II', data[stop - 8:stop])
                    found['tracks'].append((width >> 16, height >> 16))
                elif kind == b'stsd':
                    for entry, _, _ in _boxes(data, body + 8, stop):
                        found['codecs'].append(entry)
                elif kind in CONTAINERS:
                    walk(body, stop, depth + 1)
        for kind, body, stop in top:
            if kind == b'moov':
                walk(body, stop)
    except (struct.error, IndexError, ValueError) as error:
        raise ValueError('Video output could not be parsed as MP4.') from error
    if not found['timescale'] or found['duration'] is None:
        raise ValueError('Video output lacks a movie header.')
    seconds = found['duration'] / found['timescale']
    if not MIN_SECONDS <= seconds <= MAX_SECONDS:
        raise ValueError('Video output duration is outside the supported range.')
    video = [track for track in found['tracks'] if track != (0, 0)]
    if len(video) != 1 or video[0] not in SIZES:
        raise ValueError('Video output must contain exactly one video track of a supported size.')
    if b'avc1' not in found['codecs']:
        raise ValueError('Video output must be H.264.')
    return {'mimeType': 'video/mp4', 'bytes': len(data), 'sha256': hashlib.sha256(data).hexdigest(),
            'width': video[0][0], 'height': video[0][1], 'durationSeconds': round(seconds, 3), 'codec': 'avc1'}
