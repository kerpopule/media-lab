"""Bounded request contract shared by the music queue and the isolated renderer."""
import json
MIN_SECONDS, MAX_SECONDS = 10, 120
INSTRUMENTAL = '[inst]'


def decode_request(data):
    if type(data) is not bytes or len(data) > 65536:
        raise ValueError('Invalid music request size.')
    request = json.loads(data)
    if (type(request) is not dict or set(request) != {'prompt', 'lyrics', 'seconds', 'seed'}
            or type(request['prompt']) is not str or not request['prompt'].strip() or len(request['prompt']) > 600
            or type(request['lyrics']) is not str or len(request['lyrics']) > 4000
            or type(request['seconds']) is not int or not MIN_SECONDS <= request['seconds'] <= MAX_SECONDS
            or type(request['seed']) is not int or not 0 <= request['seed'] < 2**32):
        raise ValueError('Invalid music prompt, lyrics, length or seed.')
    return request
