"""Bounded request contract shared by the video queue and the isolated renderer (Wan2.2 TI2V-5B)."""
import json

SIZES = ('704*1280', '1280*704')
MIN_FRAMES, MAX_FRAMES = 9, 121          # 4n+1 frames at 24 fps: 9 = 0.33 s, 121 = 5 s
MIN_STEPS, MAX_STEPS = 10, 50


def decode_request(data):
    if type(data) is not bytes or len(data) > 65536:
        raise ValueError('Invalid video request size.')
    request = json.loads(data)
    if (type(request) is not dict or set(request) != {'prompt', 'frames', 'size', 'steps', 'seed'}
            or type(request['prompt']) is not str or not request['prompt'].strip() or len(request['prompt']) > 600
            or type(request['frames']) is not int or not MIN_FRAMES <= request['frames'] <= MAX_FRAMES or request['frames'] % 4 != 1
            or request['size'] not in SIZES
            or type(request['steps']) is not int or not MIN_STEPS <= request['steps'] <= MAX_STEPS
            or type(request['seed']) is not int or not 0 <= request['seed'] < 2**32):
        raise ValueError('Invalid video prompt, length, size, steps or seed.')
    return request
