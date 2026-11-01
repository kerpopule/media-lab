"""Package only the reviewed independent controller source; no runtimes or models."""
import argparse
import hashlib
import json
from pathlib import Path
from .notice_bundle import build_bundle

MODULES = (
    # Shared verified HDR/SDR finishing (lazy imports; no model runtime).
    'hdr_color', 'hdr_finish', 'hdr_delivery', 'sdr_delivery', 'dual_delivery',
    '__init__', 'background_host', 'background_jobs', 'background_lifecycle',
    'birefnet_cpu', 'cpu_jobs', 'cpu_worker', 'director_adapter', 'director_context',
    'director_host', 'director_library', 'director_probe', 'director_transport',
    'game_assets', 'glb_contract', 'host_resources', 'job_store', 'model_catalog',
    'setup_wizard', 'speech_artifact', 'studio_cli', 'studio_director', 'studio_gate',
    # Per-host settings and the engine licence gate (Qwen-Image-2.1 is opt-in).
    'local_config', 'engine_licences',
    'media_inventory', 'media_migration', 'migrated_catalog', 'migrated_edits',
    'preserved_edit_import', 'studio_collections', 'storyboard_edit',
    'cut', 'studio_editing', 'studio_import', 'studio_inputs', 'studio_jobs', 'studio_library', 'studio_server',
    # Independent server model management (administrator-only setup lifecycle).
    'background_install', 'background_remove', 'background_setup', 'runtime_inventory', 'studio_admin',
    # Experimental English speech pack (owned queue, isolated renderer, verified WAV gate).
    'speech_host', 'speech_jobs', 'speech_worker', 'speech_render', 'speech_request', 'chatterbox_cpu', 'perth_cpu',
    # Candidate image-to-3D pack (TripoSR CPU worker, canonical mesh order, verified GLB publication).
    'triposr_host', 'triposr_jobs', 'triposr_worker', 'triposr_result', 'triposr_cpu', 'triposr_compatibility',
    'triposr_integrity', 'mesh_order',
    'studio_service', 'studio_packs',
    # ACE-Step music pack (GPU, canonical inference lease).
    'music_host', 'music_jobs', 'music_worker', 'music_render', 'music_request', 'music_artifact',
    # Wan2.2 TI2V-5B video pack (GPU, canonical inference lease, warm renderer).
    'video_host', 'video_jobs', 'video_worker', 'video_render', 'video_request', 'video_artifact',
    # Qwen-Image-2.1 image pack (GPU, canonical inference lease, warm renderer).
    'image_host', 'image_jobs', 'image_worker', 'image_render', 'image_request', 'image_artifact',
)
DATA = ('birefnet-cpu.json', 'birefnet-cpu-linux-arm64.json', 'models.example.toml',
        # Pinned, hash-locked model runtime requirements read by the setup plan/installer.
        'birefnet-cpu.requirements.in', 'birefnet-cpu.requirements.lock',
        'birefnet-cpu-linux-arm64.requirements.in', 'birefnet-cpu-linux-arm64.requirements.lock',
        # Pinned Qwen-Image-2.1 image runtime (linux aarch64, GB10): the lock is the
        # canonical dependency set for the diffusers commit that carries QwenImage21Pipeline.
        'qwen-image-21-runtime.requirements.in', 'qwen-image-21-runtime.requirements.lock',
        # Reviewed supplemental dependency notices recorded by the runtime inventory.
        'runtime-notices/manifest.json', 'runtime-notices/README.md',
        'runtime-notices/antlr4-python3-runtime-4.9.3-LICENSE.txt', 'runtime-notices/tokenizers-0.22.2-LICENSE.txt',
        # Reviewed TripoSR runtime specifications and rebuilt-file manifest read by the worker path.
        'triposr-runtime.json', 'triposr-runtime-without-vision.json', 'triposr-rebuilt-files.json')
# The administrator setup page ships beside the package so an unpacked
# controller can serve it without the legacy static tree.
SETUP_WEB = ('background-setup.html', 'background-setup.js')


def package_source(root, output, *, include_runtime_locks=False):
    root=Path(root)
    paths=['LICENSE', *[f'media_lab_core/{name}.py' for name in MODULES],
           *[f'media_lab_core/data/{name}' for name in DATA]]
    copies={f'static/{name}':f'media_lab_core/setup-web/{name}' for name in SETUP_WEB}
    if include_runtime_locks:
        paths.extend(f'media_lab_core/data/controller-{platform}.requirements.{suffix}'
                     for platform in ('macos-arm64', 'linux-arm64', 'macos-x64')
                     for suffix in ('in', 'lock'))
    entries=[]
    for name in [*paths,*copies]:
        data=(root/name).read_bytes()
        entries.append({'source':name,'path':copies.get(name,name),'bytes':len(data),
                        'sha256':hashlib.sha256(data).hexdigest()})
    return build_bundle(root,entries,Path(output))


def main(argv=None):
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args(argv)
    result=package_source(Path(__file__).resolve().parents[1],args.output)
    print(json.dumps({**result,'scope':'Controller source only; no dependency installation or engine qualification.'},sort_keys=True))
    return 0


if __name__=='__main__':
    raise SystemExit(main())
