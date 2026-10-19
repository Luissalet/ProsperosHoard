"""Renders as the hub's canonical job events, and the notification through the hub (setting notify.via)."""

import pytest

from conftest import FakeHub, make_services, needs_ffmpeg
from prosperos_hoard.editing import media as media_store
from prosperos_hoard.editing import projects as store
from prosperos_hoard.editing.errors import LumiereError

pytestmark = needs_ffmpeg


def got(svc, *types):
    return [(t, d) for t, d in svc._emit.events if t in types]


@pytest.fixture
def hub():
    return FakeHub()


@pytest.fixture
def svc(tmp_path, hub):
    s = make_services(tmp_path, port=5198, hub_notify=hub)
    s.job_events.min_interval_s = 0           # every progress step is announced, so the test sees them
    s.start()
    yield s
    s.stop()


@pytest.fixture
def project(svc, media_dir):
    mid = media_store.import_path(svc, str(media_dir / "talk.mp4"))["id"]
    return store.create(svc, "Mi corto", preset="hd720", media=[mid])["id"]


def render(svc, project, **kw):
    return svc.jobs.get(svc.start_render(project, preset="preview", end=1500, **kw)["id"])


def test_a_render_sends_the_whole_lifecycle_in_order(svc, project):
    job = render(svc, project)
    assert job["state"] == "done", job["error"]
    sent = got(svc, "lumiere.job.queued", "lumiere.job.started", "lumiere.job.progress", "lumiere.render.done")
    kinds = [t for t, _ in sent]
    assert kinds[0] == "lumiere.job.queued" and kinds[1] == "lumiere.job.started" and kinds[-1] == "lumiere.render.done"
    assert "lumiere.job.progress" in kinds
    for _, d in sent:
        assert d["job_id"] == job["id"] and d["kind"] == "render" and d["title"] == "Mi corto" and d["url"] == f"http://127.0.0.1:5198/#/p/{project}"
    progress = [d["progress"] for t, d in sent if t == "lumiere.job.progress"]
    assert progress == sorted(progress) and all(0 <= x <= 1 for x in progress)
    done = sent[-1][1]
    assert done["progress"] == 1.0 and done["ref"] == f"hoard://lumiere/render/{job['result']['id']}" and done["path"] == job["result"]["path"]
    assert done["job"] == job["id"] and done["id"] == job["result"]["id"]          # the old fields are all still there


def test_progress_is_throttled(tmp_path, media_dir):
    s = make_services(tmp_path)
    s.start()
    try:
        mid = media_store.import_path(s, str(media_dir / "talk.mp4"))["id"]
        pid = store.create(s, "T", preset="hd720", media=[mid])["id"]
        s.jobs.get(s.start_render(pid, preset="preview", end=1500)["id"])
        assert got(s, "lumiere.job.progress") == []        # the default is one event every five seconds: a short render sends none
    finally:
        s.stop()


def test_each_canvas_of_a_multi_format_render_has_its_own_ref(svc, project):
    job = render(svc, project, formats=["16:9", "9:16"])
    assert job["state"] == "done", job["error"]
    done = [d for _, d in got(svc, "lumiere.render.done")]
    assert len(done) == 2 and len({d["ref"] for d in done}) == 2 and {d["job_id"] for d in done} == {job["id"]}
    assert len(svc.notifier.sent) == 1                         # but the person is told once


def test_a_lossless_cut_is_a_render_too(svc, project):
    job = svc.jobs.get(svc.start_render(project, mode="copy")["id"])
    assert job["state"] == "done", job["error"]
    (_, done), = got(svc, "lumiere.render.done")
    assert done["ref"] == f"hoard://lumiere/render/{job['result']['id']}" and done["kind"] == "render" and done["job_id"] == job["id"]
    assert [t for t, _ in got(svc, "lumiere.job.queued", "lumiere.job.started")] == ["lumiere.job.queued", "lumiere.job.started"]


def test_other_jobs_are_not_announced_as_renders(svc, media_dir):
    media_store.import_path(svc, str(media_dir / "talk.mp4"))      # a proxy job runs
    assert got(svc, "lumiere.job.queued", "lumiere.job.started", "lumiere.job.progress") == []


def test_a_failed_render_sends_one_event_with_the_error(svc, media_dir, tmp_path, hub):
    mine = tmp_path / "mine.mp4"
    mine.write_bytes((media_dir / "talk.mp4").read_bytes())
    pid = store.create(svc, "Roto", preset="hd720", media=[media_store.import_path(svc, str(mine))["id"]])["id"]
    mine.rename(tmp_path / "mine.gone")
    job = render(svc, pid)
    assert job["state"] == "failed"
    (_, failed), = got(svc, "lumiere.render.failed")
    assert failed["job_id"] == job["id"] and failed["kind"] == "render" and failed["title"] == "Roto" and "missing" in failed["error"]
    assert got(svc, "lumiere.job.failed") == [] and got(svc, "lumiere.render.done") == []
    (note,) = hub.notices
    assert note["priority"] == "high" and note["group"] == "job" and note["dedupe_key"] == "lumiere:render:failed"
    assert note["title"] == "Exportación fallida: Roto" and "missing" in note["body"] and note["url"].endswith(f"/#/p/{pid}")


def test_a_canceled_queued_render_says_so(tmp_path, media_dir):
    s = make_services(tmp_path, inline=False)               # a queue nobody works on: the render stays queued
    try:
        mid = media_store.import_path(s, str(media_dir / "talk.mp4"), prepare=False)["id"]
        pid = store.create(s, "Espera", preset="hd720", media=[mid])["id"]
        job = s.start_render(pid, preset="preview")
        assert got(s, "lumiere.job.queued")[0][1]["job_id"] == job["id"]
        s.jobs.cancel(job["id"])
        (_, d), = got(s, "lumiere.job.cancelled")
        assert d["job_id"] == job["id"] and d["kind"] == "render"
        assert s.notifier.sent == []                        # nobody is told about something they canceled
    finally:
        s.stop()


def test_a_finished_render_notifies_through_the_hub(svc, project, hub):
    job = render(svc, project)
    (note,) = hub.notices
    assert note["priority"] == "normal" and note["group"] == "render" and note["dedupe_key"] == f"lumiere:render:{job['id']}"
    assert note["title"] == "Exportación lista: Mi corto" and note["body"].endswith(".mp4") and note["url"] == f"http://127.0.0.1:5198/#/p/{project}"
    assert svc.notifier.sent[-1]["ok"] is True


def test_notify_via_off_says_nothing(svc, project, hub):
    svc.update_settings({"notify.via": "off"})
    assert render(svc, project)["state"] == "done"
    assert hub.notices == []


def test_auto_waits_for_a_hub_that_answers_and_hub_always_tries(svc, project, hub):
    hub.available = False
    assert render(svc, project)["state"] == "done"
    assert hub.notices == []                                # auto: no hub, no notice (Lumiere has no channel of its own)
    svc.update_settings({"notify.via": "hub"})
    assert render(svc, project)["state"] == "done"
    assert len(hub.notices) == 1                            # hub: ask anyway


def test_a_hub_that_fails_never_reaches_the_render(svc, project, hub):
    hub.ok = False
    assert render(svc, project)["state"] == "done"
    assert svc.notifier.sent[-1] == {"title": "Exportación lista: Mi corto", "priority": "normal", "ok": False, "error": "hub unreachable"}

    def boom(*a, **k):
        raise RuntimeError("hub exploded")

    hub.notify = boom
    assert render(svc, project)["state"] == "done"


def test_the_setting_is_validated_and_listed(svc):
    assert svc.get_settings()["notify.via"] == "auto"
    assert svc.update_settings({"notify.via": "hub"})["notify.via"] == "hub"
    with pytest.raises(LumiereError, match="notify.via must be one of auto, hub, off"):
        svc.update_settings({"notify.via": "toast"})


def test_the_setting_over_http(client):
    assert client.get("/api/settings").json()["notify.via"] == "auto"
    r = client.patch("/api/settings", json={"notify.via": "off"})
    assert r.status_code == 200 and r.json()["notify.via"] == "off"
    assert client.patch("/api/settings", json={"notify.via": "x"}).status_code == 400
