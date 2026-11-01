#!/usr/bin/env python3
"""Dependency-light regression tests for the fused-r1024 FC2 dtype bridge."""
import unittest
from pathlib import Path

from runner.h3_fused_dtype import install_fused_fc2_dtype_bridge

ROOT = Path(__file__).resolve().parents[1]


class FakeDType:
    def __init__(self, name, is_floating_point):
        self.name = name
        self.is_floating_point = is_floating_point

    def __repr__(self):
        return self.name


FLOAT32 = FakeDType("float32", True)
BFLOAT16 = FakeDType("bfloat16", True)
INT8 = FakeDType("int8", False)


class FakeTensor:
    def __init__(self, dtype):
        self.dtype = dtype

    def to(self, *, dtype):
        return FakeTensor(dtype)


class FakeTorch:
    Tensor = FakeTensor


class FakeLinear:
    def __init__(self, weight_dtype):
        self.weight = FakeTensor(weight_dtype)
        self.received = []

    def forward(self, input_tensor, *args, **kwargs):
        self.received.append(input_tensor.dtype)
        return FakeTensor(self.weight.dtype)


class FakeTransformer:
    def __init__(self, fc2_dtypes):
        self.fc2 = [FakeLinear(dtype) for dtype in fc2_dtypes]
        self.other = FakeLinear(BFLOAT16)

    def named_modules(self):
        for index, module in enumerate(self.fc2):
            yield f"blocks.{index}.mlp.fc2", module
        yield "blocks.0.attn.out_proj", self.other


class FusedFC2DTypeBridgeTest(unittest.TestCase):
    def test_wraps_exact_fused_contract_and_preserves_residual_dtype(self):
        transformer = FakeTransformer([BFLOAT16] * 52)
        count = install_fused_fc2_dtype_bridge(transformer, FakeTorch)
        self.assertEqual(count, 52)

        output = transformer.fc2[0].forward(FakeTensor(FLOAT32))
        self.assertEqual(transformer.fc2[0].received, [BFLOAT16])
        self.assertIs(output.dtype, FLOAT32)

    def test_is_idempotent(self):
        transformer = FakeTransformer([BFLOAT16] * 52)
        self.assertEqual(install_fused_fc2_dtype_bridge(transformer, FakeTorch), 52)
        first_forward = transformer.fc2[0].forward
        self.assertEqual(install_fused_fc2_dtype_bridge(transformer, FakeTorch), 52)
        self.assertIs(transformer.fc2[0].forward, first_forward)

    def test_does_not_wrap_promoted_quantized_fc2(self):
        transformer = FakeTransformer([INT8] * 52)
        original = transformer.fc2[0].forward
        self.assertEqual(install_fused_fc2_dtype_bridge(transformer, FakeTorch), 0)
        self.assertIs(transformer.fc2[0].forward.__func__, original.__func__)

    def test_runtime_mount_and_fused_only_gate_are_explicit(self):
        start = (ROOT / "runner/start_h3_engine.sh").read_text()
        engine = (ROOT / "runner/engine_server.py").read_text()
        self.assertIn("h3_fused_dtype.py,dst=/work/h3_fused_dtype.py,readonly", start)
        self.assertIn("if H3_FUSED_COMBINED:", engine)
        self.assertIn("install_fused_fc2_dtype_bridge(_h3_transformer)", engine)
        self.assertIn("if _fused_fc2_count != 52:", engine)
        self.assertLess(engine.index("profile = offload.profile("),
                        engine.index("install_fused_fc2_dtype_bridge(_h3_transformer)"))


if __name__ == "__main__":
    unittest.main()
