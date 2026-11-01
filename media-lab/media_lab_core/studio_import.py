"""Bounded uploads to the owner's independent host, never a central service."""
import asyncio
from pathlib import Path
import tempfile
import threading

from fastapi import APIRouter, HTTPException, Request
from starlette.concurrency import run_in_threadpool

MAX_BYTES = 256 * 1024**2
SUFFIXES = {'.png', '.jpg', '.jpeg', '.webp', '.mp4', '.mov', '.webm', '.mkv',
            '.mp3', '.wav', '.m4a', '.flac', '.ogg'}


def router(root, credentials):
    api = APIRouter()
    admission = threading.BoundedSemaphore(1)

    @api.post('/api/studio/library/import')
    async def upload(request: Request, filename: str, title: str = ''):
        editing = request.headers.get('authorization', '')
        library = request.headers.get('x-library-authorization', '')
        if (not editing.startswith('Bearer ') or not credentials.authorize_editing(editing[7:])
                or not library.startswith('Bearer ') or not credentials.authorize_library(library[7:])):
            raise HTTPException(401, 'Pair Library and editing before adding server files.')
        if len(filename) > 240 or Path(filename).name != filename or '\\' in filename:
            raise HTTPException(422, 'Choose a plain media filename.')
        suffix = Path(filename).suffix.lower()
        if suffix not in SUFFIXES or len(title) > 240:
            raise HTTPException(422, 'Choose a supported image, video or audio file and a short title.')
        length = request.headers.get('content-length')
        if length is not None and (not length.isdigit() or not 0 < int(length) <= MAX_BYTES):
            raise HTTPException(413, 'Choose a media file up to 256 MiB.')
        if not admission.acquire(blocking=False):
            raise HTTPException(429, 'Another file is being added. Try again shortly.')
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(dir=root/'state', suffix=suffix, delete=False) as writer:
                temporary = Path(writer.name)
                size = 0
                try:
                    async with asyncio.timeout(180):
                        async for chunk in request.stream():
                            size += len(chunk)
                            if size > MAX_BYTES:
                                raise HTTPException(413, 'Choose a media file up to 256 MiB.')
                            await run_in_threadpool(writer.write, chunk)
                except TimeoutError:
                    raise HTTPException(408, 'Upload took too long. Try a smaller file.') from None
            if size == 0:
                raise HTTPException(422, 'The selected file is empty.')
            from .studio_cli import import_media
            try:
                return await run_in_threadpool(import_media, root, temporary, title.strip() or Path(filename).stem)
            except ValueError:
                raise HTTPException(422, 'Could not import this media. Check the file and server FFprobe installation.') from None
            except OSError:
                raise HTTPException(503, 'The server could not save the file. Check its available storage.') from None
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
            admission.release()

    return api
