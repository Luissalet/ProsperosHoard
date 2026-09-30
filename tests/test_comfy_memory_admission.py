"""Pinned ComfyUI memory policy, synthetic HTTP only; no free/unload calls."""
import asyncio
import json

import httpx
import pytest

from prosperos_hoard.backend import Backend
from prosperos_hoard.engine import check_vram_or_wait
from prosperos_hoard.hoard_link._comfy import ComfyClient
from prosperos_hoard.jobs import WaitingForResources

MB = 1024**2
URL = "http://127.0.0.1:8189"
# Numeric telemetry observed after a completed QA render, before manual free.
BEFORE = {"type": "cuda", "index": 0, "vram_total": 17074421760,
          "vram_free": 6108488620, "torch_vram_total": 33554432, "torch_vram_free": 22553516}


def fixture(data_dir, *, enabled=True, devices=None, queue=None, status=200, selected=URL, mutate=None):
    config = data_dir / "backend.json"
    config.write_text(json.dumps({"comfy": {"url": URL, "manage_memory": enabled}}), encoding="utf-8")
    calls = []
    def handler(request):
        calls.append((request.method, str(request.url)))
        assert request.method == "GET" and request.url.path in ("/system_stats", "/queue")
        if request.url.path == "/system_stats":
            return httpx.Response(200, json={"devices": [dict(BEFORE)] if devices is None else devices})
        if mutate:
            mutate(config)
        return httpx.Response(status, json={"queue_running": [], "queue_pending": []} if queue is None else queue)
    client = ComfyClient(selected, client=httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    backend = Backend(data_dir)
    backend.comfy = lambda: client
    backend.run_async = lambda operation: asyncio.run(operation)
    return backend, calls


def test_optin_real_dynamic_vram_snapshot_allows_idle_render_without_free(data_dir):
    backend, calls = fixture(data_dir)
    assert backend.vram_free_mb() == 5836
    check_vram_or_wait(backend, {"vram_class": "qwen21"})
    assert any(url.endswith("/queue") for _, url in calls)
    assert all(method == "GET" for method, _ in calls)


@pytest.mark.parametrize("enabled", [False, "true", "false", 1, None])
def test_default_and_nonboolean_optin_remain_conservative(data_dir, enabled):
    backend, calls = fixture(data_dir, enabled=enabled)
    with pytest.raises(WaitingForResources):
        check_vram_or_wait(backend, {"vram_class": "qwen21"})
    assert not any(url.endswith("/queue") for _, url in calls)


def test_primary_device_not_freest_and_secondary_total_cannot_admit(data_dir):
    primary = dict(BEFORE, vram_total=11000*MB)
    secondary = dict(BEFORE, index=1, vram_free=24000*MB, vram_total=24576*MB)
    backend, calls = fixture(data_dir, devices=[primary, secondary])
    assert backend.vram_free_mb() == 5836
    with pytest.raises(WaitingForResources):
        check_vram_or_wait(backend, {"vram_class": "qwen21"})
    assert not any(url.endswith("/queue") for _, url in calls)


@pytest.mark.parametrize("queue", [{"queue_running": [[1]], "queue_pending": []},
    {"queue_running": [], "queue_pending": [[1]]}, {}, {"queue_running": False, "queue_pending": []}, []])
def test_busy_or_malformed_queue_stays_conservative(data_dir, queue):
    backend, _ = fixture(data_dir, queue=queue)
    with pytest.raises(WaitingForResources):
        check_vram_or_wait(backend, {"vram_class": "qwen21"})


def test_failed_queue_probe_stays_conservative(data_dir):
    backend, _ = fixture(data_dir, status=503)
    with pytest.raises(WaitingForResources):
        check_vram_or_wait(backend, {"vram_class": "qwen21"})


def test_optin_does_not_follow_another_endpoint(data_dir):
    backend, calls = fixture(data_dir, selected="http://127.0.0.1:8190")
    with pytest.raises(WaitingForResources):
        check_vram_or_wait(backend, {"vram_class": "qwen21"})
    assert not any(url.endswith("/queue") for _, url in calls)


def test_config_identity_change_during_probe_revokes_optin(data_dir):
    def mutate(path):
        path.write_text(json.dumps({"comfy": {"url": URL+"/other", "manage_memory": True}}), encoding="utf-8")
    backend, _ = fixture(data_dir, mutate=mutate)
    with pytest.raises(WaitingForResources):
        check_vram_or_wait(backend, {"vram_class": "qwen21"})


@pytest.mark.parametrize("devices", [[{"type": "cpu", "vram_free": 100000*MB}],
    [{}], [None], "malformed", [dict(BEFORE, vram_total=True)], [dict(BEFORE, vram_total=-1)],
    [dict(BEFORE, vram_free="6108488620")], [dict(BEFORE, vram_free=2**63)],
    [dict(BEFORE, type="garbage")]])
def test_unknown_or_malformed_device_never_enables_managed_override(data_dir, devices):
    backend, _ = fixture(data_dir, devices=devices)
    assert backend.comfy_manages_memory(12000) is False


def test_demo_never_uses_real_managed_override(data_dir):
    backend, calls = fixture(data_dir)
    backend.demo = True
    assert backend.comfy_manages_memory(12000) is False
    assert calls == []


def test_testdouble_free_memory_contract_unchanged():
    class Legacy:
        def vram_estimates_mb(self): return {"qwen21": 12000}
        def vram_free_mb(self): return None
    check_vram_or_wait(Legacy(), {"vram_class": "qwen21"})
