"""Demo mode must not be gated by the machine's real, busy GPUs."""
from __future__ import annotations

from prosperos_hoard.backend import Backend
from prosperos_hoard.hoard_link import gpu as gpu_mod
from prosperos_hoard.hoard_link.gpu import GpuMemory


def _busy_gpus():
    return [GpuMemory(index=0, total_mb=16000, used_mb=13100)]


def test_demo_uses_the_fake_comfy_vram_not_the_real_gpus(data_dir, fake_comfy, monkeypatch):
    server, port = fake_comfy
    (data_dir / "backend.json").write_text('{"comfy": {"url": "http://127.0.0.1:%d"}}' % port, encoding="utf-8")
    monkeypatch.setattr(gpu_mod, "gpu_free_mb", _busy_gpus)
    backend = Backend(data_dir, demo=True)
    assert backend.vram_free_mb() == server.vram_free_bytes // (1024 * 1024)


def test_real_mode_still_reads_the_real_gpus(data_dir, fake_comfy, monkeypatch):
    _, port = fake_comfy
    (data_dir / "backend.json").write_text('{"comfy": {"url": "http://127.0.0.1:%d"}}' % port, encoding="utf-8")
    monkeypatch.setattr(gpu_mod, "gpu_free_mb", _busy_gpus)
    assert Backend(data_dir).vram_free_mb() == 2900
