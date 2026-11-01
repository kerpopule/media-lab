"""Candidate 3D queue entry point; no automatic engine registration.
The host must supply a qualified, bounded executor with verified publication.
"""
import re
from .cpu_jobs import run_next_cpu
from .triposr_cpu import MODEL_SHA, VARIANT

ENGINE = 'triposr-cpu'


def validate_payload(payload):
    if (payload.get('engineId'), payload.get('revision'), payload.get('kind')) != (ENGINE, MODEL_SHA, 'model'):
        raise ValueError('The exact 3D reconstruction model is unavailable.')
    settings = payload.get('settings')
    if (not isinstance(settings, dict) or set(settings) != {'operation', 'variant', 'inputId', 'inputSha256'}
            or settings.get('operation') != 'image-to-3d' or settings.get('variant') != VARIANT
            or not isinstance(settings.get('inputId'), str)
            or not isinstance(settings.get('inputSha256'), str)
            or not re.fullmatch('[a-f0-9]{32}', settings['inputId'])
            or not re.fullmatch('[a-f0-9]{64}', settings['inputSha256'])):
        raise ValueError('A verified cutout and the exact 3D variant are required.')


def run_next(store, *, root, execute, read_input=None):
    return run_next_cpu(store, root=root, engine=ENGINE, kind='model',
                        stage='reconstructing-3d', validate=validate_payload,
                        execute=execute, read_input=read_input,
                        failure_message='3D reconstruction did not produce an accepted result.')
