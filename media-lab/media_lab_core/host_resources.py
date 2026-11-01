"""Read-only installation resource snapshot; never model/GPU qualification."""
import math
from pathlib import Path
import platform
import shutil


def assess_resources(models, available_memory_bytes, free_disk_bytes):
    if any(not isinstance(n, int) or isinstance(n, bool) or n < 0 for n in (available_memory_bytes, free_disk_bytes)):
        raise ValueError('Host resource measurements are unavailable.')
    reasons=[]
    if not models:reasons.append('Choose capabilities before checking installation resources.')
    for model in models:
        if any(not math.isfinite(n) or n <= 0 for n in (model.disk_gb, model.memory_floor_gb)):
            reasons.append(f'{model.id}: resource estimates are not qualified.')
    disk=sum(model.disk_gb for model in models)
    memory=max((model.memory_floor_gb for model in models),default=0)
    if math.isfinite(disk) and disk*10**9>free_disk_bytes:reasons.append('Insufficient free storage for the catalog download estimate.')
    if math.isfinite(memory) and memory*10**9>available_memory_bytes:reasons.append('Insufficient available memory for the catalog runtime floor.')
    return {'available_memory_bytes':available_memory_bytes,'free_disk_bytes':free_disk_bytes,
            'catalog_resources_fit':not reasons,'blocked_reasons':reasons,'hardware_verified':False,
            'scope':'Snapshot against catalog estimates only; temporary installation space, GPU, drivers and concurrent workloads require separate preflight.'}


def inspect_host_resources(models, storage_root):
    import psutil
    target=Path(storage_root).expanduser().absolute()
    probe=target
    while not probe.exists():probe=probe.parent
    if not probe.is_dir():raise ValueError('Choose an installation storage directory.')
    return {**assess_resources(models,psutil.virtual_memory().available,shutil.disk_usage(probe).free),
            'system':platform.system(),'machine':platform.machine(),'storage_root':str(target),'measured_filesystem_path':str(probe.resolve())}
