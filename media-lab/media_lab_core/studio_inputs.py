"""Snapshot a Library image using both Library and device job permissions."""
import io
import threading
from PIL import Image, ImageOps
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field

_SNAPSHOT_SLOTS = threading.BoundedSemaphore(2)


class LibraryInput(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    assetId: str = Field(pattern=r'^[A-Za-z0-9_-]{1,128}$')


def validate_image(data):
    if not data or len(data) > 20 * 1024**2:
        raise ValueError('Choose an image no larger than 20 MiB.')
    try:
        with Image.open(io.BytesIO(data)) as image:
            if (image.format not in ('PNG', 'JPEG', 'WEBP') or getattr(image, 'n_frames', 1) != 1
                    or image.width * image.height > 16_000_000):
                raise ValueError('Choose a single PNG, JPEG or WebP of at most 16 megapixels.')
            image.load()
            return ImageOps.exif_transpose(image).size
    except (OSError, Image.DecompressionBombError):
        raise ValueError('This image could not be decoded safely.') from None


def router(get_store, authorize, authorize_library, read_library):
    api = APIRouter()

    @api.post('/api/studio/inputs/library')
    def snapshot(body: LibraryInput, request: Request):
        auth = request.headers.get('authorization', '')
        owner = authorize(auth[7:]) if auth.startswith('Bearer ') else None
        if not owner:
            raise HTTPException(401, 'Connect this device with generation permission.')
        library = request.headers.get('x-library-authorization', '')
        if not library.startswith('Bearer ') or not authorize_library(library[7:]):
            raise HTTPException(401, 'Connect this device with Library permission.')
        if not _SNAPSHOT_SLOTS.acquire(blocking=False):
            raise HTTPException(429, 'Image preparation is busy. Try again shortly.')
        try:
            data = read_library(body.assetId)
            width, height = validate_image(data)
            result = get_store().put_input(owner, data, width=width, height=height)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from None
        finally:
            _SNAPSHOT_SLOTS.release()
        return {'version': 1, **result}

    return api


def validate_reference(store, owner, payload):
    """Every accepted-image reference a payload names must be this device's own snapshot, by id and digest.

    Text-to-image names none. An edit names a source, up to ten further references and an optional mask; each is
    checked the same way, so a job can never reach the renderer carrying an image its owner was not granted.
    """
    settings = payload.get('settings', {})
    declared = []
    if 'inputId' in settings:
        declared.append((settings.get('inputId'), settings.get('inputSha256')))
    if 'sourceId' in settings:
        declared.append((settings.get('sourceId'), settings.get('sourceSha256')))
    for entry in settings.get('references') or []:
        if isinstance(entry, dict):
            declared.append((entry.get('inputId'), entry.get('inputSha256')))
    if isinstance(settings.get('mask'), dict):
        declared.append((settings['mask'].get('inputId'), settings['mask'].get('inputSha256')))
    for input_id, digest in declared:
        item = store.input_metadata(owner, input_id) if isinstance(input_id, str) else None
        if item is None or item['sha256'] != digest:
            raise HTTPException(404, 'This accepted image is not available to this device.')
