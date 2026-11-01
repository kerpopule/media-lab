"""Video queue adapter (Wan2.2 TI2V-5B); the host supplies the exact revision and a bounded executor."""
import json
from .cpu_jobs import run_next_cpu
from .video_request import MIN_FRAMES, MAX_FRAMES, MIN_STEPS, MAX_STEPS, SIZES

ENGINE = 'wan22-ti2v-5b-gpu'


def validate_payload(payload, revision):
    if (payload.get('engineId'), payload.get('revision'), payload.get('kind')) != (ENGINE, revision, 'video'):
        raise ValueError('The exact video engine is unavailable.')
    prompt = payload.get('prompt')
    settings = payload.get('settings')
    if not isinstance(prompt, str) or not prompt.strip() or len(prompt) > 600:
        raise ValueError('Video requires a bounded nonempty description.')
    if (type(settings) is not dict or set(settings) != {'operation', 'frames', 'size', 'steps', 'seed'}
            or settings['operation'] != 'text-to-video'
            or type(settings['frames']) is not int or not MIN_FRAMES <= settings['frames'] <= MAX_FRAMES or settings['frames'] % 4 != 1
            or settings['size'] not in SIZES
            or type(settings['steps']) is not int or not MIN_STEPS <= settings['steps'] <= MAX_STEPS
            or type(settings['seed']) is not int or not 0 <= settings['seed'] < 2**32):
        raise ValueError('Explicit frame count (4n+1), size, steps and seed are required.')


def run_next(store, *, root, revision, execute):
    if not isinstance(revision, str) or not revision or len(revision) > 128:
        raise ValueError('An exact host revision is required.')

    def prepare(job):
        payload = job['payload']; settings = payload['settings']
        return json.dumps({'prompt': payload['prompt'], 'frames': settings['frames'], 'size': settings['size'],
                           'steps': settings['steps'], 'seed': settings['seed']},
                          ensure_ascii=True, sort_keys=True, separators=(',', ':')).encode('utf-8')
    return run_next_cpu(store, root=root, engine=ENGINE, kind='video', stage='rendering-video',
        validate=lambda payload: validate_payload(payload, revision), prepare_input=prepare,
        execute=execute, failure_message='Video generation did not produce an accepted result.')
