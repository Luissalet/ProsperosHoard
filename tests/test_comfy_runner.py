"""How a job talks to ComfyUI: pinned to one event loop across a backend
reload, cancelling only its own prompt, surviving blips, noticing a
restarted server, and not refetching /object_info for every job."""
import threading
import time

import httpx
import pytest

from prosperos_hoard import backend as backend_mod
from prosperos_hoard import comfy_driver, engine
from prosperos_hoard.hoard_link._comfy import ComfyClient
from prosperos_hoard.jobs import JobCancelled


class _Progress:
    def __init__(self):
        self.cancel = False

    def __call__(self, *_a, **_k):
        return None

    def cancelled(self):
        return self.cancel


def _workflow():
    wf, spec = comfy_driver.load_template("sdxl_txt2img")
    return comfy_driver.apply_params(wf, spec, {"seed": 5, "width": 256, "height": 256})


def _sync_threads():
    return [t for t in threading.enumerate() if t.name == "hoard-link-sync" and t.is_alive()]


def _in_thread(fn):
    box = {}

    def run():
        try:
            box["result"] = fn()
        except BaseException as exc:  # noqa: BLE001 - handed back to the test
            box["error"] = exc

    t = threading.Thread(target=run, daemon=True)
    t.start()
    return t, box


def test_reload_mid_job_keeps_the_job_on_its_loop_and_closes_the_old_link(backend_with_comfy, fake_comfy):
    server, _ = fake_comfy
    server.history_delay_s = 2.0
    backend = backend_with_comfy
    backend.status()  # the main link's loop is running
    old_link = backend.link
    t, box = _in_thread(lambda: engine._run_comfy_workflow(backend, _workflow(), [], _Progress(), 30.0, None))
    time.sleep(0.6)
    backend.reload()  # what saving Settings does
    assert backend.link is not old_link
    assert old_link.sync._thread is not None  # still in use by the job
    t.join(20)
    assert "error" not in box, box.get("error")
    assert box["result"], "the job's outputs"
    # the replaced link was closed once the job let go of it
    assert old_link.sync._thread is None
    # and a reload with nothing running closes the old link straight away
    backend.status()
    before = len(_sync_threads())
    second = backend.link
    backend.reload()
    assert second.sync._thread is None
    assert len(_sync_threads()) < before


def test_cancel_takes_only_our_prompt_off_comfy(backend_with_comfy, fake_comfy):
    server, _ = fake_comfy
    server.history_delay_s = 30.0
    progress = _Progress()
    t, box = _in_thread(lambda: engine._run_comfy_workflow(backend_with_comfy, _workflow(), [], progress, 60.0, None))
    time.sleep(0.8)
    progress.cancel = True
    t.join(15)
    assert isinstance(box.get("error"), JobCancelled)
    assert len(server.deleted) == 1
    pid = server.deleted[0]
    # interrupted by name, and only because it was the running prompt
    assert server.interrupts == [pid]


def test_cancel_of_a_pending_prompt_never_interrupts(backend_with_comfy, fake_comfy):
    server, _ = fake_comfy
    with backend_with_comfy.runner() as r:
        done = r.run(backend_mod.cancel_prompt(r.comfy(), "not-running"))
    assert done["deleted"] and not done["interrupted"]
    assert server.deleted == ["not-running"] and server.interrupts == []


def test_timeout_dequeues_the_prompt(backend_with_comfy, fake_comfy):
    server, _ = fake_comfy
    server.history_delay_s = 30.0
    with pytest.raises(engine.EngineError, match="within 1 s"):
        engine._run_comfy_workflow(backend_with_comfy, _workflow(), [], _Progress(), 1.0, None)
    assert len(server.deleted) == 1


def test_a_restarted_comfy_fails_the_job_quickly(backend_with_comfy, fake_comfy, monkeypatch):
    server, _ = fake_comfy
    server.history_delay_s = 60.0
    monkeypatch.setattr(engine, "COMFY_QUEUE_CHECK_EVERY_S", 0.2)
    monkeypatch.setattr(engine, "COMFY_LOST_GRACE_S", 0.5)
    t, box = _in_thread(lambda: engine._run_comfy_workflow(backend_with_comfy, _workflow(), [], _Progress(), 120.0, None))
    time.sleep(1.2)  # still in the queue: no false alarm
    assert t.is_alive()
    server.forget_everything()
    t.join(15)
    err = box.get("error")
    assert isinstance(err, engine.EngineError) and "no longer has job" in str(err)


def test_transient_transport_errors_are_retried(backend_with_comfy, fake_comfy, monkeypatch):
    real_wait = ComfyClient.wait
    fails = {"n": 0}

    async def flaky_wait(self, prompt_id, **kw):
        if fails["n"] < 2:
            fails["n"] += 1
            raise httpx.ConnectError("connection refused")
        return await real_wait(self, prompt_id, **kw)

    monkeypatch.setattr(ComfyClient, "wait", flaky_wait)
    out = engine._run_comfy_workflow(backend_with_comfy, _workflow(), [], _Progress(), 30.0, None)
    assert out and fails["n"] == 2


def test_a_comfy_that_stays_gone_fails_after_the_grace(backend_with_comfy, fake_comfy, monkeypatch):
    async def gone(self, prompt_id, **kw):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr(ComfyClient, "wait", gone)
    monkeypatch.setattr(engine, "COMFY_TRANSPORT_GRACE_S", 0.5)
    with pytest.raises(engine.EngineError, match="lost contact"):
        engine._run_comfy_workflow(backend_with_comfy, _workflow(), [], _Progress(), 30.0, None)


def test_object_info_is_cached_and_falls_back_on_error(backend_with_comfy, fake_comfy, monkeypatch):
    server, _ = fake_comfy
    calls = {"n": 0}
    real = server._object_info

    def counted(node=None):
        calls["n"] += 1
        return real(node)

    monkeypatch.setattr(server, "_object_info", counted)
    first = engine._object_info(backend_with_comfy)
    second = engine._object_info(backend_with_comfy)
    assert first is second and calls["n"] == 1
    monkeypatch.setattr(backend_mod, "OBJECT_INFO_TTL_S", 0.0)

    async def broken(self, node=None):
        raise httpx.ReadTimeout("slow")

    monkeypatch.setattr(ComfyClient, "object_info", broken)
    assert engine._object_info(backend_with_comfy) is first  # stale copy beats a failed job


def test_template_readiness_names_what_is_missing():
    import copy

    from prosperos_hoard.devtools.fake_comfy import real_object_info

    info = copy.deepcopy(real_object_info())
    ready = comfy_driver.template_readiness(info)
    assert ready["wan22_ti2v"] == "ready" and ready["sdxl_txt2img"] == "ready"
    unet = info["UNETLoader"]["input"]["required"]["unet_name"]
    unet[0] = [f for f in unet[0] if "kontext" not in f]
    del info["TextEncodeAceStepAudio1.5"]
    out = comfy_driver.template_readiness(info)
    assert out["flux_kontext_edit"] == {"missing": ["flux1-dev-kontext_fp8_scaled.safetensors"]}
    assert out["ace15_song"] == {"missing": ["TextEncodeAceStepAudio1.5"]}
    assert out["wan22_ti2v"] == "ready"
    assert comfy_driver.template_readiness({}) == {}


def test_status_reports_template_readiness(backend_with_comfy):
    templates = backend_with_comfy.status()["comfy"]["templates"]
    assert templates["sdxl_txt2img"] == "ready" and "wan22_ti2v" in templates
