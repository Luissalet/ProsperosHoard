"""Which free-VRAM figure gates a ComfyUI job."""
from __future__ import annotations

from prosperos_hoard.backend import Backend
from prosperos_hoard.hoard_link import gpu as gpu_mod
from prosperos_hoard.hoard_link.gpu import GpuMemory

MB = 1024 * 1024


def _busy_gpus():
    return [GpuMemory(index=0, total_mb=16000, used_mb=13100)]


def _point_at(data_dir, port):
    (data_dir / "backend.json").write_text('{"comfy": {"url": "http://127.0.0.1:%d"}}' % port, encoding="utf-8")


def test_demo_uses_the_fake_comfy_vram_not_the_real_gpus(data_dir, fake_comfy, monkeypatch):
    server, port = fake_comfy
    _point_at(data_dir, port)
    monkeypatch.setattr(gpu_mod, "gpu_free_mb", _busy_gpus)
    backend = Backend(data_dir, demo=True)
    assert backend.vram_free_mb() == server.vram_free_bytes // MB


def test_real_mode_reads_the_card_comfy_runs_on(data_dir, fake_comfy, monkeypatch):
    """A ComfyUI pinned to one card of a multi-GPU machine: its own device
    report wins over the freest card nvidia-smi sees."""
    server, port = fake_comfy
    _point_at(data_dir, port)
    server.devices_override = [{"name": "cuda:0", "type": "cuda", "vram_total": 16_000 * MB, "vram_free": 9_000 * MB}]
    monkeypatch.setattr(gpu_mod, "gpu_free_mb", lambda: [GpuMemory(index=0, total_mb=16000, used_mb=1000)])
    assert Backend(data_dir).vram_free_mb() == 9_000


def test_memory_comfy_itself_reserved_counts_as_available(data_dir, fake_comfy, monkeypatch):
    """Models ComfyUI keeps warm are evicted by ComfyUI when the next job
    needs room, so they must not make that job wait (the job used to sit in
    waiting_gpu behind memory held by the renderer it was waiting for)."""
    server, port = fake_comfy
    _point_at(data_dir, port)
    server.devices_override = [{"name": "cuda:0", "type": "cuda", "vram_total": 16_000 * MB, "vram_free": 6_000 * MB,
                                "torch_vram_total": 7_000 * MB, "torch_vram_free": 1_000 * MB}]
    monkeypatch.setattr(gpu_mod, "gpu_free_mb", _busy_gpus)
    assert Backend(data_dir).vram_free_mb() == 12_000


def test_real_mode_falls_back_to_nvidia_smi_without_comfy_devices(data_dir, fake_comfy, monkeypatch):
    server, port = fake_comfy
    _point_at(data_dir, port)
    server.devices_override = [{"name": "cpu", "type": "cpu", "vram_total": 64_000 * MB, "vram_free": 60_000 * MB}]
    monkeypatch.setattr(gpu_mod, "gpu_free_mb", _busy_gpus)
    assert Backend(data_dir).vram_free_mb() == 2900


def test_a_failed_demo_generation_does_not_leave_a_running_job(store, project):
    import pytest

    from prosperos_hoard.devtools import demo_seed

    class NoComfy:
        def runner(self, *_a):
            raise RuntimeError("no backend here")

    with pytest.raises(RuntimeError):
        demo_seed._run_generation(store, NoComfy(), project["id"], "a portrait", "", 1)
    jobs = store.list_jobs(limit=50)
    items = jobs["items"] if isinstance(jobs, dict) else jobs
    assert items and all(j["state"] == "failed" for j in items)
