"""Device-scoped generation API, independent of the legacy render controller.

Admission is supplied by the qualified adapter host. No host means no engines;
this module never substitutes a legacy renderer or a paid provider.
"""
import hashlib
import hmac
import json
import re
import time
import mimetypes
import threading
from pathlib import Path
from typing import Literal

from fastapi import APIRouter, HTTPException, Request, Query
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, ConfigDict, Field

from .job_store import RequestConflict
from .studio_inputs import validate_reference
from .studio_library import thumbnail

_PREVIEW_SLOTS = threading.BoundedSemaphore(2)

TOKEN_AGE = 30 * 24 * 3600


def ticket(secret, role, code, device_id, now=None):
    if role not in ('user', 'admin') or not re.fullmatch(r'[a-f0-9]{32}', device_id or ''):
        raise ValueError('A valid device identity is required.')
    prefix = f'mlab-render-v1.{role}.{int(time.time() if now is None else now)}.{device_id}'
    signature = hmac.new(secret.encode(), f'{prefix}:{code}'.encode(), hashlib.sha256).hexdigest()
    return f'{prefix}.{signature}'


def identity(raw, secret, role_code, now=None, max_age=None):
    """``max_age`` lets a host keep passes longer than TOKEN_AGE (the family
    studio uses a year); rotating the role's code still revokes them at once."""
    match = re.fullmatch(r'(mlab-render-v1\.(user|admin)\.(\d{10,12})\.([a-f0-9]{32}))\.([a-f0-9]{64})', raw or '')
    if not match:
        return None
    prefix, role, issued, device_id, signature = match.groups()
    age = (time.time() if now is None else now) - int(issued)
    expected = hmac.new(secret.encode(), f'{prefix}:{role_code(role)}'.encode(), hashlib.sha256).hexdigest()
    limit = TOKEN_AGE if max_age is None else max_age
    return device_id if -300 <= age <= limit and hmac.compare_digest(signature, expected) else None


def is_jobs_path(path):
    return path in ('/api/studio/engines', '/api/studio/jobs', '/api/studio/inputs/library', '/api/studio/requests/cancel') or bool(
        re.fullmatch(r'/api/studio/jobs/[a-f0-9]{32}(?:/content|/preview|/input|/cancel|/library)?', path))


class Generation(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    requestId: str = Field(pattern=r'^[A-Za-z0-9_-]{16,128}$')
    engineId: str = Field(pattern=r'^[A-Za-z0-9_-]{1,80}$')
    revision: str = Field(min_length=1, max_length=128)
    kind: Literal['image', 'video', 'audio', 'model', 'sprites']
    prompt: str = Field(min_length=1, max_length=4000)
    settings: dict = Field(default_factory=dict)


def public_job(job):
    # Result paths, engine diagnostics, credentials and other users' requests
    # never become queue metadata. Artifact retrieval is a separate contract.
    prompt = job.get('payload', {}).get('prompt')
    title = prompt.strip()[:240] if isinstance(prompt, str) else ''
    return {'id': job['id'], 'kind': job['kind'], 'status': job['status'], 'title': title,
            'createdAt': int(job['created_at'] * 1000),
            'updatedAt': int(job['updated_at'] * 1000)}


def router(get_store, authorize, engines, admit, artifact_root=None, save_image=None, save_audio=None, save_video=None, save_generated_image=None):
    api = APIRouter()

    def require(request):
        auth = request.headers.get('authorization', '')
        owner = authorize(auth[7:]) if auth.startswith('Bearer ') else None
        if not owner:
            raise HTTPException(401, 'Connect this device with generation permission.')
        return owner

    @api.get('/api/studio/engines')
    def capabilities(request: Request):
        require(request)
        return {'version': 1, 'engines': engines()}

    @api.get('/api/studio/jobs')
    def history(request: Request, limit: int = Query(50, ge=1, le=100),
                before: str | None = Query(None, pattern=r'^[a-f0-9]{32}$')):
        owner = require(request)
        try:
            rows = get_store().list_owned(owner, limit=limit + 1, before=before)
        except ValueError:
            raise HTTPException(404, 'This history cursor is unavailable.') from None
        page = rows[:limit]
        return {'version': 1, 'jobs': [public_job(job) for job in page],
                'nextCursor': page[-1]['id'] if len(rows) > limit else None}

    def checked_payload(body):
        if not body.prompt.strip():
            raise HTTPException(422, 'Enter a prompt.')
        payload = body.model_dump(exclude={'requestId'})
        try:
            encoded = json.dumps(payload, allow_nan=False, ensure_ascii=False)
        except (ValueError, TypeError):
            raise HTTPException(422, 'Generation settings must contain finite JSON values.') from None
        if len(encoded.encode()) > 32_768:
            raise HTTPException(413, 'Generation settings are too large. Use library asset IDs for references.')
        return payload, encoded

    @api.post('/api/studio/requests/cancel')
    def cancel_request(body: Generation, request: Request):
        owner = require(request)
        payload, _ = checked_payload(body)
        store = get_store()
        # Cancellation cannot execute work; it must remain available even when
        # the engine or original input is unavailable.
        try:
            jid = store.enqueue_once(owner, body.requestId, body.kind, payload, cancel=True)
        except RequestConflict as exc:
            raise HTTPException(409, str(exc)) from None
        return public_job(store.get_owned(owner, jid))

    @api.post('/api/studio/jobs')
    def submit(body: Generation, request: Request):
        owner = require(request)
        payload, encoded = checked_payload(body)
        store = get_store()
        # Accepted retries remain recoverable when an adapter is later offline.
        # enqueue_once checks their exact persisted content before returning.
        previous = store.find_owned_request(owner, body.requestId)
        if previous is None:
            admit(payload)  # exact variant, qualification and execution availability
            if json.dumps(payload, allow_nan=False, ensure_ascii=False) != encoded:
                raise HTTPException(500, 'The adapter changed the requested generation settings.')
            validate_reference(store, owner, payload)
        try:
            jid = store.enqueue_once(owner, body.requestId, body.kind, payload)
        except RequestConflict as exc:
            raise HTTPException(409, str(exc)) from None
        return public_job(store.get_owned(owner, jid))

    @api.get('/api/studio/jobs/{job_id}')
    def status(job_id: str, request: Request):
        owner = require(request)
        job = get_store().get_owned(owner, job_id)
        if job is None:
            raise HTTPException(404, 'This job is not available to this device.')
        return public_job(job)

    @api.get('/api/studio/jobs/{job_id}/content')
    def content(job_id: str, request: Request):
        owner = require(request)
        job = get_store().get_owned(owner, job_id)
        if job is None or job['status'] != 'succeeded' or artifact_root is None:
            raise HTTPException(404, 'This result is not available to this device.')
        artifact = (job.get('result') or {}).get('artifact')
        if not isinstance(artifact, dict):
            raise HTTPException(404, 'This job has no downloadable result.')
        speech = job['kind'] == 'audio' and job['payload'].get('engineId') == 'chatterbox-english-cpu'
        music = job['kind'] == 'audio' and job['payload'].get('engineId') == 'acestep-gpu'
        video = job['kind'] == 'video' and job['payload'].get('engineId') == 'wan22-ti2v-5b-gpu'
        try:
            root = Path(artifact_root).resolve()
            path = Path(artifact.get('path', ''))
            # Every job owns a separate publication directory. A result record
            # cannot point to a different user's output or a library original.
            if path.is_absolute() or len(path.parts) != 2 or path.parts[0] != job_id:
                raise ValueError('invalid artifact path')
            candidate = (root / path).resolve()
            if not candidate.is_relative_to(root / job_id) or not candidate.is_file():
                raise ValueError('artifact escaped its publication directory')
            if candidate.suffix.lower() not in {'.png', '.jpg', '.jpeg', '.webp', '.mp4', '.webm', '.wav', '.mp3', '.flac', '.glb', '.zip'}:
                raise ValueError('unsupported artifact format')
            expected = artifact.get('sha256')
            if not isinstance(expected, str) or not re.fullmatch(r'[a-f0-9]{64}', expected):
                raise ValueError('missing artifact hash')
            if candidate.stat().st_size != artifact.get('bytes'):
                raise ValueError('artifact size changed')
            if job['kind'] == 'model':
                from .glb_contract import MAX_BYTES, inspect_generated_glb
                if candidate.suffix.lower() != '.glb' or candidate.stat().st_size > MAX_BYTES:
                    raise ValueError('unsupported model artifact')
                with candidate.open('rb') as handle:
                    model_bytes = handle.read(MAX_BYTES + 1)
                if len(model_bytes) != artifact['bytes']:
                    raise ValueError('model artifact size changed')
                actual = hashlib.sha256(model_bytes).hexdigest()
                inspect_generated_glb(model_bytes)
            elif speech or music:
                if music:
                    from .music_artifact import MAX_BYTES, inspect_wav
                else:
                    from .speech_artifact import MAX_BYTES, inspect_wav
                if candidate.suffix.lower() != '.wav' or candidate.stat().st_size > MAX_BYTES:
                    raise ValueError('unsupported speech artifact')
                with candidate.open('rb') as handle:
                    speech_bytes = handle.read(MAX_BYTES + 1)
                checked = inspect_wav(speech_bytes)
                if checked['bytes'] != artifact['bytes']:
                    raise ValueError('speech artifact size changed')
                actual = checked['sha256']
            elif video:
                from .video_artifact import MAX_BYTES, inspect_mp4
                if candidate.suffix.lower() != '.mp4' or candidate.stat().st_size > MAX_BYTES:
                    raise ValueError('unsupported video artifact')
                with candidate.open('rb') as handle:
                    video_bytes = handle.read(MAX_BYTES + 1)
                checked = inspect_mp4(video_bytes)
                if checked['bytes'] != artifact['bytes']:
                    raise ValueError('video artifact size changed')
                actual = checked['sha256']
            else:
                with candidate.open('rb') as handle:
                    actual = hashlib.file_digest(handle, 'sha256').hexdigest()
            if not hmac.compare_digest(actual, expected):
                raise ValueError('artifact hash changed')
        except (ValueError, OSError, TypeError):
            raise HTTPException(409, 'The result is missing or changed. Its integrity must be checked before downloading.') from None
        if job['kind'] == 'model':
            # Return the exact checked buffer, not a second mutable file read.
            return Response(model_bytes, media_type='model/gltf-binary', headers={
                'X-Content-SHA256': expected, 'X-Studio-Portable': 'glb-v1',
                'Cache-Control': 'no-store',
                'Content-Disposition': 'attachment; filename="output.glb"',
            })
        if speech or music:
            return Response(speech_bytes, media_type='audio/wav', headers={
                'X-Content-SHA256': expected, 'Cache-Control': 'private, no-store',
                'Content-Disposition': 'attachment; filename="output.wav"',
            })
        if video:
            return Response(video_bytes, media_type='video/mp4', headers={
                'X-Content-SHA256': expected, 'Cache-Control': 'private, no-store',
                'Content-Disposition': 'attachment; filename="output.mp4"',
            })
        return FileResponse(candidate, filename=candidate.name,
                            media_type=mimetypes.guess_type(candidate.name)[0] or 'application/octet-stream',
                            headers={'X-Content-SHA256': expected})

    @api.get('/api/studio/jobs/{job_id}/preview')
    def preview(job_id: str, request: Request):
        # Reuse the exact owner/status/path/hash checks before decoding anything.
        verified = content(job_id, request)
        if verified.media_type not in {'image/png', 'image/jpeg', 'image/webp'}:
            raise HTTPException(404, 'This result has no image preview.')
        if not _PREVIEW_SLOTS.acquire(blocking=False):
            raise HTTPException(429, 'Another preview is loading. Try again shortly.')
        try:
            return Response(thumbnail(Path(verified.path)), media_type='image/png')
        except (ValueError, OSError):
            raise HTTPException(422, 'This image could not be previewed.') from None
        finally:
            _PREVIEW_SLOTS.release()

    @api.post('/api/studio/jobs/{job_id}/library')
    def save_result(job_id: str, request: Request):
        # content enforces device ownership, success and the qualified artifact hash.
        verified = content(job_id, request)
        if verified.media_type == 'audio/wav':
            if not callable(save_audio):
                raise HTTPException(409, 'Update this server to save speech in Library.')
            import tempfile
            owned = get_store().get_owned(require(request), job_id) or {}
            music = owned.get('payload', {}).get('engineId') == 'acestep-gpu'
            if music:
                from .music_artifact import inspect_wav
            else:
                from .speech_artifact import inspect_wav
            try:
                checked = inspect_wav(bytes(verified.body))
                if checked['sha256'] != verified.headers['X-Content-SHA256']:
                    raise HTTPException(409, 'The result changed before it could be saved.')
                with tempfile.TemporaryDirectory(prefix='studio-speech-') as directory:
                    snapshot = Path(directory) / ('music.wav' if music else 'speech.wav')
                    snapshot.write_bytes(verified.body)
                    title = owned.get('payload', {}).get('prompt', '')
                    title = ' '.join(str(title).split())[:60] or ('Music ' if music else 'Speech ') + job_id[:8]
                    return save_audio(snapshot, title, 'music' if music else 'speech')
            except (ValueError, OSError):
                raise HTTPException(409, 'The speech could not be saved. Your generated result is still available.') from None
        if verified.media_type == 'video/mp4':
            if not callable(save_video):
                raise HTTPException(409, 'Update this server to save generated video in Library.')
            import tempfile
            from .video_artifact import inspect_mp4
            owned = get_store().get_owned(require(request), job_id) or {}
            try:
                checked = inspect_mp4(bytes(verified.body))
                if checked['sha256'] != verified.headers['X-Content-SHA256']:
                    raise HTTPException(409, 'The result changed before it could be saved.')
                with tempfile.TemporaryDirectory(prefix='studio-video-') as directory:
                    snapshot = Path(directory) / 'clip.mp4'
                    snapshot.write_bytes(verified.body)
                    title = ' '.join(str(owned.get('payload', {}).get('prompt', '')).split())[:60] or 'Video ' + job_id[:8]
                    return save_video(snapshot, title)
            except (ValueError, OSError):
                raise HTTPException(409, 'The video could not be saved. Your generated result is still available.') from None
        if verified.media_type != 'image/png':
            raise HTTPException(422, 'Choose a completed PNG cutout, WAV audio or MP4 video result.')
        owned = get_store().get_owned(require(request), job_id) or {}
        generated = owned.get('payload', {}).get('engineId') == 'qwen-image-21-gpu'
        if generated and not callable(save_generated_image):
            raise HTTPException(409, 'Update this server to save generated images in Library.')
        if not generated and not callable(save_image):
            raise HTTPException(409, 'Update this server to save cutouts in Library.')
        from .studio_inputs import validate_image
        import tempfile
        try:
            with Path(verified.path).open('rb') as stream:
                data = stream.read(20 * 1024**2 + 1)
            if hashlib.sha256(data).hexdigest() != verified.headers['X-Content-SHA256']:
                raise HTTPException(409, 'The result changed before it could be saved.')
            validate_image(data)
            # Pass immutable, verified snapshot bytes to the import callback.
            with tempfile.TemporaryDirectory(prefix='studio-cutout-') as directory:
                snapshot = Path(directory) / ('image.png' if generated else 'cutout.png')
                snapshot.write_bytes(data)
                if generated:
                    title = ' '.join(str(owned.get('payload', {}).get('prompt', '')).split())[:60] or 'Image ' + job_id[:8]
                    return save_generated_image(snapshot, title)
                return save_image(snapshot, 'Cutout ' + job_id[:8])
        except (ValueError, OSError):
            raise HTTPException(409, 'The cutout could not be saved. Your generated result is still available.') from None

    @api.post('/api/studio/jobs/{job_id}/input')
    def snapshot_result(job_id: str, request: Request):
        # Job permission is sufficient for the owner's own result. Library
        # permission never grants access to someone else's generation history.
        owner = require(request)
        verified = content(job_id, request)
        if verified.media_type not in {'image/png', 'image/jpeg', 'image/webp'}:
            raise HTTPException(422, 'Choose a completed image result.')
        from .studio_inputs import _SNAPSHOT_SLOTS, validate_image
        if not _SNAPSHOT_SLOTS.acquire(blocking=False):
            raise HTTPException(429, 'Image preparation is busy. Try again shortly.')
        try:
            with Path(verified.path).open('rb') as handle:
                data = handle.read(20 * 1024**2 + 1)
            # content() checked the saved result; this read is independently
            # bound to that same hash before accepting any bytes as an input.
            if hashlib.sha256(data).hexdigest() != verified.headers['X-Content-SHA256']:
                raise HTTPException(409, 'The result changed before it could be prepared.')
            width, height = validate_image(data)
            result = get_store().put_input(owner, data, width=width, height=height)
            return {'version': 1, **result}
        except (ValueError, OSError):
            raise HTTPException(422, 'This image could not be prepared. Choose a valid image up to 20 MiB.') from None
        finally:
            _SNAPSHOT_SLOTS.release()

    @api.post('/api/studio/jobs/{job_id}/cancel')
    def cancel(job_id: str, request: Request):
        owner = require(request)
        store = get_store()
        if store.get_owned(owner, job_id) is None:
            raise HTTPException(404, 'This job is not available to this device.')
        # Ownership is immutable. Repeated cancellation is safe, including when
        # the worker completed between the ownership check and cancellation.
        store.request_cancel(job_id)
        return public_job(store.get_owned(owner, job_id))

    return api
