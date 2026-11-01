#!/usr/bin/env python3
"""Runtime bridge for the experimental H3 fused-r1024 checkpoint.

The fused checkpoint intentionally stores every MLP ``fc2`` weight as raw
BF16. Maestro's promoted checkpoints store those projections as quantized
ConvRot tensors whose custom forward accepts the float activation emitted by
``fc1``. A raw ``nn.Linear`` does not: PyTorch requires its input and weight to
share a dtype.

Install this bridge only for the explicitly gated ``fused_r1024`` runtime,
after MMGP has installed its offload forwards. It casts the activation to the
live floating-point weight dtype for the projection and casts the result back
to the residual-stream dtype. Official FL2VA/Ref2VA paths never call it.
"""
from __future__ import annotations

import importlib


def install_fused_fc2_dtype_bridge(transformer, torch_module=None) -> int:
    """Wrap fused H3 MLP fc2 projections and return the wrapped layer count.

    ``torch_module`` is injectable so the contract can be unit-tested without a
    local PyTorch install. Runtime callers omit it and use the container's torch.
    The wrapper reads ``module.weight`` on every call because MMGP may move or
    replace the live tensor while offloading.
    """
    if torch_module is None:
        torch_module = importlib.import_module("torch")

    tensor_type = torch_module.Tensor

    def floating_tensor(value) -> bool:
        return (
            isinstance(value, tensor_type)
            and bool(getattr(getattr(value, "dtype", None), "is_floating_point", False))
        )

    wrapped = 0
    for name, module in transformer.named_modules():
        if not name.endswith(".mlp.fc2"):
            continue
        weight = getattr(module, "weight", None)
        if not floating_tensor(weight):
            continue
        if getattr(module, "_media_lab_fused_fc2_dtype_bridge", False):
            wrapped += 1
            continue

        original_forward = module.forward

        def fused_fc2_forward(input_tensor, *args, _module=module,
                              _original=original_forward, **kwargs):
            live_weight = getattr(_module, "weight", None)
            if (floating_tensor(input_tensor) and floating_tensor(live_weight)
                    and getattr(input_tensor, "dtype") != getattr(live_weight, "dtype")):
                residual_dtype = getattr(input_tensor, "dtype")
                projected = _original(
                    input_tensor.to(dtype=getattr(live_weight, "dtype")), *args, **kwargs)
                if (floating_tensor(projected)
                        and getattr(projected, "dtype") != residual_dtype):
                    projected = projected.to(dtype=residual_dtype)
                return projected
            return _original(input_tensor, *args, **kwargs)

        module.forward = fused_fc2_forward
        module._media_lab_fused_fc2_dtype_bridge = True
        wrapped += 1
    return wrapped
