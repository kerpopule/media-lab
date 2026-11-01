"""Device-owned Cut drafts with a distinct, explicitly requested permission.

CPU preview and high-quality export rendering; no model generation, source mutation, publication or legacy controller imports.
Library access is additionally required when adding sources to a draft.
"""
import hashlib
import hmac
import json
import math
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

TOKEN_AGE = 30 * 24 * 3600


def ticket(secret, code, device_id, now=None):
    if not re.fullmatch(r'[a-f0-9]{32}', device_id or ''):
        raise ValueError('A valid device identity is required.')
    prefix = f'mlab-edit-v1.{int(time.time() if now is None else now)}.{device_id}'
    signature = hmac.new(secret.encode(), f'{prefix}:{code}'.encode(), hashlib.sha256).hexdigest()
    return f'{prefix}.{signature}'


def identity(raw, secret, code, now=None):
    match = re.fullmatch(r'(mlab-edit-v1\.(\d{10,12})\.([a-f0-9]{32}))\.([a-f0-9]{64})', raw or '')
    if not match:
        return None
    prefix, issued, device_id, signature = match.groups()
    age = (time.time() if now is None else now) - int(issued)
    expected = hmac.new(secret.encode(), f'{prefix}:{code}'.encode(), hashlib.sha256).hexdigest()
    return device_id if -300 <= age <= TOKEN_AGE and hmac.compare_digest(signature, expected) else None


class NewDraft(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    requestId: str = Field(pattern=r'^[a-zA-Z0-9_-]{16,80}$')
    title: str = Field(min_length=1, max_length=160)
    assetIds: list[str] = Field(min_length=1, max_length=8)


class PreviewRequest(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    revision: int = Field(ge=0, le=1000000000)


class StoryboardScene(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    assetId: str = Field(pattern=r'^[A-Za-z0-9_-]{1,128}$')
    seconds: float = Field(gt=0, le=600, allow_inf_nan=False)


class StoryboardDraft(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    requestId: str = Field(pattern=r'^[a-zA-Z0-9_-]{16,80}$')
    storyboardId: str = Field(min_length=1, max_length=240)
    sourceSha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    title: str = Field(min_length=1, max_length=160)
    fps: int = Field(ge=1, le=60)
    scenes: list[StoryboardScene] = Field(min_length=1, max_length=128)
    musicAssetId: str | None = Field(default=None, pattern=r'^[A-Za-z0-9_-]{1,128}$')


class EditCommand(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    id: str = Field(pattern=r'^[a-zA-Z0-9_-]{1,80}$')
    type: str = Field(min_length=1, max_length=40)
    payload: dict = Field(default_factory=dict)


class EditDraft(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    transactionId: str = Field(pattern=r'^[a-zA-Z0-9_-]{16,80}$')
    revision: int = Field(ge=0)
    commands: list[EditCommand] = Field(min_length=1, max_length=32)


def router(root: Path, authorize, library, save_export=None):
    from . import cut, studio_library
    api = APIRouter(prefix='/api/studio/editing')
    # Serial admission also keeps idempotent draft creation atomic in this
    # single-process host. Reject contention rather than queue expensive probes.
    admission = threading.Lock()
    preview_admission = threading.Lock()
    export_jobs_admission = threading.Lock()
    runtime_id = uuid.uuid4().hex
    allowed = {'clip.add', 'clip.remove', 'clip.trim', 'clip.split', 'clip.move',
               'transition.set', 'transition.remove', 'caption.add', 'caption.edit',
               'caption.remove', 'audio.mix', 'color.apply', 'undo', 'redo'}

    def owned_root(request):
        raw = request.headers.get('authorization', '')
        owner = authorize(raw[7:]) if raw.startswith('Bearer ') else None
        if not owner or not re.fullmatch(r'[a-f0-9]{32}', owner):
            raise HTTPException(401, 'Pair this device with editing permission first.')
        return root / owner

    def open_owned(base, project_id):
        if not re.fullmatch(r'cut-(?:[a-f0-9]{10}|[a-f0-9]{32})', project_id):
            raise HTTPException(404, 'This editing draft is unavailable.')
        try:
            return cut.open_project(base, project_id)
        except cut.CutError:
            raise HTTPException(404, 'This editing draft is unavailable.') from None

    def preserved_permission(request):
        base = owned_root(request)
        raw = request.headers.get('x-library-authorization', '')
        if not raw.startswith('Bearer ') or not library.authorize(raw[7:]):
            raise HTTPException(403, 'Library permission is required to import preserved edits.')
        return base

    @api.get('/preserved')
    def preserved(request: Request):
        preserved_permission(request)
        return {'projects': cut.list_projects(root.parent / 'preserved-edits')}

    @api.post('/preserved/{project_id}/import')
    def import_preserved(project_id: str, request: Request):
        base = preserved_permission(request)
        if not re.fullmatch(r'cut-(?:[a-f0-9]{10}|[a-f0-9]{32})', project_id):
            raise HTTPException(404, 'This preserved edit is unavailable.')
        from .preserved_edit_import import import_edit
        try:
            return {'project': import_edit(root.parent / 'preserved-edits', base, project_id, library.media_root)}
        except FileNotFoundError:
            raise HTTPException(404, 'This preserved edit is unavailable.') from None
        except (OSError, ValueError, cut.CutError):
            raise HTTPException(409, 'Could not import this edit. Check its source files and existing project identity.') from None

    @api.get('/projects')
    def projects(request: Request):
        return {'projects': cut.list_projects(owned_root(request))}

    @api.post('/storyboards')
    def import_storyboard(body: StoryboardDraft, request: Request):
        from .storyboard_edit import build_storyboard_project
        from .studio_collections import source_digest
        base = preserved_permission(request)
        if library.load_collections is None:
            raise HTTPException(409, 'This server does not expose saved storyboards.')
        project_id = 'cut-' + hashlib.sha256(('storyboard:' + body.requestId).encode()).hexdigest()[:32]
        fingerprint = hashlib.sha256(json.dumps(body.model_dump(exclude_none=True), sort_keys=True).encode()).hexdigest()
        if not admission.acquire(blocking=False):
            raise HTTPException(409, 'The editor is preparing another draft. Try again shortly.')
        try:
            if (cut.project_dir(base, project_id) / 'project.json').exists():
                saved = cut.open_project(base, project_id).load()
                if saved.get('provenance', {}).get('studio_request_sha256') != fingerprint:
                    raise HTTPException(409, 'This request already created a different draft.')
                return {'project': saved}
            records = library.load_collections().get('storyboards', [])
            record = next((row for row in records if isinstance(row, dict) and row.get('id') == body.storyboardId), None)
            if record is None:
                raise HTTPException(404, 'This saved storyboard is unavailable.')
            if source_digest(record) != body.sourceSha256:
                raise HTTPException(409, 'The saved storyboard changed. Reload it before creating an editing copy.')
            beats = record.get('beats')
            if not isinstance(beats, list) or len(beats) != len(body.scenes) or any(not isinstance(beat, dict) for beat in beats):
                raise HTTPException(422, 'Choose media and timing for every storyboard scene.')
            available = {row['id']: (row, path) for row, path in studio_library.catalog(library.load_rows(), library.media_root)}
            sources, total_bytes = {}, 0
            source_ids = [choice.assetId for choice in body.scenes] + ([body.musicAssetId] if body.musicAssetId else [])
            for asset_id in source_ids:
                if asset_id in sources:
                    continue
                entry = available.get(asset_id)
                expected_kinds = ('audio',) if asset_id == body.musicAssetId else ('image', 'video')
                if not entry or entry[0]['kind'] not in expected_kinds:
                    raise HTTPException(404, 'A chosen storyboard source is unavailable in Library.')
                row, path = entry
                size = path.stat().st_size
                total_bytes += size
                if size > 256 * 1024**2 or total_bytes > 2 * 1024**3:
                    raise HTTPException(422, 'Choose sources up to 256 MB each and 2 GB total for this editor.')
                sources[asset_id] = cut.probe_gallery_file(path, media_root=library.media_root)
            scenes = [{'asset_id': choice.assetId, 'seconds': choice.seconds,
                       'label': beat.get('title') or f'Scene {index + 1}',
                       'narration': beat.get('narration') or ''}
                      for index, (choice, beat) in enumerate(zip(body.scenes, beats))]
            manifest = build_storyboard_project(project_id, body.title, scenes, sources, storyboard_id=body.storyboardId, fps=body.fps, music_asset_id=body.musicAssetId)
            manifest['provenance'].update(studio_request_sha256=fingerprint, storyboard_source_sha256=body.sourceSha256)
            return {'project': cut.create_project(base, manifest).load()}
        except cut.CutError as error:
            raise HTTPException(422, str(error)) from None
        except (OSError, ValueError, TypeError, subprocess.TimeoutExpired):
            raise HTTPException(422, 'Could not prepare this storyboard. Check its saved data and source files.') from None
        finally:
            admission.release()

    @api.get('/projects/{project_id}')
    def project(project_id: str, request: Request):
        return {'project': open_owned(owned_root(request), project_id).load()}

    @api.post('/projects')
    def create(body: NewDraft, request: Request):
        base = owned_root(request)
        raw = request.headers.get('x-library-authorization', '')
        if not raw.startswith('Bearer ') or not library.authorize(raw[7:]):
            raise HTTPException(403, 'Library permission is required to use source media.')
        if len(set(body.assetIds)) != len(body.assetIds):
            raise HTTPException(422, 'Choose each source once; duplicate clips can be added later.')
        project_id = 'cut-' + hashlib.sha256(body.requestId.encode()).hexdigest()[:32]
        fingerprint = hashlib.sha256(json.dumps(body.model_dump(exclude_none=True), sort_keys=True).encode()).hexdigest()
        if not admission.acquire(blocking=False):
            raise HTTPException(409, 'The editor is preparing another draft. Try again shortly.')
        try:
            path = cut.project_dir(base, project_id) / 'project.json'
            if path.exists():
                saved = cut.open_project(base, project_id).load()
                if saved.get('provenance', {}).get('studio_request_sha256') != fingerprint:
                    raise HTTPException(409, 'This request already created a different draft.')
                return {'project': saved}
            available = {row['id']: (row, path) for row, path in
                         studio_library.catalog(library.load_rows(), library.media_root)}
            sources = []
            for asset_id in body.assetIds:
                entry = available.get(asset_id)
                if not entry or entry[0]['kind'] not in ('image', 'video', 'audio'):
                    raise HTTPException(404, 'A selected Library source is unavailable.')
                row, path = entry
                if path.stat().st_size > 256 * 1024**2:
                    raise HTTPException(422, 'Choose a source smaller than 256 MB for this editor preview.')
                sources.append({**cut.probe_gallery_file(path, media_root=library.media_root), 'id': asset_id,
                                'title': row['title'], 'prompt': row['prompt']})
            manifest = cut.build_gallery_project(project_id, body.title, sources)
            manifest.setdefault('provenance', {})['studio_request_sha256'] = fingerprint
            return {'project': cut.create_project(base, manifest).load()}
        except (cut.CutError, OSError, ValueError, subprocess.TimeoutExpired):
            raise HTTPException(422, 'Could not prepare the source media. Check that FFprobe is installed and the files can be opened.') from None
        finally:
            admission.release()

    @api.post('/projects/{project_id}/transactions')
    def edit(project_id: str, body: EditDraft, request: Request):
        store = open_owned(owned_root(request), project_id)
        commands = [command.model_dump() for command in body.commands]
        if len(json.dumps(commands)) > 64 * 1024 or any(command.get('type') not in allowed for command in commands):
            raise HTTPException(422, 'Choose a supported timeline edit. Rendering and publication are separate actions.')
        adding = [command for command in commands if command['type'] == 'clip.add']
        if adding:
            raw = request.headers.get('x-library-authorization', '')
            if not raw.startswith('Bearer ') or not library.authorize(raw[7:]):
                raise HTTPException(403, 'Library permission is required to add source media.')
            if len(adding) > 8:
                raise HTTPException(422, 'Add up to eight Library items at a time.')
            if not admission.acquire(blocking=False):
                raise HTTPException(409, 'The editor is preparing other media. Retry this request shortly.')
            def resolve(asset_id):
                available = {row['id']: (row, path) for row, path in
                             studio_library.catalog(library.load_rows(), library.media_root)}
                entry = available.get(asset_id)
                if not entry or entry[0]['kind'] not in ('image', 'video', 'audio'):
                    raise cut.CutError('A selected Library source is unavailable.')
                row, path = entry
                if path.stat().st_size > 256 * 1024**2:
                    raise cut.CutError('Choose a source in the media folder smaller than 256 MB.')
                return {**cut.probe_gallery_file(path, media_root=library.media_root), 'id': asset_id, 'title': row['title'], 'prompt': row['prompt']}
            store.asset_resolver = resolve
        try:
            result = store.transact(commands, actor='agent',
                                    transaction_id=body.transactionId, expected_revision=body.revision)
            return {'result': result, 'project': store.load()}
        except cut.CutError as error:
            status = 409 if 'revision conflict' in str(error) else 422
            raise HTTPException(status, str(error)) from None
        except (OSError, ValueError, subprocess.TimeoutExpired):
            raise HTTPException(422, 'Could not inspect the selected source. Check FFprobe and retry.') from None
        finally:
            if adding:
                admission.release()

    def preview_paths(base, project_id, revision, export=False):
        if not re.fullmatch(r'cut-(?:[a-f0-9]{10}|[a-f0-9]{32})', project_id) or not 0 <= revision <= 1000000000:
            raise HTTPException(404, 'This preview is unavailable.')
        directory = cut.project_dir(base, project_id) / ('exports' if export else 'previews') / str(revision)
        return directory, directory / ('export.mp4' if export else 'preview.mp4'), directory / 'receipt.json'

    def digest_file(path):
        digest = hashlib.sha256()
        with path.open('rb') as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b''):
                digest.update(chunk)
        return digest.hexdigest()

    def cached_preview(base, project_id, revision, export=False):
        _, output, receipt_path = preview_paths(base, project_id, revision, export)
        try:
            receipt = json.loads(receipt_path.read_text())
            size = output.stat().st_size
            if not isinstance(receipt, dict):
                return None
            if (receipt.get('projectId') != project_id or receipt.get('revision') != revision or
                    receipt.get('bytes') != size or not 0 < size <= (1024 if export else 64) * 1024**2 or
                    receipt.get('sha256') != digest_file(output)):
                return None
            return receipt
        except (OSError, ValueError):
            return None

    def render(project_id, body, request, *, export=False, base=None, snapshot=None, reserved=False):
        base = base if base is not None else owned_root(request)
        if not reserved:
            open_owned(base, project_id)
            cached = cached_preview(base, project_id, body.revision, export)
            if cached:
                return cached
        if not reserved and not preview_admission.acquire(blocking=False):
            raise HTTPException(409, 'An edit is rendering. Retry this same revision shortly.')
        try:
            cached = cached_preview(base, project_id, body.revision, export)
            if cached:
                return cached
            project = snapshot if snapshot is not None else open_owned(base, project_id).load()  # immutable revision snapshot
            if project['revision'] != body.revision:
                raise HTTPException(409, 'The timeline changed. Refresh before rendering.')
            settings = project['settings']
            clips = [clip for track in project['timeline']['tracks'] for clip in track['clips']]
            if (project['duration_seconds'] > (600 if export else 60) or len(clips) > (128 if export else 16) or settings['fps'] > (60 if export else 30) or
                    (export and (max(settings['width'], settings['height']) > 1920 or
                                 settings['width'] * settings['height'] > 1920 * 1080))):
                raise HTTPException(422, 'Export supports up to 10 minutes, 128 clips, 60 fps and 1080p.' if export else 'Preview supports up to 60 seconds, 16 clips, 30 fps and 720p. Use export for larger timelines.')
            for asset in project.get('assets', []):
                source = cut._asset_file(Path(library.media_root), asset)
                if source.stat().st_size > 256 * 1024**2:
                    raise HTTPException(422, 'A source exceeds the 256 MB preview limit.')
            cursor = 0
            for clip in sorted([clip for track in project['timeline']['tracks'] if track.get('type') == 'video' for clip in track['clips']], key=lambda item: item['start_frame']):
                if clip['start_frame'] != cursor:
                    raise HTTPException(422, 'Rendering does not support gaps or overlapping video clips yet.')
                cursor += clip['duration_frames']
            directory, output, receipt_path = preview_paths(base, project_id, body.revision, export)
            # A file without a verified receipt is never offered as a preview.
            receipt_path.unlink(missing_ok=True)
            render_project = project
            if not export:
                width, height = settings['width'], settings['height']
                bounds = (1280, 720) if width >= height else (720, 1280)
                scale = min(1, bounds[0] / width, bounds[1] / height)
                preview_width, preview_height = max(2, int(width * scale) // 2 * 2), max(2, int(height * scale) // 2 * 2)
                render_project = {**project, 'settings': {**settings, 'width': preview_width,
                    'height': preview_height, 'aspect_ratio': f'{preview_width}:{preview_height}'}}
            receipt = cut.render_timeline(render_project, media_dir=library.media_root, output=output,
                                          export_request={'format':'mp4', 'quality':'high' if export else 'preview', 'include_audio':True},
                                          work_dir=directory/'work', timeout_seconds=900 if export else 60)
            if not 0 < output.stat().st_size <= (1024 if export else 64) * 1024**2:
                output.unlink(missing_ok=True)
                raise HTTPException(422, 'The export exceeds the 1 GB limit.' if export else 'The preview exceeds the 64 MB limit.')
            decoded = subprocess.run(['ffmpeg', '-nostdin', '-v', 'error', '-i', str(output),
                                      '-f', 'null', '-'], capture_output=True, timeout=180 if export else 30, check=False)
            if decoded.returncode:
                output.unlink(missing_ok=True)
                raise HTTPException(422, 'The rendered preview could not be decoded.')
            duration = float(receipt['ffprobe'].get('format', {}).get('duration', 0))
            if not math.isfinite(duration) or abs(duration - receipt['expected_seconds']) > max(0.25, 2 / settings['fps']):
                output.unlink(missing_ok=True)
                raise HTTPException(422, 'The rendered duration did not match the timeline.')
            public = {'projectId':project_id, 'revision':body.revision, 'bytes':receipt['bytes'],
                      'sha256':receipt['sha256'], 'seconds':duration, 'mimeType':'video/mp4',
                      'candidate':True}
            if export:
                public.update(quality='high', width=settings['width'], height=settings['height'], fps=settings['fps'])
            cut._atomic_write(receipt_path, public)
            return public
        except (cut.CutError, OSError, ValueError, subprocess.TimeoutExpired):
            raise HTTPException(422, 'Rendering failed. Check FFmpeg, source files and server capacity, then retry this revision.') from None
        finally:
            preview_admission.release()

    @api.post('/projects/{project_id}/preview')
    def preview(project_id: str, body: PreviewRequest, request: Request):
        return render(project_id, body, request)

    @api.post('/projects/{project_id}/export')
    def export_video(project_id: str, body: PreviewRequest, request: Request):
        return render(project_id, body, request, export=True)

    def export_job_status(base, project_id, revision):
        receipt = cached_preview(base, project_id, revision, True)
        state, message = ('ready', None) if receipt else ('not-ready', None)
        directory, _, _ = preview_paths(base, project_id, revision, True)
        if not receipt:
            try:
                job = json.loads((directory / 'job.json').read_text())
                if job.get('state') == 'running':
                    state = 'running' if job.get('runtime') == runtime_id else 'interrupted'
                elif job.get('state') == 'failed':
                    state = 'failed'
                elif job.get('state') == 'ready':
                    state = 'failed'  # output no longer verifies
                if state in ('failed', 'interrupted'):
                    message = 'No verified export is available. Retry this revision to render again.'
            except (OSError, ValueError, AttributeError):
                pass
        return {'projectId': project_id, 'revision': revision, 'state': state,
                'receipt': receipt, 'message': message}

    @api.post('/projects/{project_id}/export-jobs')
    def start_export_job(project_id: str, body: PreviewRequest, request: Request):
        base = owned_root(request)
        store = open_owned(base, project_id)
        with export_jobs_admission:
            status = export_job_status(base, project_id, body.revision)
            if status['state'] in ('ready', 'running'):
                return JSONResponse(status, headers={'Cache-Control': 'no-store'})
            snapshot = store.load()
            if snapshot['revision'] != body.revision:
                raise HTTPException(409, 'The timeline changed. Refresh before rendering.')
            if not preview_admission.acquire(blocking=False):
                raise HTTPException(409, 'An edit is rendering. Retry this same revision shortly.')
            directory, _, _ = preview_paths(base, project_id, body.revision, True)
            job_path = directory / 'job.json'
            def worker():
                state = 'failed'
                try:
                    render(project_id, body, None, export=True, base=base,
                           snapshot=snapshot, reserved=True)
                    state = 'ready'
                except Exception:
                    pass  # expose only a bounded, non-sensitive failure message
                finally:
                    cut._atomic_write(job_path, {'runtime': runtime_id, 'state': state})
            try:
                cut._atomic_write(job_path, {'runtime': runtime_id, 'state': 'running'})
                thread = threading.Thread(target=worker, name='studio-edit-export', daemon=True)
                thread.start()
            except Exception:
                preview_admission.release()
                raise HTTPException(503, 'Could not start the export. Check server storage.') from None
            return JSONResponse({'projectId': project_id, 'revision': body.revision,
                                 'state': 'running', 'receipt': None, 'message': None},
                                status_code=202, headers={'Cache-Control': 'no-store'})

    @api.get('/projects/{project_id}/export-jobs/{revision}')
    def read_export_job(project_id: str, revision: int, request: Request):
        base = owned_root(request)
        open_owned(base, project_id)
        if not 0 <= revision <= 1000000000:
            raise HTTPException(422, 'Choose a valid saved revision.')
        return JSONResponse(export_job_status(base, project_id, revision),
                            headers={'Cache-Control': 'no-store'})

    @api.get('/projects/{project_id}/exports/{revision}/status')
    def export_status(project_id: str, revision: int, request: Request):
        base = owned_root(request)
        open_owned(base, project_id)
        if not 0 <= revision <= 1000000000:
            raise HTTPException(422, 'Choose a valid saved revision.')
        receipt = cached_preview(base, project_id, revision, True)
        return JSONResponse({'projectId': project_id, 'revision': revision,
                             'state': 'ready' if receipt else 'not-ready',
                             'receipt': receipt}, headers={'Cache-Control': 'no-store'})

    @api.post('/projects/{project_id}/exports/{revision}/library')
    def export_to_library(project_id: str, revision: int, request: Request):
        base = owned_root(request)
        project = open_owned(base, project_id).load()
        raw = request.headers.get('x-library-authorization', '')
        if not raw.startswith('Bearer ') or not library.authorize(raw[7:]):
            raise HTTPException(403, 'Connect Library before saving this export there.')
        if save_export is None:
            raise HTTPException(409, 'This host does not support saving exports to Library.')
        receipt = cached_preview(base, project_id, revision, True)
        if not receipt:
            raise HTTPException(404, 'Prepare this revision’s export first.')
        if receipt['bytes'] > 256 * 1024**2:
            raise HTTPException(413, 'Library imports currently support exports up to 256 MiB.')
        if not admission.acquire(blocking=False):
            raise HTTPException(409, 'The editor is busy. Try again shortly.')
        try:
            _, output, _ = preview_paths(base, project_id, revision, True)
            return save_export(output, f"{project.get('title', 'Edited video')} · revision {revision}"[:240])
        except ValueError:
            raise HTTPException(422, 'Could not add this export to Library.') from None
        except OSError:
            raise HTTPException(503, 'Could not save the export. Check server storage.') from None
        finally:
            admission.release()

    @api.get('/projects/{project_id}/exports/{revision}/content')
    def export_content(project_id: str, revision: int, request: Request):
        base = owned_root(request)
        open_owned(base, project_id)
        receipt = cached_preview(base, project_id, revision, True)
        if not receipt:
            raise HTTPException(404, 'This revision has no verified export. Export it first.')
        _, output, _ = preview_paths(base, project_id, revision, True)
        return FileResponse(output, media_type='video/mp4', filename=f'{project_id}-revision-{revision}.mp4',
                            headers={'Cache-Control':'no-store', 'X-Content-SHA256':receipt['sha256']})

    @api.get('/projects/{project_id}/previews/{revision}/content')
    def preview_content(project_id: str, revision: int, request: Request):
        base = owned_root(request)
        open_owned(base, project_id)
        receipt = cached_preview(base, project_id, revision)
        if not receipt:
            raise HTTPException(404, 'This revision has no verified preview. Render it first.')
        _, output, _ = preview_paths(base, project_id, revision)
        return FileResponse(output, media_type='video/mp4',
                            headers={'Cache-Control':'no-store', 'X-Content-SHA256':receipt['sha256']})

    return api
