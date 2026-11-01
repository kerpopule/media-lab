from media_lab_core.durable_gpu_protocol import DurableGpuProtocol
from media_lab_core.gpu_lease_runtime import (
    authorize_environment,
    authorize_values,
    delegation_env,
    delegation_headers,
)


def test_exact_http_and_process_delegation_are_fenced(tmp_path):
    protocol = DurableGpuProtocol(
        tmp_path / "gpu.sqlite3",
        tmp_path / "gpu.lock",
        boot_id=lambda: "boot-a",
        available_gib=lambda: 120.0,
        pid_alive=lambda _pid: True,
    )
    protocol.qualify("h3", "fl2va", peak_gib=10, reserve_gib=2, evidence="fixture")
    lease = protocol.acquire(job_id="job-1", engine="h3", task="fl2va", owner="controller")
    assert not authorize_values(
        protocol, delegation_headers(lease), engine="h3", task="fl2va"
    )
    for phase in ("unload", "reclaim", "load", "render"):
        protocol.advance(lease, phase)
    assert authorize_values(protocol, delegation_headers(lease), engine="h3", task="fl2va")
    assert not authorize_values(protocol, {}, engine="h3", task="fl2va")
    assert not authorize_values(
        protocol, delegation_headers(lease), engine="h3", task="ref2va"
    )
    environment = delegation_env(lease)
    assert authorize_environment(
        protocol, environ=environment, engine="h3", task="fl2va"
    )
    environment["MEDIA_LAB_GPU_FENCE"] = str(lease.fence - 1)
    assert not authorize_environment(
        protocol, environ=environment, engine="h3", task="fl2va"
    )
