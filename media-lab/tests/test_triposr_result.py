import hashlib, json, os, struct, tempfile, unittest
from pathlib import Path
from media_lab_core.triposr_compatibility import expected_runtime_receipt
from media_lab_core.triposr_cpu import MODEL_SHA, SOURCE_RECEIPT_SHA, VARIANT
from media_lab_core.triposr_result import verify_triposr_result

INPUT = 'a' * 64

def result(root):
    meta = {'asset': {'version': '2.0'}, 'buffers': [{'byteLength': 12}], 'meshes': [{'primitives': []}]}
    raw = json.dumps(meta).encode(); raw += b' ' * (-len(raw) % 4)
    data = struct.pack('<4sII', b'glTF', 2, 40 + len(raw)) + struct.pack('<II', len(raw), 0x4e4f534a) + raw + struct.pack('<II', 12, 0x004e4942) + bytes(12)
    receipt = dict(variant=VARIANT, device='cpu', dtype='float32', model_sha256=MODEL_SHA,
                   source_receipt_sha256=SOURCE_RECEIPT_SHA, input_sha256=INPUT,
                   output_sha256=hashlib.sha256(data).hexdigest(), bytes=len(data),
                   vertices=3, triangles=1, seconds=1.0, seed=7, resolution=128,
                   creative_status='draft', install_qualified=False, runtime=expected_runtime_receipt())
    (root / 'output.glb').write_bytes(data)
    (root / 'receipt.json').write_text(json.dumps(receipt))
    return receipt, data

class ResultTests(unittest.TestCase):
    def test_exact_bytes(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d); expected=result(root)
            self.assertEqual(verify_triposr_result(root, INPUT), expected)

    def test_changed_receipts(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d)
            for key,value in [('input_sha256','b'*64),('variant','other'),('model_sha256','b'*64),('bytes',1),('seed',True),('vertices',0),('triangles',2000001),('seconds',float('nan')),('install_qualified',True)]:
                with self.subTest(key=key):
                    receipt,_=result(root);receipt[key]=value
                    (root/'receipt.json').write_text(json.dumps(receipt))
                    with self.assertRaises(ValueError): verify_triposr_result(root,INPUT)

    def test_unqualified_runtime_receipt_cannot_recover(self):
        with tempfile.TemporaryDirectory() as d:
            root = Path(d)
            variants = [None, {}, {**expected_runtime_receipt(), 'machine': 'x86_64'},
                        {**expected_runtime_receipt(), 'package_versions_sha256': '0' * 64},
                        {**expected_runtime_receipt(), 'python': [3, 12, 4]}]
            for runtime in variants:
                with self.subTest(runtime=runtime):
                    receipt, _ = result(root)
                    receipt['runtime'] = runtime
                    (root / 'receipt.json').write_text(json.dumps(receipt))
                    with self.assertRaisesRegex(ValueError, 'runtime provenance'):
                        verify_triposr_result(root, INPUT)

    def test_corrupt_or_incomplete(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);result(root)
            (root/'output.glb').write_bytes(b'corrupt')
            with self.assertRaises(ValueError): verify_triposr_result(root,INPUT)
            (root/'receipt.json').unlink()
            with self.assertRaises(OSError): verify_triposr_result(root,INPUT)

    def test_nonregular_files(self):
        for kind in ('symlink','fifo'):
            with self.subTest(kind=kind), tempfile.TemporaryDirectory() as d:
                root=Path(d);result(root);path=root/'output.glb';path.unlink()
                if kind=='fifo': os.mkfifo(path)
                else: path.symlink_to(root/'receipt.json')
                with self.assertRaises((ValueError,OSError)): verify_triposr_result(root,INPUT)


def test_result_cannot_be_recovered_under_another_runtime_profile(tmp_path):
    import pytest
    receipt,data=result(tmp_path)
    with pytest.raises(ValueError,match='runtime provenance'):
        verify_triposr_result(tmp_path,INPUT,runtime_profile='without-vision-v1')
    receipt['runtime']=expected_runtime_receipt(profile='without-vision-v1')
    (tmp_path/'receipt.json').write_text(json.dumps(receipt))
    assert verify_triposr_result(tmp_path,INPUT,runtime_profile='without-vision-v1')[1]==data
    with pytest.raises(ValueError,match='runtime provenance'):
        verify_triposr_result(tmp_path,INPUT)
