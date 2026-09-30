"""Cancelling one image job must never stop another job's ComfyUI prompt."""
import asyncio
import json

import httpx
import pytest

from prosperos_hoard import engine
from prosperos_hoard.jobs import JobCancelled


class _Comfy:
    def __init__(self, running, pending):
        self.url = "http://127.0.0.1:8188"
        self.posts = []
        self.running, self.pending = list(running), list(pending)

        def handler(request):
            if request.method == "GET" and request.url.path == "/queue":
                return httpx.Response(200, json={
                    "queue_running": [[0, pid, {}, {}, []] for pid in self.running],
                    "queue_pending": [[i + 1, pid, {}, {}, []] for i, pid in enumerate(self.pending)]})
            if request.method == "POST":
                self.posts.append((request.url.path, json.loads(request.content or b"{}")))
                return httpx.Response(200)
            raise AssertionError(request.url.path)

        self._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))


def _cancel(comfy, prompt_id):
    return asyncio.run(engine._cancel_comfy_prompt(comfy, prompt_id))


def test_running_prompt_is_interrupted_by_its_own_id():
    comfy = _Comfy(["ours"], ["other"])
    assert _cancel(comfy, "ours") == "interrupted"
    assert comfy.posts == [("/interrupt", {"prompt_id": "ours"})]


def test_pending_prompt_is_dequeued_without_touching_the_running_one():
    comfy = _Comfy(["someone-else"], ["ours"])
    assert _cancel(comfy, "ours") == "dequeued"
    assert comfy.posts == [("/queue", {"delete": ["ours"]})]
    assert not any(path == "/interrupt" for path, _ in comfy.posts)


def test_prompt_that_already_left_the_queue_is_not_interrupted():
    comfy = _Comfy(["someone-else"], [])
    assert _cancel(comfy, "ours") == "not_queued" and comfy.posts == []


def test_cancelled_workflow_uses_the_scoped_cancel(monkeypatch):
    calls = []

    class Comfy:
        url = "http://127.0.0.1:8188"

        async def queue(self, workflow, client_id):
            return "ours"

        async def interrupt(self):  # the global stop must not be used
            pytest.fail("global ComfyUI interrupt used for a single job")

    async def scoped(comfy, prompt_id):
        calls.append(prompt_id)
        return "dequeued"

    class Backend:
        def run_async(self, coro):
            return asyncio.run(coro)

    progress = lambda *a, **k: None
    progress.cancelled = lambda: True
    monkeypatch.setattr(engine, "_comfy", lambda backend: Comfy())
    monkeypatch.setattr(engine, "_cancel_comfy_prompt", scoped)
    with pytest.raises(JobCancelled):
        engine._run_comfy_workflow(Backend(), {"1": {}}, [], progress, 5.0, None)
    assert calls == ["ours"]
