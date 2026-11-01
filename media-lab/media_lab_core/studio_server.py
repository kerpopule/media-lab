"""Independent owned-job ASGI application, without the legacy controller.

This factory does not listen on a socket or configure public hosting. The caller
supplies its device-token verifier and explicit storage paths. The paired
factory supports scoped device connection and, only when the operator enables it
with an enrolled administrator, the background-removal model setup lifecycle.
"""
import asyncio
import re
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware

# The packaged desktop app's origin, which differs by platform.
TAURI_DESKTOP_ORIGINS = ('tauri://localhost', 'http://tauri.localhost')
from fastapi.responses import FileResponse

from .background_host import BackgroundHost
from .speech_host import SpeechHost
from .triposr_host import TriposrHost
from .music_host import MusicHost
from .video_host import VideoHost
from .image_host import ImageHost
from .director_adapter import LocalDirectorAdapter
from .director_host import DirectorHost
from .job_store import JobStore
from . import studio_jobs, studio_library, studio_inputs
from .studio_gate import Credentials
from . import studio_gate, studio_director, studio_editing


@dataclass(frozen=True)
class Library:
    """Explicit host Library access, separate from device generation permission."""
    media_root: Path
    load_rows: Callable[[], list]
    authorize: Callable[[str], bool]
    load_collections: Callable[[], dict] | None = None

    def __post_init__(self):
        if not Path(self.media_root).is_absolute():
            raise ValueError('The Library media root must be absolute.')
        if not callable(self.load_rows) or not callable(self.authorize):
            raise TypeError('Library catalog and permission callbacks are required.')

    def read_image(self, asset_id):
        item = next((entry for entry in studio_library.catalog(self.load_rows(), self.media_root)
                     if entry[0]['id'] == asset_id and entry[0]['kind'] == 'image'), None)
        if item is None:
            raise HTTPException(404, 'This Library image is unavailable.')
        try:
            with item[1].open('rb') as stream:
                return stream.read(20 * 1024**2 + 1)
        except OSError:
            raise HTTPException(404, 'This Library image is unavailable.') from None


def create_app(*, state_root: Path, artifact_root: Path,
               authorize: Callable[[str], str | None],
               qualification: Path | None = None,
               library: Library | None = None, director_reply=None, save_image=None,
               speech_config: Path | None = None, save_audio=None,
               triposr_config: Path | None = None, pack_root: Path | None = None,
               music_config: Path | None = None, video_config: Path | None = None, save_video=None,
               image_config: Path | None = None, save_generated_image=None) -> FastAPI:
    """Assemble one independent host; construction does not start a worker.

    ``authorize`` must return the authenticated device's stable owner ID or None.
    Use the existing studio_jobs.identity verifier with a host-owned secret and
    role codes. Never derive an owner from a request-supplied ID or client IP.
    ``artifact_root`` must match the qualification receipt's canonical root.
    """
    state_root, artifact_root = Path(state_root), Path(artifact_root)
    if not state_root.is_absolute() or not artifact_root.is_absolute():
        raise ValueError('Explicit absolute state and artifact roots are required.')
    if qualification is not None and not Path(qualification).is_absolute():
        raise ValueError('The qualification receipt path must be absolute.')
    if speech_config is not None and not Path(speech_config).is_absolute():
        raise ValueError('The speech configuration path must be absolute.')
    if triposr_config is not None and not Path(triposr_config).is_absolute():
        raise ValueError('The 3D configuration path must be absolute.')
    if music_config is not None and not Path(music_config).is_absolute():
        raise ValueError('The music configuration path must be absolute.')
    if video_config is not None and not Path(video_config).is_absolute():
        raise ValueError('The video configuration path must be absolute.')
    if image_config is not None and not Path(image_config).is_absolute():
        raise ValueError('The image configuration path must be absolute.')
    if not callable(authorize):
        raise TypeError('A device-token verifier is required.')

    store = None

    def get_store():
        # Initialize once during startup, before concurrent requests arrive.
        if store is None:
            raise RuntimeError('The Studio server lifespan has not started.')
        return store

    host = BackgroundHost(get_store, artifact_root, qualification)
    speech = SpeechHost(get_store, artifact_root, speech_config)
    model3d = TriposrHost(get_store, artifact_root, triposr_config)
    music = MusicHost(get_store, artifact_root, music_config)
    video = VideoHost(get_store, artifact_root, video_config)
    picture = ImageHost(get_store, artifact_root, image_config)
    hosts = (host, speech, model3d, music, video, picture)

    def engines():
        return [engine for each in hosts for engine in each.engines()]

    def admit(payload):
        # Route by the exact engine identity; an unknown engine is refused by
        # whichever host is asked last, without ever guessing a substitute.
        for each in hosts:
            if any(engine['id'] == payload.get('engineId') for engine in each.engines()):
                return each.admit(payload)
        raise HTTPException(503, 'No independently qualified engine matches this request.')
    director = DirectorHost(director_reply) if isinstance(director_reply, LocalDirectorAdapter) else None

    @asynccontextmanager
    async def lifespan(app):
        nonlocal store
        store = await asyncio.to_thread(JobStore, state_root / 'studio-jobs.sqlite')
        try:
            host.start()
            # Operator-configured packs honour the administrator's saved on/off choice.
            from .studio_packs import read_preferences
            preferences = read_preferences(state_root.parent) if pack_root is None else read_preferences(pack_root)
            if preferences['speech']:
                speech.start()
            if preferences['model3d']:
                model3d.start()
            if preferences['music']:
                music.start()
            if preferences['video']:
                video.start()
            if preferences['image']:
                picture.start()
            if director is not None:
                director.start()
            yield
        finally:
            try:
                if director is not None:
                    await asyncio.to_thread(director.stop)
            finally:
                try:
                  try:
                    await asyncio.to_thread(picture.stop)
                  finally:
                    await asyncio.to_thread(video.stop)
                finally:
                  try:
                    await asyncio.to_thread(music.stop)
                  finally:
                    try:
                        await asyncio.to_thread(model3d.stop)
                    finally:
                        try:
                            await asyncio.to_thread(speech.stop)
                        finally:
                            await asyncio.to_thread(host.stop)
                            store = None

    app = FastAPI(title='VibeX Studio owned jobs', lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.background_host = host
    app.state.speech_host = speech
    app.state.triposr_host = model3d
    app.state.music_host = music
    app.state.video_host = video
    app.state.image_host = picture
    app.state.pack_root = pack_root if pack_root is not None else state_root.parent
    app.include_router(studio_jobs.router(
        get_store, authorize, engines, admit, artifact_root, save_image=save_image, save_audio=save_audio, save_video=save_video, save_generated_image=save_generated_image))
    app.include_router(studio_director.router(authorize, director if director is not None else director_reply, library))
    if library is not None:
        app.include_router(studio_library.router(
            library.load_rows, library.media_root, library.authorize))
        app.include_router(studio_inputs.router(
            get_store, authorize, library.authorize, library.read_image))
    return app


def setup_web_root():
    """The administrator setup page: packaged beside this module, or the checkout's static tree."""
    for candidate in (Path(__file__).with_name('setup-web'), Path(__file__).resolve().parents[1]/'static'):
        if (candidate/'background-setup.html').is_file() and (candidate/'background-setup.js').is_file():
            return candidate
    raise ValueError('The administrator setup page assets are missing from this installation.')


def create_paired_app(*, state_root: Path, artifact_root: Path, credentials: Credentials,
                      media_root: Path, load_rows: Callable[[], list],
                      qualification: Path | None = None,
                      allowed_origins: tuple[str, ...] = (), director_reply=None,
                      web_root: Path | None = None, save_export=None, load_collections=None, save_image=None,
                      admin=None, setup=None, speech_config: Path | None = None, save_audio=None,
                      triposr_config: Path | None = None, pack_root: Path | None = None,
                      music_config: Path | None = None, video_config: Path | None = None, save_video=None,
                      image_config: Path | None = None, save_generated_image=None) -> FastAPI:
    """Single-process development host with the existing Studio pairing contract.

    Browser clients require explicitly configured origins; native clients do not
    use CORS. This factory does not enable tunnels, TLS, services or listeners.
    ``setup`` (a background_setup.Setup bound to this host's artifact root) and
    ``admin`` (a studio_admin.AdminGate) together expose administrator-only model
    setup; either alone is refused so the capability is never half-wired.
    """
    if (admin is None) != (setup is None):
        raise ValueError('Model setup requires both an administrator gate and a setup controller.')
    # Tauri's packaged desktop webview has a DIFFERENT origin per platform:
    # tauri://localhost on macOS/Linux, http://tauri.localhost on Windows.
    # They are the same logical caller -- the packaged app -- so allowing
    # either one allows its platform twin. Without this, a Windows desktop
    # app is refused by CORS before any request leaves the machine and the
    # app can only report a generic "could not reach Media Lab" (found
    # 2026-09-09: a host allowing only the macOS/Linux origin could not be
    # paired from Windows at all).
    allowed_origins = list(allowed_origins)
    if any(origin in TAURI_DESKTOP_ORIGINS for origin in allowed_origins):
        allowed_origins += [o for o in TAURI_DESKTOP_ORIGINS if o not in allowed_origins]
    for origin in allowed_origins:
        # These are opt-in, like every HTTP origin; never accept arbitrary
        # schemes or the opaque "null" origin shared by sandboxed/file documents.
        if origin in TAURI_DESKTOP_ORIGINS:
            continue
        parsed = urlsplit(origin)
        if (parsed.scheme not in ('http', 'https') or not parsed.hostname or
                parsed.username or parsed.password or parsed.path or parsed.query or
                parsed.fragment or '*' in origin or origin != f'{parsed.scheme}://{parsed.netloc}'):
            raise ValueError('Allow exact HTTP(S) browser origins without paths, or a Tauri desktop origin.')
    app = create_app(state_root=state_root, artifact_root=artifact_root,
                     authorize=credentials.authorize, qualification=qualification,
                     library=Library(media_root, load_rows, credentials.authorize_library, load_collections),
                     director_reply=director_reply, save_image=save_image,
                     speech_config=speech_config, save_audio=save_audio, triposr_config=triposr_config,
                     pack_root=pack_root, music_config=music_config, video_config=video_config, save_video=save_video, image_config=image_config, save_generated_image=save_generated_image)
    app.include_router(studio_gate.router(credentials, library_available=True, editing_available=True))
    app.include_router(studio_editing.router(state_root / 'editing', credentials.authorize_editing,
                                            Library(media_root, load_rows, credentials.authorize_library, load_collections), save_export=save_export))
    if load_collections is not None:
        from .studio_collections import router as collection_router
        def collection_assets():
            root = Path(media_root).resolve()
            return {'/media/' + path.relative_to(root).as_posix():
                    {'id': row['id'], 'kind': row['kind'], 'title': row['title']}
                    for row, path in studio_library.catalog(load_rows(), root)}
        app.include_router(collection_router(load_collections, credentials.authorize_library, collection_assets))
    app.add_middleware(CORSMiddleware, allow_origins=list(allowed_origins),
                       allow_methods=['GET', 'POST'],
                       expose_headers=['X-Content-SHA256', 'X-Studio-Portable'],
                       allow_headers=['Authorization', 'Content-Type', 'X-Library-Authorization'])

    if setup is not None:
        from . import background_setup
        if Path(setup.artifacts) != Path(artifact_root):
            raise ValueError('Model setup must manage the same artifact root as the job host.')
        host = app.state.background_host
        setup.get_host = lambda: host
        app.include_router(admin.router())
        app.include_router(background_setup.router(lambda: setup, admin.authorized, lambda: True))
        from .studio_packs import Packs, router as packs_router
        app.state.packs = Packs(app.state.pack_root, {'speech': app.state.speech_host, 'model3d': app.state.triposr_host, 'music': app.state.music_host, 'video': app.state.video_host, 'image': app.state.image_host})
        app.include_router(packs_router(app.state.packs, admin.authorized))
        pages = setup_web_root()

        @app.get('/setup/background')
        def setup_page():
            return FileResponse(str(pages/'background-setup.html'), headers={'Cache-Control': 'no-cache'})

        @app.get('/static/background-setup.js')
        def setup_script():
            return FileResponse(str(pages/'background-setup.js'), media_type='text/javascript',
                                headers={'Cache-Control': 'no-cache'})

    @app.get('/manifest.json')
    def manifest():
        return {'name': 'VibeX Media Lab', 'short_name': 'Media Lab',
                'description': 'Independent development Studio host',
                'vibexStudio': {'version': 1, 'legacyQueue': False, 'webInterface': web_root is not None, 'integratedStudio': web_root is not None, 'editingDrafts': True, 'editingPreview': True, 'editingExport': True, 'editingLibrarySave': callable(save_export), 'editingAddSources': True, 'libraryCollections': load_collections is not None,
                                'modelSetup': setup is not None, 'speech': speech_config is not None, 'model3d': triposr_config is not None, 'music': music_config is not None, 'video': video_config is not None, 'image': image_config is not None}}

    if web_root is not None:
        from starlette.staticfiles import StaticFiles
        from starlette.exceptions import HTTPException
        web_root = Path(web_root).resolve(strict=True)
        if not (web_root / 'index.html').is_file():
            raise ValueError('Web root must contain an exported index.html.')

        class StudioFiles(StaticFiles):
            async def get_response(self, path, scope):
                # Never turn a missing API into a successful HTML response.
                if path == 'api' or path.startswith('api/'):
                    raise HTTPException(status_code=404)
                try:
                    response = await super().get_response(path, scope)
                except HTTPException as error:
                    if error.status_code != 404 or Path(path).suffix:
                        raise
                    # Expo exports one template for the dynamic project route.
                    # Only map valid project IDs; missing assets/APIs must remain 404.
                    target = ('project/[id].html' if re.fullmatch(r'project/[A-Za-z0-9_-]{1,128}/?', path)
                              else path.rstrip('/') + '.html')
                    try:
                        response = await super().get_response(target, scope)
                    except HTTPException as missing:
                        # A direct load of an unknown page gets the export's
                        # branded not-found screen, still with a 404 status.
                        page = web_root / '+not-found.html'
                        if missing.status_code != 404 or not page.is_file():
                            raise
                        response = FileResponse(str(page), status_code=404)
                # Exported documents point at revision-specific JS bundles.
                # Revalidate them after updates instead of letting browsers
                # heuristically reuse an old entry point. Apply to 304s too.
                if (response.headers.get('content-type', '').startswith('text/html')
                        or not Path(path).suffix or path.endswith('.html')):
                    response.headers['Cache-Control'] = 'no-cache'
                return response

        app.mount('/', StudioFiles(directory=web_root, html=True), name='studio-web')

    return app
