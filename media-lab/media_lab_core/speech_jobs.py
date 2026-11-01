"""Development speech queue adapter; no automatic engine registration.

A qualified host must provide an exact revision and bounded verified executor.
This module does not advertise readiness or enable the experimental speech model.
"""
import json
from .cpu_jobs import run_next_cpu
from .speech_request import VOICE

ENGINE = 'chatterbox-english-cpu'


def validate_payload(payload, revision):
    if (payload.get('engineId'), payload.get('revision'), payload.get('kind')) != (ENGINE, revision, 'audio'):
        raise ValueError('The exact speech engine is unavailable.')
    prompt = payload.get('prompt')
    settings = payload.get('settings')
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 4000:
        raise ValueError('Speech requires bounded nonempty text.')
    if (type(settings) is not dict or set(settings) != {'operation', 'voice', 'seed'}
            or settings['operation'] != 'speak' or settings['voice'] != VOICE
            or type(settings['seed']) is not int or not 0 <= settings['seed'] < 2**32):
        raise ValueError('Explicit supported voice and seed are required.')


def run_next(store, *, root, revision, execute):
    if not isinstance(revision, str) or not revision or len(revision) > 128:
        raise ValueError('An exact host revision is required.')

    def prepare(job):
        payload = job['payload']
        return json.dumps({'text':payload['prompt'], 'voice':payload['settings']['voice'],
                           'seed':payload['settings']['seed']}, ensure_ascii=True,
                          sort_keys=True, separators=(',', ':')).encode('utf-8')

    return run_next_cpu(store, root=root, engine=ENGINE, kind='audio', stage='generating-speech',
        validate=lambda payload: validate_payload(payload, revision), prepare_input=prepare,
        execute=execute, failure_message='Speech generation did not produce an accepted result.')
