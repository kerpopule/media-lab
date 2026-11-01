"""Experimental durable CPU queue controller; not automatically registered.

The host supplies an owner-checked immutable input resolver and an explicitly
qualified runtime. No legacy engine, URL fetch, or provider fallback exists.
"""
import json
from pathlib import Path
import re

from .birefnet_cpu import MANIFEST
from .cpu_worker import run_background_job

ENGINE = 'birefnet-cpu'


def validate_payload(payload):
    manifest = json.loads(MANIFEST.read_text())
    settings = payload.get('settings', {})
    if (payload.get('engineId'), payload.get('revision'), payload.get('kind')) != (
            ENGINE, manifest['revision'], 'image'):
        raise ValueError('The exact background-removal model is unavailable.')
    if (not isinstance(settings, dict) or set(settings) != {'operation', 'inputId', 'inputSha256'}
            or settings.get('operation') != 'remove-background'
            or not isinstance(settings.get('inputId'), str)
            or not isinstance(settings.get('inputSha256'), str)
            or not re.fullmatch(r'[a-f0-9]{32}', settings.get('inputId', ''))
            or not re.fullmatch(r'[a-f0-9]{64}', settings.get('inputSha256', ''))):
        raise ValueError('A verified immutable image input is required.')


def run_next(store, *, root: Path, runtime: Path, package: Path, cache: Path, read_input=None,
             execute=run_background_job):
    """Execute at most one owned job. Return its terminal record, or None.

    read_input(job) must resolve only that job owner's accepted inputId. It
    must not fetch arbitrary URLs or return mutable Library paths. Input hashes
    are checked again here. Network/API integration is deliberately separate.
    """
    from .cpu_jobs import run_next_cpu

    def run(**kwargs):
        return execute(**kwargs, runtime=runtime, package=package, cache=cache)

    return run_next_cpu(store, root=root, engine=ENGINE, kind='image',
                        stage='removing-background', validate=validate_payload,
                        execute=run, read_input=read_input,
                        failure_message='Background removal did not produce an accepted result.')
