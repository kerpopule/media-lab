"""Run with a qualified torch/safetensors interpreter via unittest."""
import hashlib
import importlib.util
from pathlib import Path
import tempfile
import unittest
try:
    import torch
except ImportError:
    raise unittest.SkipTest("Requires qualified torch/safetensors interpreter")

spec=importlib.util.spec_from_file_location('triposr_conversion',Path(__file__).parents[1]/'tools/convert-triposr-checkpoint.py')
conversion=importlib.util.module_from_spec(spec);spec.loader.exec_module(conversion)


class ConversionTests(unittest.TestCase):
    def test_roundtrip_and_existing_output_preservation(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'input.ckpt';output=root/'output.safetensors'
            torch.save({'weight':torch.arange(12,dtype=torch.float32).reshape(3,4)},source)
            conversion.SIZE=source.stat().st_size;conversion.SHA256=hashlib.sha256(source.read_bytes()).hexdigest()
            result=conversion.convert(source,output)
            self.assertTrue(result['tensor_roundtrip_verified'])
            before=output.read_bytes()
            with self.assertRaisesRegex(ValueError,'existing files'):
                conversion.convert(source,output)
            self.assertEqual(before,output.read_bytes())

    def test_nonfinite_values_do_not_publish(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);source=root/'input.ckpt';output=root/'output.safetensors'
            torch.save({'weight':torch.tensor([float('nan')])},source)
            conversion.SIZE=source.stat().st_size;conversion.SHA256=hashlib.sha256(source.read_bytes()).hexdigest()
            with self.assertRaisesRegex(ValueError,'nonfinite'):
                conversion.convert(source,output)
            self.assertFalse(output.exists())


if __name__=='__main__':unittest.main()
