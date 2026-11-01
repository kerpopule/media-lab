import hashlib
import tempfile
import json
from unittest.mock import patch
from media_lab_core import triposr_cpu
import unittest
from pathlib import Path
from PIL import Image
from media_lab_core.triposr_cpu import generate, verify_file, verify_package

class TripoSRWorkerTests(unittest.TestCase):
    def test_component_integrity(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            path = root / 'component'
            path.write_bytes(b'approved')
            digest = hashlib.sha256(b'approved').hexdigest()
            verify_file(path, digest, 8)
            for sha, limit in [('0' * 64, 8), (digest, 7)]:
                with self.assertRaises(ValueError): verify_file(path, sha, limit)
            link = root / 'link'
            link.symlink_to(path)
            with self.assertRaises(ValueError): verify_file(link, digest, 8)

    def test_untrusted_source_receipt_is_not_authority(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / 'offline-source').mkdir()
            (root / 'offline-source/vibex-source-modifications.json').write_text('{"files": {}}')
            with self.assertRaisesRegex(ValueError, 'hash mismatch'): verify_package(root)

    def test_local_encoder_config_is_pinned(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            source = root / 'offline-source'
            source.mkdir()
            receipt = source / 'vibex-source-modifications.json'
            receipt.write_text(json.dumps({'files': {}}))
            config = source / 'dino-config.json'
            config.write_bytes(b'approved')
            (root / 'config.yaml').write_bytes(b'config')
            (root / 'model.safetensors').write_bytes(b'model')
            digest = lambda data: hashlib.sha256(data).hexdigest()
            with patch.multiple(triposr_cpu,
                                SOURCE_RECEIPT_SHA=digest(receipt.read_bytes()),
                                DINO_CONFIG_SHA=digest(b'approved'),
                                CONFIG_SHA=digest(b'config'), MODEL_SHA=digest(b'model')):
                self.assertEqual(verify_package(root), source)
                config.write_bytes(b'altered')
                with self.assertRaisesRegex(ValueError, 'dino-config.json'):
                    verify_package(root)
                config.unlink()
                config.symlink_to(root / 'config.yaml')
                with self.assertRaisesRegex(ValueError, 'dino-config.json'):
                    verify_package(root)

    def test_existing_output_preserved(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            (root / 'saved').write_text('keep')
            with self.assertRaises(FileExistsError): generate(root, root / 'missing', root)
            self.assertEqual((root / 'saved').read_text(), 'keep')

    def test_invalid_input_never_publishes(self):
        with tempfile.TemporaryDirectory() as raw:
            root = Path(raw)
            for number, (mode, color) in enumerate([('RGB', 'red'), ('RGBA', (255, 0, 0, 255)), ('RGBA', (0, 0, 0, 0))]):
                source = root / f'{number}.png'
                Image.new(mode, (8, 8), color).save(source)
                output = root / f'result-{number}'
                with self.assertRaises(ValueError): generate(root, source, output)
                self.assertFalse((output / 'receipt.json').exists())
                self.assertFalse((output / 'output.glb').exists())

if __name__ == '__main__': unittest.main()
