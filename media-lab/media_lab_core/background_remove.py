"""Remove a development runtime while retaining every generated artifact."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import shutil
import time

from .background_host import verify_receipt
from .background_install import write_receipt
from .background_lifecycle import lifecycle_slot
from .cpu_worker import cpu_slot

MANAGED = ('package', 'runtime', 'download-cache', 'model-cache')


def remove(root, *, execute=False):
    root = Path(root).absolute()
    if root.resolve() != root or not root.is_dir():
        raise ValueError('Choose an existing canonical installation directory.')
    receipt_path = root/'install.json'
    if receipt_path.is_symlink():
        raise ValueError('Invalid installation receipt.')
    receipt = json.loads(receipt_path.read_text())
    artifacts = Path(receipt['identity']['artifact_root'])
    if not artifacts.is_absolute() or artifacts.resolve() != artifacts or root == artifacts or artifacts.is_relative_to(root) or root.is_relative_to(artifacts):
        raise ValueError('Installation and artifact directories must be separate canonical paths.')
    with lifecycle_slot(artifacts), cpu_slot(artifacts):
        fd = os.open(root/'.install.lock', os.O_RDWR | os.O_NOFOLLOW)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            # Reload under all locks before deciding whether removal can proceed.
            current = json.loads(receipt_path.read_text())
            if current != receipt:
                raise ValueError('Installation changed; inspect it again.')
            disabled = artifacts/'runtime-disabled.json'
            if receipt.get('status') not in ('removing', 'removed'):
                verified = verify_receipt(artifacts/'qualification.json')
                if (verified['runtime'], verified['package'], verified['cache'], verified['artifact_root']) != (
                        str(root/'runtime/bin/python'), str(root/'package'), str(root/'model-cache'), str(artifacts)):
                    raise ValueError('Qualification belongs to a different installation.')
            elif disabled.is_symlink() or json.loads(disabled.read_text()).get('installation') != str(root):
                raise ValueError('Removal evidence does not match this installation.')
            for name in MANAGED:
                if (root/name).is_symlink():
                    raise ValueError('Managed directories cannot be symbolic links.')
            plan = {'version': 1, 'installation': str(root), 'remove': [str(root/name) for name in MANAGED],
                    'retain_artifacts': str(artifacts), 'retain_receipts_and_logs': str(root)}
            if not execute or receipt.get('status') == 'removed':
                return {**plan, 'status': receipt.get('status') if execute else 'planned'}
            write_receipt(disabled, {'version':1, 'installation':str(root), 'disabled_at':time.time()})
            receipt.update(status='removing', stage='remove', updated_at=time.time())
            write_receipt(receipt_path, receipt)
            for name in MANAGED:
                directory = root/name
                if directory.exists():
                    shutil.rmtree(directory)
            receipt.update(status='removed', stage='complete', updated_at=time.time())
            write_receipt(receipt_path, receipt)
            return {**plan, 'status':'removed'}
        finally:
            os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--remove', action='store_true', help='Execute the displayed removal scope; default is plan only.')
    args = parser.parse_args()
    print(json.dumps(remove(args.root, execute=args.remove), indent=2))


if __name__ == '__main__':
    main()
