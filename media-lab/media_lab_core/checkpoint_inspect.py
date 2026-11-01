"""Static checkpoint metadata review; never unpickles or loads tensors."""
import hashlib
import pickletools
from pathlib import Path
import zipfile


def inspect_checkpoint(path, *, expected_bytes, expected_sha256):
    path = Path(path)
    if path.is_symlink() or path.stat().st_size != expected_bytes:
        raise ValueError('Checkpoint size or path does not match.')
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024**2), b''):
            digest.update(chunk)
    if digest.hexdigest() != expected_sha256:
        raise ValueError('Checkpoint hash does not match.')
    with zipfile.ZipFile(path) as archive:
        entries = archive.infolist()
        if len(entries) > 10000:
            raise ValueError('Too many checkpoint entries.')
        metadata = [entry for entry in entries if entry.filename.endswith('/data.pkl')]
        if len(metadata) != 1 or metadata[0].file_size > 8*1024**2:
            raise ValueError('Expected one bounded pickle metadata entry.')
        payload = archive.read(metadata[0])
        globals_found = set()
        dynamic_globals = False
        operations = 0
        for opcode, argument, position in pickletools.genops(payload):
            operations += 1
            if operations > 1000000:
                raise ValueError('Checkpoint metadata exceeds operation limit.')
            if opcode.name == 'GLOBAL':
                globals_found.add(argument)
            if opcode.name in ('STACK_GLOBAL', 'EXT1', 'EXT2', 'EXT4'):
                dynamic_globals = True
        return {'version': 1, 'sha256': digest.hexdigest(), 'bytes': expected_bytes,
                'archive_entries': len(entries), 'metadata_bytes': len(payload),
                'pickle_operations': operations, 'globals': sorted(globals_found),
                'dynamic_globals': dynamic_globals, 'load_approved': False,
                'scope': 'Static disassembly only. Does not prove safe loading or tensor validity.'}
