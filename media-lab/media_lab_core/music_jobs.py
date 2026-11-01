"""Music queue adapter (ACE-Step); the host supplies the exact revision and a bounded executor."""
import json
from .cpu_jobs import run_next_cpu
from .music_request import MIN_SECONDS, MAX_SECONDS

ENGINE = 'acestep-gpu'


def validate_payload(payload, revision):
    if (payload.get('engineId'), payload.get('revision'), payload.get('kind')) != (ENGINE, revision, 'audio'):
        raise ValueError('The exact music engine is unavailable.')
    prompt = payload.get('prompt')
    settings = payload.get('settings')
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 600:
        raise ValueError('Music requires a bounded nonempty description.')
    if (type(settings) is not dict or set(settings) != {'operation', 'lyrics', 'seconds', 'seed'}
            or settings['operation'] != 'compose' or type(settings['lyrics']) is not str or len(settings['lyrics']) > 4000
            or type(settings['seconds']) is not int or not MIN_SECONDS <= settings['seconds'] <= MAX_SECONDS
            or type(settings['seed']) is not int or not 0 <= settings['seed'] < 2**32):
        raise ValueError('Explicit lyrics (or [inst]), length and seed are required.')


def run_next(store, *, root, revision, execute):
    if not isinstance(revision, str) or not revision or len(revision) > 128:
        raise ValueError('An exact host revision is required.')

    def prepare(job):
        payload = job['payload']; settings = payload['settings']
        return json.dumps({'prompt': payload['prompt'], 'lyrics': settings['lyrics'], 'seconds': settings['seconds'],
                           'seed': settings['seed']}, ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode('utf-8')

    return run_next_cpu(store, root=root, engine=ENGINE, kind='audio', stage='composing-music',
        validate=lambda payload: validate_payload(payload, revision), prepare_input=prepare,
        execute=execute, failure_message='Music generation did not produce an accepted result.')
