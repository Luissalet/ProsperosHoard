"""The ComfyUI wait is generous per output kind and overridable, so a
render on a shared GPU is not failed while it is still running."""
from prosperos_hoard import engine


def test_default_timeouts_per_kind(monkeypatch):
    monkeypatch.delenv("PROSPERO_COMFY_TIMEOUT_S", raising=False)
    assert engine.comfy_timeout_s("video") == 3600.0
    assert engine.comfy_timeout_s("audio") == 1800.0
    assert engine.comfy_timeout_s("image") == 1200.0
    assert engine.comfy_timeout_s(None) == 1200.0


def test_env_override_wins_for_every_kind(monkeypatch):
    monkeypatch.setenv("PROSPERO_COMFY_TIMEOUT_S", "7200")
    assert engine.comfy_timeout_s("video") == 7200.0
    assert engine.comfy_timeout_s("image") == 7200.0


def test_bad_or_non_positive_override_is_ignored(monkeypatch):
    for raw in ("abc", "0", "-5", "  "):
        monkeypatch.setenv("PROSPERO_COMFY_TIMEOUT_S", raw)
        assert engine.comfy_timeout_s("video") == 3600.0


def test_a_prompt_comfyui_forgot_fails_fast(monkeypatch):
    """After a ComfyUI restart the prompt is in neither the queue nor the
    history: the job fails with a clear error instead of waiting out its deadline."""
    import asyncio

    import pytest

    class FakeComfy:
        url = "http://127.0.0.1:1"

        async def wait(self, prompt_id, timeout_s, poll_interval_s):
            raise TimeoutError

    class FakeBackend:
        def run_async(self, coro):
            return asyncio.run(coro)

    async def empty_queue(_comfy):
        return set(), set()

    monkeypatch.setattr(engine, "_comfy", lambda backend, autostart=None: FakeComfy())
    monkeypatch.setattr(engine, "_queue_tolerant", lambda *a, **k: "p1")
    monkeypatch.setattr(engine, "_comfy_queue_ids", empty_queue)
    monkeypatch.setattr(engine, "COMFY_QUEUE_CHECK_EVERY_S", 0.0)
    monkeypatch.setattr(engine, "COMFY_LOST_GRACE_S", 0.0)
    with pytest.raises(engine.EngineError) as caught:
        engine._run_comfy_workflow(FakeBackend(), {"1": {}}, [], lambda *a, **k: None, 600.0, None)
    assert caught.value.code == "comfy_lost_job"
