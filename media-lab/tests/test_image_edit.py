"""Qwen-Image-2.1 editing: request contract, owner-checked staging, receipt v2, paste-back mask, transparency."""
import hashlib
import io
import json
import sys
import types
from pathlib import Path

import pytest
from PIL import Image

from media_lab_core import image_jobs, image_render, image_worker
from media_lab_core.image_artifact import inspect_png
from media_lab_core.image_request import (
    EDIT,
    MASK_NAME,
    SOURCE_NAME,
    decode_request,
    reference_name,
    reference_names,
)
from media_lab_core.job_store import JobStore
from media_lab_core.studio_inputs import validate_reference

OWNER = 'a' * 32
PROMPT = 'make the coat bright red'


def png(size=(1024, 1024), mode='RGB', colour=(10, 20, 30)):
    buffer = io.BytesIO(); Image.new(mode, size, colour).save(buffer, format='PNG'); return buffer.getvalue()


def request(**over):
    base = {'operation': EDIT, 'prompt': PROMPT, 'size': '1024*1024', 'steps': 9, 'seed': 7, 'references': 1,
            'mask': False, 'transparent': True}
    return {**base, **over}


def request_bytes(**over):
    return json.dumps(request(**over), sort_keys=True, separators=(',', ':')).encode()


def settings(source, reference):
    return {'operation': EDIT, 'size': '1024*1024', 'steps': 9, 'seed': 7,
            'sourceId': source['id'], 'sourceSha256': source['sha256'],
            'references': [{'inputId': reference['id'], 'inputSha256': reference['sha256']}],
            'mask': None, 'transparent': True}


def payload(source, reference):
    return {'engineId': image_jobs.ENGINE, 'revision': 'r', 'kind': 'image', 'prompt': PROMPT,
            'settings': settings(source, reference)}


def staged(tmp_path):
    store = JobStore(tmp_path / 'jobs.sqlite')
    source = store.put_input(OWNER, png(), width=1024, height=1024)
    reference = store.put_input(OWNER, png(colour=(200, 30, 40)), width=1024, height=1024)
    jid = store.enqueue_once(OWNER, 'request-image-edit-0001', 'image', payload(source, reference))
    return store, jid, source, reference


class Resident:
    """Minimal owned-renderer stub: writes the result and the receipt the worker is expected to verify."""

    def __init__(self, mode='RGBA'):
        self.mode = mode
        self.calls = 0

    def render(self, *, input_path, output_dir, timeout, cancelled=lambda: False, job_label='image'):
        self.calls += 1
        data = Path(input_path).read_bytes()
        body = decode_request(data)
        accepted = Path(output_dir)
        Image.new(self.mode, (1024, 1024), (5, 6, 7) if self.mode == 'RGB' else (5, 6, 7, 255)).save(accepted / 'output.png', format='PNG')
        metadata = inspect_png((accepted / 'output.png').read_bytes(), require_alpha=body['transparent'])
        receipt = image_worker.expected_receipt(hashlib.sha256(data).hexdigest(), 'rev', body, metadata)
        (accepted / 'receipt.json').write_text(json.dumps(receipt, sort_keys=True))


def test_request_contract_admits_edits_and_keeps_text_requests_unchanged():
    body = decode_request(request_bytes())
    assert body['references'] == 1 and body['transparent'] is True
    text = decode_request(json.dumps({'prompt': 'a cat', 'size': '1024*1024', 'steps': 9, 'seed': 7}).encode())
    assert 'operation' not in text
    for bad in (request(references=11), request(references=-1), request(references='1'), request(mask=1),
                request(transparent='yes'), request(operation='compose'), {k: v for k, v in request().items() if k != 'mask'},
                {**request(), 'extra': 1}):
        with pytest.raises(ValueError):
            decode_request(json.dumps(bad).encode())


def test_only_the_exact_edit_settings_shape_is_admitted(tmp_path):
    _store, _jid, source, reference = staged(tmp_path)
    image_jobs.validate_payload(payload(source, reference), 'r')
    # Zero further references is a valid edit: the source image alone is the condition.
    image_jobs.validate_payload({**payload(source, reference),
                                 'settings': {**settings(source, reference), 'references': []}}, 'r')
    bad_shapes = (
        {'references': [{'inputId': reference['id']}]},
        {'references': [{'inputId': reference['id'], 'inputSha256': 'x'}]},
        {'references': [{'inputId': reference['id'], 'inputSha256': reference['sha256']}] * 11},
        {'sourceId': 'not-hex'}, {'transparent': 'yes'}, {'mask': {'inputId': reference['id']}},
        {'mask': {'inputId': reference['id'], 'inputSha256': reference['sha256'], 'extra': 1}},
    )
    for over in bad_shapes:
        with pytest.raises(ValueError):
            image_jobs.validate_payload({**payload(source, reference), 'settings': {**settings(source, reference), **over}}, 'r')


def test_prepared_request_carries_counts_and_flags_only(tmp_path):
    store, jid, _source, _reference = staged(tmp_path)
    body = image_jobs.prepare_request(store.get(jid))
    assert body == {'operation': EDIT, 'prompt': PROMPT, 'size': '1024*1024', 'steps': 9, 'seed': 7,
                    'references': 1, 'mask': False, 'transparent': True}
    assert set(body) == {'operation', 'prompt', 'size', 'steps', 'seed', 'references', 'mask', 'transparent'}


def test_staging_reads_only_this_jobs_accepted_inputs(tmp_path):
    store, jid, _source, _reference = staged(tmp_path)
    body = decode_request(request_bytes())
    assert image_worker.stage_names(body) == [SOURCE_NAME, reference_name(0)]
    assert image_worker.stage_names(decode_request(request_bytes(mask=True))) == [SOURCE_NAME, reference_name(0), MASK_NAME]
    assert image_worker.stage_names(decode_request(json.dumps({'prompt': 'a cat', 'size': '1024*1024', 'steps': 9, 'seed': 7}).encode())) == []
    files = image_worker.edit_sources(store, jid, body)
    assert files[SOURCE_NAME].startswith(b'\x89PNG') and files[reference_name(0)].startswith(b'\x89PNG')
    for change in ({'sourceSha256': 'f' * 64}, {'references': []}):
        current = store.get(jid)['payload']
        mutated = {**current, 'settings': {**current['settings'], **change}}
        with pytest.raises(ValueError):
            image_worker.edit_sources(FakeStore(store, mutated), jid, body)


class FakeStore:
    def __init__(self, store, payload):
        self._store = store
        self._payload = payload

    def get(self, job_id):
        return {**self._store.get(job_id), 'payload': self._payload}

    def read_job_input(self, job_id, input_id):
        return self._store.read_job_input(job_id, input_id)


def test_run_publishes_a_verified_transparent_edit(tmp_path):
    store, jid, _source, _reference = staged(tmp_path)
    data = request_bytes()
    result = image_worker.run_image_job(job_id=jid, data=data, root=tmp_path / 'results', resident=Resident(),
                                        inference_lock=tmp_path / 'lock', revision='rev', store=store)
    assert (result['path'], result['recovered'], result['mode'], result['width']) == (jid + '/output.png', False, 'RGBA', 1024)
    assert (tmp_path / 'results' / jid / 'output.png').is_file()
    assert not list((tmp_path / 'results').glob(f'.{jid}-*'))


def test_transparent_request_fails_closed_when_the_sampler_is_opaque(tmp_path):
    store, jid, _source, _reference = staged(tmp_path)
    with pytest.raises(ValueError):
        image_worker.run_image_job(job_id=jid, data=request_bytes(), root=tmp_path / 'results', resident=Resident('RGB'),
                                   inference_lock=tmp_path / 'lock', revision='rev', store=store)
    assert not (tmp_path / 'results' / jid).exists()


def test_png_gate_requires_alpha_only_when_transparency_was_asked_for():
    assert inspect_png(png())['mode'] == 'RGB'
    assert inspect_png(png(mode='RGBA'))['mode'] == 'RGBA'
    with pytest.raises(ValueError):
        inspect_png(png(), require_alpha=True)
    inspect_png(png(mode='RGBA'), require_alpha=True)


def install_torch(monkeypatch):
    monkeypatch.setitem(sys.modules, 'torch', types.SimpleNamespace(
        Generator=lambda device: types.SimpleNamespace(manual_seed=lambda seed: None)))


def render_edit(tmp_path, monkeypatch, pipe, mask=False, transparent=True, references=1):
    install_torch(monkeypatch)
    directory = tmp_path / 'stage'; directory.mkdir()
    (directory / 'input.json').write_bytes(request_bytes(references=references, mask=mask, transparent=transparent))
    (directory / SOURCE_NAME).write_bytes(png(colour=(10, 20, 30)))
    for name in reference_names(references):
        (directory / name).write_bytes(png(colour=(60, 70, 80)))
    if mask:
        board = Image.new('L', (1024, 1024), 0)
        board.paste(255, (0, 0, 512, 1024))
        board.save(directory / MASK_NAME, format='PNG')
    output = tmp_path / 'out'; output.mkdir()
    image_render.render(pipe, directory / 'input.json', output, 'rev')
    return output


class Pipe:
    def __init__(self, mode='RGBA', colour=(200, 0, 0, 255)):
        self.mode, self.colour, self.kwargs = mode, colour, None

    def __call__(self, **kwargs):
        self.kwargs = kwargs
        return types.SimpleNamespace(images=[Image.new(self.mode, (1024, 1024), self.colour)])


def test_edit_conditions_the_sampler_on_the_source_plus_every_reference(tmp_path, monkeypatch):
    pipe = Pipe()
    output = render_edit(tmp_path, monkeypatch, pipe, references=3)
    conditions = pipe.kwargs['image']
    assert isinstance(conditions, list) and len(conditions) == 4
    assert all(condition.mode == 'RGBA' for condition in conditions)
    assert pipe.kwargs['prompt'] == PROMPT and pipe.kwargs['true_cfg_scale'] == 1.0 and pipe.kwargs['use_kv_cache'] is True
    assert inspect_png((output / 'output.png').read_bytes(), require_alpha=True)['mode'] == 'RGBA'
    receipt = json.loads((output / 'receipt.json').read_text())
    assert (receipt['version'], receipt['references'], receipt['mask'], receipt['transparent']) == (2, 3, False, True)
    image_worker.verify_result(output, receipt['inputSha256'], 'rev', request(references=3))


def test_mask_is_a_deterministic_paste_back_not_a_claim_of_inpainting(tmp_path, monkeypatch):
    output = render_edit(tmp_path, monkeypatch, Pipe(), mask=True)
    with Image.open(output / 'output.png') as image:
        assert image.getpixel((10, 512))[:3] == (200, 0, 0)   # inside the mask: sampled pixels
        assert image.getpixel((1010, 512))[:3] == (10, 20, 30)  # outside the mask: untouched source pixels
    receipt = json.loads((output / 'receipt.json').read_text())
    assert receipt['mask'] is True


def test_opaque_edit_is_saved_without_an_alpha_channel(tmp_path, monkeypatch):
    output = render_edit(tmp_path, monkeypatch, Pipe(), transparent=False)
    assert inspect_png((output / 'output.png').read_bytes())['mode'] == 'RGB'
    with pytest.raises(ValueError):
        inspect_png((output / 'output.png').read_bytes(), require_alpha=True)


def test_edit_fails_closed_when_the_sampler_cannot_deliver_transparency(tmp_path, monkeypatch):
    with pytest.raises(ValueError, match='alpha'):
        render_edit(tmp_path, monkeypatch, Pipe(mode='RGB', colour=(200, 0, 0)), transparent=True)


def test_validate_reference_covers_source_references_and_mask(tmp_path):
    from fastapi import HTTPException
    store, _jid, source, reference = staged(tmp_path)
    validate_reference(store, OWNER, payload(source, reference))
    for change in ({'sourceSha256': 'f' * 64}, {'sourceId': 'b' * 32},
                   {'references': [{'inputId': reference['id'], 'inputSha256': 'f' * 64}]},
                   {'references': [{'inputId': 'e' * 32, 'inputSha256': reference['sha256']}]},
                   {'mask': {'inputId': 'e' * 32, 'inputSha256': 'f' * 64}}):
        with pytest.raises(HTTPException):
            validate_reference(store, OWNER, {**payload(source, reference),
                                             'settings': {**settings(source, reference), **change}})
