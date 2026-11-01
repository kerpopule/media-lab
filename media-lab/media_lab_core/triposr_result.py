"""Verify candidate worker provenance and return the exact GLB bytes to publish.
Geometry decode remains the isolated worker's responsibility.
"""
import hashlib
import json
import math
import os
from pathlib import Path
import re
import stat
from .triposr_compatibility import expected_runtime_receipt
from .glb_contract import MAX_BYTES, inspect_generated_glb
from .triposr_cpu import MODEL_SHA, SOURCE_RECEIPT_SHA, VARIANT


def _read_at(directory_fd, name, limit):
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= limit:
            raise ValueError('Worker result must be a bounded regular file.')
        data = stream.read(limit + 1)
    if not 0 < len(data) <= limit:
        raise ValueError('Worker result exceeds its size limit.')
    return data


def verify_triposr_result(directory: Path, input_sha256: str, *, runtime_profile='original') -> tuple[dict, bytes]:
    """Caller must retain private directory ownership and await successful child exit."""
    if not re.fullmatch('[0-9a-f]{64}', input_sha256):
        raise ValueError('A verified input SHA-256 is required.')
    fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        receipt = json.loads(_read_at(fd, 'receipt.json', 16384))
        data = _read_at(fd, 'output.glb', MAX_BYTES)
    finally:
        os.close(fd)
    if not isinstance(receipt, dict):
        raise ValueError('Worker receipt must be an object.')
    expected = dict(variant=VARIANT, device='cpu', dtype='float32', model_sha256=MODEL_SHA,
                    source_receipt_sha256=SOURCE_RECEIPT_SHA, input_sha256=input_sha256,
                    creative_status='draft', install_qualified=False, seed=7, resolution=128, bytes=len(data))
    if any(type(receipt.get(k)) is not type(v) or receipt[k] != v for k, v in expected.items()):
        raise ValueError('Worker result does not match the requested input and variant.')
    if receipt.get('runtime') != expected_runtime_receipt(profile=runtime_profile):
        raise ValueError('Worker result lacks the reviewed runtime provenance.')
    for key, maximum in [('vertices', 1000000), ('triangles', 2000000)]:
        if type(receipt.get(key)) is not int or not 1 <= receipt[key] <= maximum:
            raise ValueError('Worker geometry receipt exceeds bounds.')
    seconds = receipt.get('seconds')
    if type(seconds) not in (int, float) or not math.isfinite(seconds) or seconds < 0:
        raise ValueError('Worker timing receipt is invalid.')
    if receipt.get('output_sha256') != hashlib.sha256(data).hexdigest():
        raise ValueError('Worker output failed its integrity check.')
    inspect_generated_glb(data)
    return receipt, data
