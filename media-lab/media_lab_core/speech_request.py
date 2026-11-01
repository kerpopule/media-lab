"""Bounded request contract shared by speech queue and isolated renderer."""
import json
VOICE = 'upstream-default-english'


def decode_request(data):
    if type(data) is not bytes or len(data) > 65536:
        raise ValueError('Invalid speech request size.')
    request = json.loads(data)
    if (type(request) is not dict or set(request) != {'text', 'voice', 'seed'}
            or type(request['text']) is not str or not request['text'].strip()
            or len(request['text']) > 4000 or request['voice'] != VOICE
            or type(request['seed']) is not int or not 0 <= request['seed'] < 2**32):
        raise ValueError('Invalid speech text, voice or seed.')
    return request
