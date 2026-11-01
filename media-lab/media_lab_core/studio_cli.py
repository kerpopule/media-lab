"""Independent development host: init, inspect, pair, admin enrollment, and foreground serve.

Does not import the legacy controller, register services, or configure public
networking. Model installation is exposed only with ``serve --model-setup`` after
``admin-init`` enrolled an administrator. Server dependencies must already be installed.
"""
import argparse
import json
import os
from pathlib import Path
import secrets
import stat


def _root(value):
    return Path(value).expanduser().resolve()


def initialize(root):
    if os.name!='posix':
        raise ValueError('Independent host credential storage is not qualified on this platform.')
    root.mkdir(mode=0o700,parents=True,exist_ok=False)
    for name in ('media','artifacts','state'):
        (root/name).mkdir(mode=0o700)
    credentials={'secret':secrets.token_hex(32),'code':secrets.token_hex(32)}
    fd=os.open(root/'credentials.json',os.O_WRONLY|os.O_CREAT|os.O_EXCL,0o600)
    with os.fdopen(fd,'w') as stream:json.dump(credentials,stream)
    (root/'library.json').write_text('[]\n')


def read_credentials(root):
    from .studio_gate import Credentials
    if os.name!='posix':
        raise ValueError('Independent host credential storage is not qualified on this platform.')
    fd=os.open(root/'credentials.json',os.O_RDONLY|os.O_NOFOLLOW)
    with os.fdopen(fd,'r') as stream:
        info=os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid!=os.getuid() or info.st_mode&0o077 or info.st_size>4096:
            raise ValueError('Credentials must be a private regular file owned by this user.')
        data=json.load(stream)
    if not isinstance(data,dict) or set(data)!={'secret','code'}:
        raise ValueError('Invalid host credentials.')
    return Credentials(**data)


def read_library_catalog(root):
    with (root/'library.json').open('rb') as stream:data=stream.read(8*1024**2+1)
    if len(data)>8*1024**2:raise ValueError('The Library catalog is too large.')
    result=json.loads(data)
    if not isinstance(result,list):raise ValueError('The Library catalog must be a list.')
    return result


def inspect_host(root):
    """Validate existing host files without starting a listener or revealing keys."""
    read_credentials(root)
    for name in ('media','artifacts','state'):
        path=root/name
        if path.is_symlink() or not path.is_dir():
            raise ValueError('An independent host directory is missing or symbolic.')
    rows=read_library_catalog(root)
    return {'version':1,'runtime':'independent-studio','root':str(root),
            'configuration_valid':True,'credential_storage_verified':True,'library_catalog_entries':len(rows),
            'running':None,'engines_qualified':None,
            'scope':'Existing file configuration only; server health and engine qualification require separate checks.'}


def import_media(root, source, title=None, *, edited=False, cutout=False, speech=False, music=False, generated=False, generated_image=False):
    """Copy a local file into this user's host; idempotent by content and type."""
    import hashlib
    import tempfile
    import time
    import re
    from urllib.parse import quote, unquote, urlsplit
    from . import cut
    from .studio_library import KINDS
    inspect_host(root)
    source = Path(source).expanduser()
    suffix = source.suffix.lower()
    if KINDS.get(suffix) not in ('image', 'video', 'audio'):
        raise ValueError('Choose a supported image, video or audio file.')
    if title is not None and not 1 <= len(title.strip()) <= 240:
        raise ValueError('Choose a title between 1 and 240 characters.')
    maximum = 256 * 1024**2
    temporary = None
    try:
        with source.open('rb') as reader:
            info = os.fstat(reader.fileno())
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= maximum:
                raise ValueError('Choose a regular media file up to 256 MiB.')
            with tempfile.NamedTemporaryFile(dir=root/'media', suffix=suffix, delete=False) as writer:
                temporary = Path(writer.name)
                digest = hashlib.sha256()
                size = 0
                while chunk := reader.read(1024**2):
                    size += len(chunk)
                    if size > maximum:
                        raise ValueError('Media exceeds 256 MiB.')
                    writer.write(chunk)
                    digest.update(chunk)
                writer.flush()
                os.fsync(writer.fileno())
        try:
            media = cut.probe_gallery_file(temporary)
        except (cut.CutError, OSError) as error:
            raise ValueError('Media could not be inspected. Check the file and FFprobe installation.') from error
        if media['kind'] == 'music':
            valid = media['has_audio']
        else:
            valid = bool(media.get('width') and media.get('height'))
        if not valid:
            raise ValueError('The file has no usable media stream.')
        asset_id = 'import-' + digest.hexdigest() + '-' + suffix[1:]
        display_title = title.strip() if title else source.stem[:240]
        label = re.sub(r'[^\w .()-]+', '-', display_title).strip(' .-')[:70].rstrip(' .') or 'Untitled'
        folder = {'image':'Images/Generated' if generated_image else 'Images/Cutouts' if cutout else 'Images/Imported', 'video':'Videos/Generated' if generated else 'Videos/Edits' if edited else 'Videos/Imported',
                  'audio':'Audio/Speech' if speech else 'Audio/Music' if music else 'Audio/Imported'}[KINDS[suffix]]
        filename = label + '--' + asset_id + suffix
        with (root/'state/library-import.lock').open('a+b') as lock, cut._exclusive_file_lock(lock):
            rows = read_library_catalog(root)
            existing = next((row for row in rows if isinstance(row, dict) and row.get('id') == asset_id), None)
            relative = folder + '/' + filename
            if existing is not None:
                url = urlsplit(str(existing.get('url', '')))
                if url.scheme or url.netloc or url.query or url.fragment or not url.path.startswith('/media/'):
                    raise ValueError('Existing import has an invalid Library path.')
                relative = unquote(url.path[len('/media/'):])
            if '\\' in relative or any(part in ('', '.', '..') for part in relative.split('/')):
                raise ValueError('Invalid imported media path.')
            target = root/'media'/relative
            parent = root/'media'
            for part in Path(relative).parts[:-1]:
                parent = parent/part
                if parent.is_symlink():
                    raise ValueError('Library folders must not be symbolic links.')
                parent.mkdir(mode=0o700, exist_ok=True)
            if not target.resolve().is_relative_to((root/'media').resolve()):
                raise ValueError('Imported media escapes Library storage.')
            if target.exists():
                if target.is_symlink() or hashlib.sha256(target.read_bytes()).hexdigest() != digest.hexdigest():
                    raise ValueError('Existing imported media does not match its content identity.')
            else:
                os.replace(temporary, target)
            if existing is None:
                rows.append({'id':asset_id, 'url':'/media/'+quote(relative,safe='/'), 'status':'done',
                             'title':display_title,
                             'ts':time.time(), 'engine':'Speech (Chatterbox English CPU)' if speech else 'Music (ACE-Step)' if music else 'Video (Wan2.2 TI2V-5B)' if generated else 'Image (Qwen-Image-2.1)' if generated_image else 'Imported file'})
                if len(json.dumps(rows).encode()) > 8*1024**2:
                    raise ValueError('The Library catalog is full.')
                cut._atomic_write(root/'library.json', rows)
        return {'id':asset_id, 'bytes':size, 'sha256':digest.hexdigest(),
                'already_imported':existing is not None, 'kind':KINDS[suffix]}
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def application(args):
    from .studio_server import create_paired_app
    from .director_transport import LocalDirectorTransport
    from .director_probe import LocalVllmIdleProbe
    from .director_adapter import LocalDirectorAdapter
    root=_root(args.root)
    credentials=read_credentials(root)
    qualification=Path(args.background_receipt) if args.background_receipt else None
    admin=setup=None
    if args.model_setup:
        from .studio_admin import AdminGate
        from .background_setup import Setup, managed_receipt
        admin=AdminGate(root)  # Refuses to start without an enrolled administrator.
        # Fixed app-owned stores: the job host already uses root/artifacts, so the
        # installer qualifies into that same root and keeps runtimes beside it.
        setup=Setup(root,artifact_root=root/'artifacts',runtime_root=root/'runtimes',
                    python=args.setup_python,uv=args.setup_uv,
                    externally_controlled=lambda:args.background_receipt is not None)
        if qualification is None:
            saved=managed_receipt(root,root/'artifacts')
            # Restore the saved activation choice; the host re-verifies the receipt
            # and advertises the engine only after that verification passes.
            qualification=Path(saved) if saved else None
    configured=[args.director_model is not None,args.director_port is not None,args.inference_lock is not None]
    director=None
    if any(configured):
        if not all(configured):
            raise ValueError('Sparky requires an exact model, runtime port and canonical inference lock together.')
        transport=LocalDirectorTransport(port=args.director_port,model=args.director_model)
        director=LocalDirectorAdapter(transport=transport,lease_path=Path(args.inference_lock),runtime_idle=LocalVllmIdleProbe(transport))
    def rows():
        return read_library_catalog(root)
    rows()  # Refuse invalid catalog before starting a listener.
    def collections():
        path = root/'collections.json'
        if path.is_symlink() or path.stat().st_size > 8 * 1024**2:
            raise ValueError('Saved collections must be a local JSON file under 8 MiB.')
        value = json.loads(path.read_text())
        if not isinstance(value, dict):
            raise ValueError('Saved collections must be an object.')
        return value
    has_collections = (root/'collections.json').exists()
    if has_collections:
        collections()
    app = create_paired_app(state_root=root/'state',artifact_root=root/'artifacts',
        media_root=root/'media',load_rows=rows,credentials=credentials,
        qualification=qualification,
        allowed_origins=tuple(args.origin),director_reply=director,
        web_root=Path(args.web_root) if args.web_root else None,
        save_export=lambda path,title: import_media(root,path,title,edited=True),
        save_image=lambda path,title: import_media(root,path,title,cutout=True),
        save_audio=lambda path,title,kind='speech': import_media(root,path,title,speech=kind=='speech',music=kind=='music'),
        speech_config=Path(args.speech_config) if args.speech_config else None,
        triposr_config=Path(args.triposr_config) if args.triposr_config else None,pack_root=root,
        music_config=Path(args.music_config) if args.music_config else None,
        video_config=Path(args.video_config) if args.video_config else None,
        save_video=lambda path,title: import_media(root,path,title,generated=True),
        image_config=Path(args.image_config) if args.image_config else None,
        save_generated_image=lambda path,title: import_media(root,path,title,generated_image=True),
        load_collections=collections if has_collections else None,admin=admin,setup=setup)
    from .studio_import import router as import_router
    # Prepend API routes before the optional catch-all static mount.
    app.router.routes[0:0] = import_router(root, credentials).routes
    return app


def parser():
    p=argparse.ArgumentParser(description=__doc__)
    commands=p.add_subparsers(dest='command',required=True)
    service=commands.add_parser('service',help='Install, inspect or remove a systemd --user service for this host (Linux)')
    from .studio_service import add_arguments
    add_arguments(service)
    for name in ('init','inspect','pair','serve','import','admin-init','admin-revoke'):
        s=commands.add_parser(name)
        s.add_argument('root',help='Independent host data directory')
        if name=='admin-init':
            s.add_argument('--rotate',action='store_true',help='Replace an existing administrator code and end its sessions')
        if name=='import':
            s.add_argument('file',help='Local image, video or audio to copy into this host')
            s.add_argument('--title',help='Library display title')
        if name=='serve':
            s.add_argument('--bind',default='127.0.0.1',help='Listen address; defaults to local access only')
            s.add_argument('--port',type=int,default=7864)
            s.add_argument('--web-root',help='Exported Studio web directory served alongside the API')
            s.add_argument('--origin',action='append',default=[],help='Exact browser origin; repeat to allow more than one')
            s.add_argument('--background-receipt',help='Absolute path to an existing verified background qualification')
            s.add_argument('--model-setup',action='store_true',help='Expose administrator model setup (requires admin-init)')
            s.add_argument('--speech-config',help='Absolute path to a verified experimental speech pack configuration (JSON)')
            s.add_argument('--triposr-config',help='Absolute path to a verified candidate image-to-3D pack configuration (JSON)')
            s.add_argument('--music-config',help='Absolute path to a verified ACE-Step music pack configuration (JSON)')
            s.add_argument('--video-config',help='Absolute path to a verified Wan2.2 video pack configuration (JSON)')
            s.add_argument('--image-config',help='Absolute path to a verified Qwen-Image-2.1 image pack configuration (JSON)')
            s.add_argument('--setup-python',help='Python 3.12 interpreter used to build the isolated model runtime')
            s.add_argument('--setup-uv',help='uv binary used to install the pinned model runtime')
            s.add_argument('--director-model',help='Exact already-running local vLLM model ID')
            s.add_argument('--director-port',type=int,help='Canonical local vLLM port, not an alias shim')
            s.add_argument('--inference-lock',help='Absolute canonical inference lock used by every GPU consumer')
    return p


def main(argv=None):
    args=parser().parse_args(argv)
    root=_root(args.root) if getattr(args,'root',None) else None
    try:
        if args.command=='init':
            initialize(root)
            print(f'Created independent host data at {root}. No models or services started.')
        elif args.command=='inspect':
            print(json.dumps(inspect_host(root),sort_keys=True))
        elif args.command=='import':
            print(json.dumps(import_media(root,args.file,args.title),sort_keys=True))
        elif args.command=='pair':
            print(read_credentials(root).code)
        elif args.command=='admin-init':
            from .studio_admin import enroll
            code=enroll(root,rotate=args.rotate)
            print('Administrator code (shown once; it is stored only as a hash):')
            print(code)
        elif args.command=='admin-revoke':
            from .studio_admin import revoke
            revoke(root)
            print('All administrator sessions were revoked. The administrator code is unchanged.')
        elif args.command=='service':
            from .studio_service import main_from
            print(json.dumps(main_from(args),sort_keys=True))
        else:
            if not 1<=args.port<=65535:raise ValueError('Choose a valid listen port.')
            app=application(args)
            import uvicorn
            uvicorn.run(app,host=args.bind,port=args.port)
    except (ValueError,OSError) as error:
        # Avoid dumping credential/config contents or arbitrary server replies.
        if args.command=='inspect':
            print(json.dumps({'version':1,'error':type(error).__name__,'configuration_valid':False}))
        elif args.command=='service' and isinstance(error,ValueError):
            # Service messages are operator-facing and carry no secrets.
            print(f'Service {args.action} failed: {error}')
        else:
            print(f'Independent host could not {args.command}: {type(error).__name__}. Check paths, permissions and configuration.')
        return 2
    return 0


if __name__=='__main__':
    raise SystemExit(main())
