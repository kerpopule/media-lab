"""Image queue adapter (Qwen-Image-2.1); the host supplies the exact revision and a bounded executor.

Two operations are admitted, and each has one exact settings shape:

* ``text-to-image`` -- ``{operation, size, steps, seed}`` with no condition images.
* ``image-edit``    -- ``{operation, size, steps, seed, sourceId, sourceSha256, references, mask, transparent}``.
  Every condition image is an accepted Library snapshot identified by id *and* digest, exactly as background
  removal already requires, and ``references`` carries at most ten of them.

The prepared renderer request carries counts and flags only; the bytes travel as staged files behind the queue's
owner-checked input reader, so no caller ever names a path the renderer reads.
"""
import json
import re

from .cpu_jobs import run_next_cpu
from .image_request import (EDIT, MAX_REFERENCES, MIN_STEPS, MAX_STEPS, SIZES, TEXT)

ENGINE = 'qwen-image-21-gpu'
TEXT_KEYS = {'operation', 'size', 'steps', 'seed'}
EDIT_SETTINGS = {'operation', 'size', 'steps', 'seed', 'sourceId', 'sourceSha256', 'references', 'mask', 'transparent'}
HEX_ID = re.compile(r'[a-f0-9]{32}')
HEX_SHA = re.compile(r'[a-f0-9]{64}')


def _reference(entry):
    """One accepted-image reference: exact keys, id and digest shape checked, never a path."""
    return (type(entry) is dict and set(entry) == {'inputId', 'inputSha256'}
            and isinstance(entry['inputId'], str) and bool(HEX_ID.fullmatch(entry['inputId']))
            and isinstance(entry['inputSha256'], str) and bool(HEX_SHA.fullmatch(entry['inputSha256'])))


def _bounded(settings):
    return (type(settings) is dict and settings.get('size') in SIZES
            and type(settings.get('steps')) is int and MIN_STEPS <= settings['steps'] <= MAX_STEPS
            and type(settings.get('seed')) is int and 0 <= settings['seed'] < 2**32)


def validate_payload(payload, revision):
    if (payload.get('engineId'), payload.get('revision'), payload.get('kind')) != (ENGINE, revision, 'image'):
        raise ValueError('The exact image engine is unavailable.')
    prompt = payload.get('prompt')
    settings = payload.get('settings')
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 600:
        raise ValueError('Image generation requires a bounded nonempty description.')
    if not _bounded(settings):
        raise ValueError('Explicit size, steps and seed are required.')
    if set(settings) == TEXT_KEYS and settings['operation'] == TEXT:
        return
    if set(settings) == EDIT_SETTINGS and settings['operation'] == EDIT:
        references = settings['references']
        if (type(references) is not list or len(references) > MAX_REFERENCES
                or not all(_reference(entry) for entry in references)
                or not _reference({'inputId': settings['sourceId'], 'inputSha256': settings['sourceSha256']})
                or type(settings['transparent']) is not bool
                or not (settings['mask'] is None or _reference(settings['mask']))):
            raise ValueError('An image edit needs a verified source image and at most ten verified references.')
        return
    raise ValueError('Explicit size, steps and seed are required.')


def prepare_request(job):
    """The renderer request: the prompt plus, for an edit, the condition counts and flags only."""
    payload = job['payload']; settings = payload['settings']
    request = {'prompt': payload['prompt'], 'size': settings['size'], 'steps': settings['steps'], 'seed': settings['seed']}
    if settings['operation'] == EDIT:
        request = {'operation': EDIT, **request, 'references': len(settings['references']),
                   'mask': settings['mask'] is not None, 'transparent': settings['transparent']}
    return request


def run_next(store, *, root, revision, execute):
    if not isinstance(revision, str) or not revision or len(revision) > 128:
        raise ValueError('An exact host revision is required.')

    def prepare(job):
        return json.dumps(prepare_request(job), ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode('utf-8')

    return run_next_cpu(store, root=root, engine=ENGINE, kind='image', stage='rendering-image',
        validate=lambda payload: validate_payload(payload, revision), prepare_input=prepare,
        execute=execute, failure_message='Image generation did not produce an accepted result.')
