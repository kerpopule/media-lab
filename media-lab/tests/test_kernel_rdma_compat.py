import pytest

from media_lab_core.kernel_compat import evaluate_kernel_rdma


def test_1019_blocks_two_spark_roce_qualification():
    result = evaluate_kernel_rdma(
        kernel="7.0.0-1019-nvidia", rdma_devices=["mlx5_0"], mode="multi-node-roce"
    )
    assert result["allowed"] is False
    assert result["reason"] == "kernel-7.0.0-1019-rdma-enomem-advisory"


def test_missing_rdma_device_also_blocks_multi_node_roce():
    result = evaluate_kernel_rdma(
        kernel="6.17.0-1029-nvidia", rdma_devices=[], mode="multi-node-roce"
    )
    assert result["allowed"] is False
    assert result["reason"] == "rdma-device-unavailable"


def test_single_node_h3_is_not_misattributed_to_roce_advisory():
    result = evaluate_kernel_rdma(
        kernel="6.17.0-1029-nvidia", rdma_devices=[], mode="single-node-local"
    )
    assert result["allowed"] is True
    assert result["advisory_relevant"] is False
    assert "separate" in result["note"]


def test_unknown_mode_fails_closed():
    with pytest.raises(ValueError, match="unsupported qualification mode"):
        evaluate_kernel_rdma(kernel="7.0.0-1019-nvidia", rdma_devices=[], mode="guess")
