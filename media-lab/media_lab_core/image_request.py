"""Bounded request contract shared by the image queue and the isolated renderer (Qwen-Image-2.1).

Two request shapes share one renderer:

* text-to-image -- the original five-lesson shape ``{prompt, size, steps, seed}``. A request without an
  ``operation`` key is exactly this, so already-queued work keeps its meaning and its receipt.
* image-edit    -- ``{operation, prompt, size, steps, seed, references, mask, transparent}``. The condition
  images are staged next to the request under fixed names, never at a caller-chosen path, and the request
  carries only counts and flags.
"""
import json

SIZES = ('1024*1024', '1280*768', '768*1280')
# Qwen-Image-2.1 is a full (non-distilled) sampler: the published recipe runs 40 steps.
MIN_STEPS, MAX_STEPS = 4, 50
# The pipeline conditions on one set of images: the edited source plus at most ten further references.
MAX_REFERENCES = 10
EDIT = 'image-edit'
TEXT = 'text-to-image'
TEXT_KEYS = {'prompt', 'size', 'steps', 'seed'}
EDIT_KEYS = {'operation', 'prompt', 'size', 'steps', 'seed', 'references', 'mask', 'transparent'}
SOURCE_NAME = 'source.png'
MASK_NAME = 'mask.png'
REFERENCE_PREFIX = 'reference-'


def reference_name(index):
    """Fixed staging name for the reference at ``index``; callers never choose this path."""
    return f'{REFERENCE_PREFIX}{index}.png'


def reference_names(count):
    return [reference_name(index) for index in range(count)]


def is_edit(request):
    return request.get('operation') == EDIT


def decode_request(data):
    if type(data) is not bytes or len(data) > 65536:
        raise ValueError('Invalid image request size.')
    request = json.loads(data)
    if type(request) is not dict:
        raise ValueError('Invalid image prompt, size, steps or seed.')
    if is_edit(request):
        if (set(request) != EDIT_KEYS
                or type(request['references']) is not int or not 0 <= request['references'] <= MAX_REFERENCES
                or type(request['mask']) is not bool or type(request['transparent']) is not bool):
            raise ValueError('Invalid image edit request.')
    elif set(request) != TEXT_KEYS:
        raise ValueError('Invalid image prompt, size, steps or seed.')
    if (type(request['prompt']) is not str or not request['prompt'].strip() or len(request['prompt']) > 600
            or request['size'] not in SIZES
            or type(request['steps']) is not int or not MIN_STEPS <= request['steps'] <= MAX_STEPS
            or type(request['seed']) is not int or not 0 <= request['seed'] < 2**32):
        raise ValueError('Invalid image prompt, size, steps or seed.')
    return request
