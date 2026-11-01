"""Read-only Studio library bridge. Tickets cannot authenticate admin APIs."""
import io
import base64
import hashlib
import hmac
import mimetypes
import re
import secrets
import time
import threading
from pathlib import Path
from urllib.parse import unquote, urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, ConfigDict, Field
from .game_assets import MAX_INPUT_BYTES, pack_frames
from .glb_contract import MAX_BYTES as MAX_GLB_BYTES, inspect_generated_glb

_PREVIEW_SLOTS = threading.BoundedSemaphore(2)
_EXPORT_SLOTS = threading.BoundedSemaphore(1)


class SpriteExport(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    assetIds: list[str] = Field(min_length=1, max_length=64)

TOKEN_AGE = 30 * 24 * 3600
KINDS = {'.png': 'image', '.jpg': 'image', '.jpeg': 'image', '.webp': 'image',
         '.gif': 'image', '.avif': 'image', '.mp4': 'video', '.webm': 'video',
         '.mov': 'video', '.mkv': 'video', '.mp3': 'audio', '.wav': 'audio',
         '.flac': 'audio', '.m4a': 'audio', '.ogg': 'audio', '.glb': 'model'}


def ticket(secret, role, code, now=None):
    prefix = f"mlab-library-v1.{role}.{int(time.time() if now is None else now)}.{secrets.token_hex(12)}"
    sig = hmac.new(secret.encode(), f'{prefix}:{code}'.encode(), hashlib.sha256).hexdigest()
    return f'{prefix}.{sig}'


def valid_ticket(raw, secret, role_code, now=None, max_age=None):
    """``max_age`` lets a host keep passes longer than TOKEN_AGE (the family
    studio uses a year); rotating the role's code still revokes them at once."""
    match = re.fullmatch(r'(mlab-library-v1\.(user|admin)\.(\d{10,12})\.[a-f0-9]{24})\.([a-f0-9]{64})', raw or '')
    if not match:
        return False
    prefix, role, issued, signature = match.groups()
    age = (time.time() if now is None else now) - int(issued)
    expected = hmac.new(secret.encode(), f'{prefix}:{role_code(role)}'.encode(), hashlib.sha256).hexdigest()
    limit = TOKEN_AGE if max_age is None else max_age
    return -300 <= age <= limit and hmac.compare_digest(signature, expected)


def is_library_path(path):
    return path in ('/api/studio/library', '/api/studio/library/sprites') or bool(re.fullmatch(r'/api/studio/library/[A-Za-z0-9_-]{1,128}/(?:content|preview)', path))


def catalog(rows, media_root):
    """Only completed, supported gallery files inside the media root are offered."""
    root = Path(media_root).resolve()
    out = []
    seen = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        item_id = str(row.get('id', ''))
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,128}', item_id) or item_id in seen:
            continue
        if row.get('status') not in (None, 'done', 'completed'):
            continue
        try:
            url = urlsplit(str(row.get('url', '')))
            path = unquote(url.path)
            if url.scheme or url.netloc or not path.startswith('/media/'):
                continue
            candidate = (root / path[len('/media/'):]).resolve()
            if not candidate.is_relative_to(root) or not candidate.is_file():
                continue
            size = candidate.stat().st_size
        except (ValueError, OSError):
            continue
        kind = KINDS.get(candidate.suffix.lower())
        if not kind:
            continue
        seen.add(item_id)
        stamp = row.get('ts', 0)
        created = int(stamp * 1000) if isinstance(stamp, (int, float)) and 0 <= stamp < 1e12 else 0
        out.append(({'id': item_id, 'kind': kind, 'title': str(row.get('title') or row.get('prompt') or 'Untitled creation')[:240],
                     'prompt': str(row.get('prompt') or '')[:2000], 'createdAt': created,
                     'folder': candidate.parent.relative_to(root).as_posix() if candidate.parent != root else '',
                     'fileName': candidate.name, 'mimeType': mimetypes.guess_type(candidate.name)[0] or 'application/octet-stream',
                     'hasPreview': bool(preview_source(row, root)), 'bytes': size, 'providerLabel': str(row.get('engine') or 'Media Lab')[:100]}, candidate))
    return sorted(out, key=lambda item: item[0]['createdAt'], reverse=True)


def selected_rows(rows, asset_ids):
    """Resolve only requested assets; unrelated files need no filesystem checks."""
    return [row for row in rows if isinstance(row, dict) and str(row.get('id', '')) in asset_ids]


def preview_source(row, media_root):
    root = Path(media_root).resolve()
    candidate_url = row.get('poster') or row.get('url') or ''
    try:
        url = urlsplit(str(candidate_url))
        decoded = unquote(url.path)
        if url.scheme or url.netloc or not decoded.startswith('/media/'):
            return None
        path = (root / decoded[len('/media/'):]).resolve()
        if path.is_relative_to(root) and path.is_file() and KINDS.get(path.suffix.lower()) == 'image':
            return path
    except (ValueError, OSError):
        pass
    return None


def thumbnail(path):
    from PIL import Image, ImageOps
    # Bounded decode; never invoke a video engine or download an external poster.
    if path.stat().st_size > 20 * 1024 * 1024:
        raise ValueError('Preview source is too large')
    with Image.open(path) as original:
        if original.width * original.height > 16_000_000:
            raise ValueError('Preview dimensions are too large')
        oriented = ImageOps.exif_transpose(original)
        oriented.thumbnail((480, 320))
        oriented = oriented.convert('RGBA')
        clean = Image.new('RGBA', oriented.size)
        clean.paste(oriented)
        output = io.BytesIO()
        clean.save(output, format='PNG')
        return output.getvalue()


def router(load_rows, media_root, authorize):
    api = APIRouter()

    def require(request):
        auth = request.headers.get('authorization', '')
        if not auth.startswith('Bearer ') or not authorize(auth[7:]):
            raise HTTPException(401, 'Connect your Media Lab library again.')

    @api.get('/api/studio/library')
    def library(request: Request):
        require(request)
        return {'version': 1, 'assets': [meta for meta, _ in catalog(load_rows(), media_root)]}

    @api.get('/api/studio/library/{asset_id}/content')
    def content(asset_id: str, request: Request):
        require(request)
        for meta, path in catalog(selected_rows(load_rows(), {asset_id}), media_root):
            if meta['id'] == asset_id:
                if meta['kind'] == 'model' and request.query_params.get('portable') == '1':
                    if meta['bytes'] > MAX_GLB_BYTES:
                        raise HTTPException(413, 'Portable 3D imports currently support GLB files up to 64 MiB.')
                    try:
                        with path.open('rb') as stream: data = stream.read(MAX_GLB_BYTES + 1)
                        inspect_generated_glb(data)
                    except (ValueError, OSError):
                        raise HTTPException(422, 'This model is not supported as a self-contained project asset. Export a GLB with embedded resources and no extensions.') from None
                    return Response(data, media_type='model/gltf-binary', headers={'X-Studio-Portable':'glb-v1'})
                return FileResponse(path, media_type=meta['mimeType'], filename=meta['fileName'])
        raise HTTPException(404, 'This creation is no longer available.')

    @api.post('/api/studio/library/sprites')
    def sprites(body: SpriteExport, request: Request):
        # A stateless derived download, like a preview: no library writes, jobs,
        # model downloads or GPU access are granted by the read-only ticket.
        require(request)
        if len(set(body.assetIds)) != len(body.assetIds):
            raise HTTPException(422, 'Choose each frame only once.')
        available = {meta['id']: (meta, path) for meta, path in catalog(selected_rows(load_rows(), set(body.assetIds)), media_root)}
        if any(item not in available for item in body.assetIds):
            raise HTTPException(404, 'A selected frame is no longer available. Refresh the library.')
        selected = [available[item] for item in body.assetIds]
        if any(path.suffix.lower() != '.png' for _, path in selected):
            raise HTTPException(422, 'Sprite export currently requires individual PNG frames.')
        if sum(meta['bytes'] for meta, _ in selected) > MAX_INPUT_BYTES:
            raise HTTPException(413, 'The selected frames exceed the 64 MiB input limit.')
        if not _EXPORT_SLOTS.acquire(blocking=False):
            raise HTTPException(429, 'Another sprite export is running. Try again shortly.')
        try:
            result = pack_frames([(f'frame-{index:03d}.png', path.read_bytes()) for index, (_, path) in enumerate(selected)])
            if len(result.png) > 24 * 1024 * 1024:
                raise HTTPException(413, 'The atlas is too large for this export. Select fewer frames.')
            return {'version': 1, 'pngBase64': base64.b64encode(result.png).decode(), 'metadata': result.metadata}
        except (ValueError, OSError) as exc:
            raise HTTPException(422, str(exc)) from None
        finally:
            _EXPORT_SLOTS.release()

    @api.get('/api/studio/library/{asset_id}/preview')
    def preview(asset_id: str, request: Request):
        require(request)
        rows = selected_rows(load_rows(), {asset_id})
        if any(meta['id'] == asset_id for meta, _ in catalog(rows, media_root)):
            row = next((row for row in rows if isinstance(row, dict) and str(row.get('id')) == asset_id), {})
            path = preview_source(row, media_root)
            if path:
                try:
                    with _PREVIEW_SLOTS:
                        return Response(thumbnail(path), media_type='image/png')
                except (ValueError, OSError):
                    pass
        raise HTTPException(404, 'No preview is available for this creation.')

    return api
