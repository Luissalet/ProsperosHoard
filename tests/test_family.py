"""Prospero in the Hoard family: the shared agent contract, canonical job events, notices through the hub, the hub's GPU lease and
the four tools other apps call (production_export_lumiere, cast_import_character, production_from_storyboard, voice_tts)."""

from __future__ import annotations

import json
import threading
import time
import wave
from pathlib import Path

import numpy as np
import pytest
from PIL import Image

from prosperos_hoard import exporters, family_tools, gpu_lease, jobevents
from prosperos_hoard import productions as prod
from prosperos_hoard import voice_engines as ve
from prosperos_hoard.family_settings import FamilySettings
from prosperos_hoard.hoard_link import LeaseTimeout, family
from prosperos_hoard.jobs import JobQueue, WaitingForResources
from test_productions import _asset, tiny_spec


class Hub:
    """Stands in for hoard_link.fam_notify."""

    def __init__(self, available=True, ok=True):
        self.available, self.ok, self.notices = available, ok, []

    def hub_available(self, timeout=1.0):
        return self.available

    def notify(self, title, body="", *, priority="normal", url="", group="", dedupe_key="", sphere=None, timeout=5.0):
        self.notices.append({"title": title, "body": body, "priority": priority, "url": url, "group": group, "dedupe_key": dedupe_key})
        return {"ok": self.ok} if self.ok else {"ok": False, "error": "hub unreachable"}


def token(app) -> dict:
    return {"Authorization": "Bearer " + (app.state.store.data_dir / "mcp-token").read_text(encoding="utf-8").strip()}


# ---------------------------------------------------------------- the shared contract

def test_the_catalogue_lists_every_per_tool_route_with_query_and_body_arguments(client):
    c, app, _ = client
    body = c.get("/api/agent/tools").json()
    tools = {t["name"]: t for t in body["tools"]}
    assert body["contract"] == "shared" and body["app"] == "prospero" and "media studio" in body["instructions"]
    routes = {r.path[len("/api/agent/"):] for r in app.router.routes if getattr(r, "path", "").startswith("/api/agent/")
              and "/" not in r.path[len("/api/agent/"):] and r.path not in ("/api/agent/tools", "/api/agent/call")}
    assert routes <= set(tools) and len(tools) > 60                      # GET tools and query-parameter tools are in, not only the POST ones
    cast = tools["studio_cast"]["inputSchema"]
    assert "project" in cast["required"] and {"action", "kind", "id", "name", "fields"} <= set(cast["properties"])
    assert tools["studio_projects"]["inputSchema"]["properties"]["limit"]["type"] == "integer"
    assert tools["studio_status"]["annotations"]["readOnlyHint"] is True and tools["studio_cast"]["annotations"]["readOnlyHint"] is False
    assert tools["studio_status"]["description"].startswith("What the studio can do right now")     # the text of the MCP tool
    for name in ("production_export_lumiere", "cast_import_character", "production_from_storyboard", "voice_tts"):
        assert name in tools and len(tools[name]["description"].splitlines()[0]) <= 110 and "Keywords:" in tools[name]["description"], name
    assert tools["voice_tts"]["annotations"]["readOnlyHint"] is False


def test_a_call_needs_the_token_and_runs_the_same_endpoint_as_the_per_tool_route(client):
    c, app, _ = client
    assert c.post("/api/agent/call", json={"name": "studio_projects"}).status_code == 401
    assert c.post("/api/agent/call", json={"name": "studio_projects"}, headers={"Authorization": "Bearer nope"}).status_code == 401
    h = token(app)
    made = c.post("/api/agent/call", json={"name": "studio_create_project", "arguments": {"name": "Shared", "brief": "b"}}, headers=h)
    assert made.status_code == 200 and made.json()["name"] == "Shared"
    pid = made.json()["id"]
    listed = c.post("/api/agent/call", json={"name": "studio_projects", "arguments": {"query": "Shared", "limit": "5"}}, headers=h)
    assert listed.status_code == 200 and [p["id"] for p in listed.json()["items"]] == [pid]
    assert c.get("/api/agent/studio_projects", params={"query": "Shared"}).json() == c.post(
        "/api/agent/call", json={"tool": "studio_projects", "args": {"query": "Shared"}}, headers=h).json()
    cast = c.post("/api/agent/call", json={"name": "studio_cast", "arguments": {"project": pid, "action": "create", "name": "Ana",
                                                                                 "fields": {"prompt": "a tall woman"}}}, headers=h)
    assert cast.status_code == 200 and cast.json()["name"] == "Ana"
    assert c.post("/api/agent/studio_cast", params={"project": pid}, json={"action": "list"}).status_code == 200   # the per-tool route still works
    count = app.state.store.list_agent_calls(20)
    assert {"studio_create_project", "studio_projects", "studio_cast"} <= {r["tool"] for r in count}


def test_a_call_that_is_wrong_says_what_is_wrong(client):
    c, app, _ = client
    h = token(app)
    unknown = c.post("/api/agent/call", json={"name": "nope"}, headers=h)
    assert unknown.status_code == 404 and "studio_status" in unknown.json()["tools"]
    missing = c.post("/api/agent/call", json={"name": "studio_cast", "arguments": {"action": "list"}}, headers=h)
    assert missing.status_code == 400 and missing.json()["code"] == "missing_argument" and "project" in missing.json()["error"]
    typo = c.post("/api/agent/call", json={"name": "studio_create_project", "arguments": {"name": "x", "bref": "b"}}, headers=h)
    assert typo.status_code == 400 and typo.json()["code"] == "unknown_argument" and "brief" in typo.json()["error"]
    bad = c.post("/api/agent/call", json={"name": "studio_projects", "arguments": {"limit": "many"}}, headers=h)
    assert bad.status_code == 400 and bad.json()["code"] == "invalid_arguments"
    gone = c.post("/api/agent/call", json={"name": "studio_job", "arguments": {"job_id": "nope"}}, headers=h)
    assert gone.status_code == 404 and gone.json()["code"] == "not_found"
    assert c.post("/api/agent/call", json={"name": "studio_status", "arguments": "x"}, headers=h).status_code == 400


def test_the_shared_routes_win_over_the_catch_all_routes(client):
    c, app, _ = client
    assert app.router.routes[0].path in ("/api/agent/tools", "/api/agent/call") and app.router.routes[1].path in ("/api/agent/tools", "/api/agent/call")
    assert c.get("/api/agent/tools").headers["content-type"].startswith("application/json")      # not index.html, not the 404
    assert c.get("/api/agent/nothing_here").status_code == 404 and c.get("/").text.startswith("<html>spa")


def test_health_carries_the_family_block(client):
    c, _, _ = client
    block = c.get("/api/health").json()["hoard_link"]
    assert block["app"] == "prospero" and block["family"] and "hub" in block


# ---------------------------------------------------------------- settings

def test_settings_are_validated_and_kept(client):
    c, app, _ = client
    assert c.get("/api/family/settings").json()["notify.via"] == "auto"
    r = c.put("/api/family/settings", json={"notify.via": "off", "notify.language": "en"})
    assert r.status_code == 200 and r.json()["notify.via"] == "off"
    assert json.loads((app.state.store.data_dir / "family.json").read_text(encoding="utf-8")) == {"notify.via": "off", "notify.language": "en"}
    assert c.put("/api/family/settings", json={"notify.via": "toast"}).status_code == 400
    assert c.put("/api/family/settings", json={"colour": "red"}).status_code == 400
    assert FamilySettings(app.state.store.data_dir).get("notify.via") == "off"
    (app.state.store.data_dir / "family.json").write_text("{broken", encoding="utf-8")
    assert FamilySettings(app.state.store.data_dir).get("notify.via") == "auto"          # a damaged file means the defaults


# ---------------------------------------------------------------- job events

class Rec:
    def __init__(self):
        self.events = []

    def __call__(self, type_, data):
        self.events.append((type_, data))

    def of(self, name):
        return [d for t, d in self.events if t == f"prospero.job.{name}"]


def make_queue(store, rec, *, base="http://127.0.0.1:8815", min_interval_s=0.0, notifier=None, guard=None):
    events = jobevents.JobEvents(store, rec, base_url=lambda: base, notifier=notifier, min_interval_s=min_interval_s)
    queue = JobQueue(store, events=events, gpu_guard=guard)
    return queue, events


def run(queue, type_, lane, params, project_id=None):
    job = queue.enqueue(type_, lane, params, project_id=project_id)
    return job, queue.wait_for(job["id"], 20)


def test_a_song_job_sends_the_canonical_lifecycle(store, project):
    rec = Rec()
    queue, _ = make_queue(store, rec)

    def compose(job, progress):
        progress(0.5, "halfway")
        return {"asset_ids": ["a1", "a2"]}

    queue.register("compose_song", compose)
    queue.start()
    try:
        job, done = run(queue, "compose_song", "gpu", {"tags": "x"}, project["id"])
    finally:
        queue.stop()
    assert done["state"] == "done"
    for name in ("queued", "started", "progress", "done"):
        assert len(rec.of(name)) == 1, name
    order = [t for t, _ in rec.events]
    assert order == ["prospero.job.queued", "prospero.job.started", "prospero.job.progress", "prospero.job.done"]
    for _, d in rec.events:
        assert d["job_id"] == job["id"] and d["kind"] == "song" and d["title"] == "Song · Test Project" and d["project_id"] == project["id"]
        assert d["url"] == f"http://127.0.0.1:8815/#/p/{project['id']}/audio"
    assert rec.of("progress")[0]["progress"] == 0.5 and rec.of("progress")[0]["message"] == "halfway"
    assert rec.of("done")[0]["progress"] == 1.0 and rec.of("done")[0]["asset_ids"] == ["a1", "a2"]


def test_each_kind_has_its_own_name_and_page(store, project):
    rec = Rec()
    queue, _ = make_queue(store, rec)
    tl = store.create_timeline(project["id"], "My cut", "16:9", 24)
    queue.register("animate", lambda j, p: {"asset_id": "c1"})
    queue.register("render_timeline", lambda j, p: {"asset_ids": ["r1"]})
    queue.register("production", lambda j, p: {"slug": "night-walk", "status": "done"})
    queue.register("generate_image", lambda j, p: {"asset_ids": ["i1"]})
    queue.start()
    try:
        run(queue, "animate", "gpu", {}, project["id"])
        run(queue, "render_timeline", "cpu", {"timeline_id": tl["id"], "quality": "final"}, project["id"])
        run(queue, "production", "cpu", {"slug": "night-walk", "name": "Night Walk"}, project["id"])
        run(queue, "generate_image", "gpu", {}, project["id"])
    finally:
        queue.stop()
    done = rec.of("done")
    assert [(d["kind"], d["title"]) for d in done] == [("clip", "Clip · Test Project"), ("render", "My cut · final"), ("production", "Night Walk")]
    assert done[0]["asset_ids"] == ["c1"] and done[1]["url"].endswith(f"/#/p/{project['id']}/timeline")
    assert done[2]["url"] == "http://127.0.0.1:8815/#/productions/night-walk" and done[2]["ref"] == "hoard://prospero/production/night-walk"
    assert len(rec.of("queued")) == 3                      # a still is part of a production's progress, not announced by itself


def test_a_failed_job_says_why_and_a_cancelled_one_says_so(store, project):
    rec = Rec()
    queue, _ = make_queue(store, rec)

    def boom(job, progress):
        raise RuntimeError("the card ran out of memory")

    def slow(job, progress):
        while True:
            progress(0.1)
            time.sleep(0.02)

    queue.register("compose_song", boom)
    queue.register("animate", slow)
    queue.start()
    try:
        _, failed = run(queue, "compose_song", "gpu", {}, project["id"])
        job = queue.enqueue("animate", "gpu", {}, project_id=project["id"])
        while queue.store.get_job(job["id"])["state"] != "running":
            time.sleep(0.02)
        queue.cancel(job["id"])
        cancelled = queue.wait_for(job["id"], 20)
    finally:
        queue.stop()
    assert failed["state"] == "failed" and cancelled["state"] == "cancelled"
    assert rec.of("failed")[0]["error"] == "the card ran out of memory" and rec.of("failed")[0]["kind"] == "song"
    assert rec.of("cancelled")[0]["kind"] == "clip" and rec.of("done") == []


def test_progress_is_throttled_and_a_dead_hub_never_reaches_the_job(store, project):
    rec = Rec()
    queue, events = make_queue(store, rec, min_interval_s=3600)

    def work(job, progress):
        for i in range(1, 10):
            progress(i / 10)
        return {}

    queue.register("compose_song", work)
    queue.start()
    try:
        _, done = run(queue, "compose_song", "gpu", {}, project["id"])
    finally:
        queue.stop()
    assert done["state"] == "done" and rec.of("progress") == []             # one event per hour at most: none in a short job

    def dead(type_, data):
        raise OSError("hub is down")

    events._emit = dead
    queue2 = JobQueue(store, events=events)
    queue2.register("compose_song", lambda j, p: {})
    queue2.start()
    try:
        assert run(queue2, "compose_song", "gpu", {}, project["id"])[1]["state"] == "done"
    finally:
        queue2.stop()


def test_the_events_of_the_real_app_carry_the_gpu(client, monkeypatch):
    c, app, _ = client
    monkeypatch.setenv("PROSPERO_GPU_LEASE", "1")
    rec = Rec()
    queue = app.state.queue
    queue.events._emit = rec
    queue.events.min_interval_s = 0
    queue._gpu_guard = lambda job: gpu_lease.hold(job, lambda: {"ace": 9000}, lease_fn=lambda **kw: FakeLease(kw), on_gpu=queue.events.set_gpu)
    queue.register("compose_song", lambda j, p: (p(0.5), {"asset_ids": []})[1])
    pid = app.state.store.create_project("Gpu")["id"]
    job = queue.enqueue("compose_song", "gpu", {}, project_id=pid)
    assert queue.wait_for(job["id"], 20)["state"] == "done"
    assert rec.of("started")[0].get("gpu") is None and rec.of("progress")[0]["gpu"] == 1 and rec.of("done")[0]["gpu"] == 1


# ---------------------------------------------------------------- the notice when a production needs the person

def a_production(store, *, stage=None, awaiting_take=False, name="Night Walk"):
    state = prod.create_production(store.data_dir, name, tiny_spec())
    state["stage"] = stage
    if awaiting_take:
        state["partial"] = {"song": {"awaiting_take": True}}
    prod.save_state(store.data_dir, state)
    return state["slug"]


def production_job(slug, state, outputs=None, message=None):
    return {"id": "job_1", "type": "production", "state": state, "outputs": outputs, "message": message, "params": {"slug": slug, "name": "Night Walk"}}


@pytest.fixture
def notifier(store):
    hub = Hub()
    return jobevents.Notifier(store, FamilySettings(store.data_dir), hub, background=False), hub


URL = "http://127.0.0.1:8815/#/productions/night-walk"


def test_a_production_that_waits_for_a_take_or_the_animatic_notifies_normal(store, notifier):
    n, hub = notifier
    slug = a_production(store, stage="song", awaiting_take=True)
    n.production(production_job(slug, "done", {"status": "awaiting_review"}), "Night Walk", URL)
    state = prod.load_state(store.data_dir, slug)
    state["partial"] = {}
    state["stage"] = "animatic"
    prod.save_state(store.data_dir, state)
    n.production(production_job(slug, "done", {"status": "awaiting_review"}), "Night Walk", URL)
    first, second = hub.notices
    assert first["title"] == "Night Walk: elige una toma" and second["title"] == "Night Walk: revisa el animático"
    assert first["priority"] == second["priority"] == "normal" and first["url"] == URL and first["group"] == "production"
    assert first["dedupe_key"] != second["dedupe_key"] and slug in first["dedupe_key"]


def test_a_finished_or_failed_production_notifies_and_failure_is_high(store, notifier):
    n, hub = notifier
    slug = a_production(store)
    n.production(production_job(slug, "done", {"status": "done"}), "Night Walk", URL)
    n.production(production_job(slug, "failed", message="clips: the card ran out of memory"), "Night Walk", URL)
    done, failed = hub.notices
    assert done["title"] == "Night Walk: render terminado" and done["priority"] == "normal"
    assert failed["priority"] == "high" and failed["title"] == "Night Walk: ha fallado" and "ran out of memory" in failed["body"]
    assert failed["dedupe_key"] == "prospero:production:failed"           # the key the hub's own failure notice uses
    n.settings.update({"notify.language": "en"})
    n.production(production_job(slug, "done", {"status": "done"}), "Night Walk", URL)
    assert hub.notices[-1]["title"] == "Night Walk: render finished"


def test_notify_via_off_auto_and_hub(store, notifier):
    n, hub = notifier
    slug = a_production(store)
    job = production_job(slug, "done", {"status": "done"})
    n.settings.update({"notify.via": "off"})
    n.production(job, "T", URL)
    assert hub.notices == []
    n.settings.update({"notify.via": "auto"})
    hub.available = False
    n.production(job, "T", URL)
    assert hub.notices == []                                              # no hub, no notice: Prospero has no channel of its own
    n.settings.update({"notify.via": "hub"})
    n.production(job, "T", URL)
    assert len(hub.notices) == 1
    hub.ok = False
    n.production(job, "T", URL)
    assert n.sent[-1]["ok"] is False and n.sent[-1]["error"] == "hub unreachable"

    def boom(*a, **k):
        raise RuntimeError("x")

    hub.notify = boom
    n.production(job, "T", URL)                                           # a hub that explodes never raises


def test_only_productions_notify_and_cancelled_ones_stay_quiet(store, notifier):
    n, hub = notifier
    slug = a_production(store)
    n.production(production_job(slug, "cancelled"), "T", URL)
    n.production(production_job(slug, "done", {"status": "something_else"}), "T", URL)
    assert hub.notices == []
    rec = Rec()
    events = jobevents.JobEvents(store, rec, notifier=n)
    events.finished({"id": "j", "type": "compose_song", "state": "done", "params": {}, "outputs": {}, "project_id": None})
    events.finished({"id": "j2", "type": "production", "state": "done", "params": {"slug": slug, "name": "Night Walk"},
                     "outputs": {"status": "done"}, "project_id": None})
    assert len(hub.notices) == 1 and rec.of("done")[1]["status"] == "done"


def test_a_paused_production_finishes_its_job_with_the_reason(store, notifier):
    n, hub = notifier
    slug = a_production(store, stage="animatic")
    rec = Rec()
    jobevents.JobEvents(store, rec, notifier=n).finished(production_job(slug, "done", {"status": "awaiting_review"}))
    (done,) = rec.of("done")
    assert done["status"] == "awaiting_review" and done["awaiting"] == "animatic" and done["kind"] == "production"
    assert hub.notices[0]["title"].endswith("revisa el animático")


# ---------------------------------------------------------------- the GPU lease

class FakeLease:
    gpu = 1

    def __init__(self, kw):
        self.kw, self.entered, self.exited = kw, False, False

    def __enter__(self):
        self.entered = True
        return self

    def __exit__(self, *exc):
        self.exited = True


def test_the_lease_asks_for_the_job_class_and_is_released(monkeypatch):
    monkeypatch.setenv("PROSPERO_GPU_LEASE", "1")
    got = {}
    leases = []

    def fake(**kw):
        got.update(kw)
        leases.append(FakeLease(kw))
        return leases[-1]

    table = {"wan": 21000, "ace": 9000, "qwen21": 12000, "flux": 16000, "sdxl": 7000}
    job = {"id": "job_9", "type": "animate", "params": {}}
    seen = []
    with gpu_lease.hold(job, lambda: table, lease_fn=fake, on_gpu=lambda jid, gpu: seen.append((jid, gpu))):
        assert leases[0].entered and not leases[0].exited
    assert leases[0].exited and got["vram_mb"] == 21000 and got["owner"] == "prospero" and "animate job_9" in got["purpose"] and seen == [("job_9", 1)]
    for jtype, params, mb in (("compose_song", {}, 9000), ("generate_image", {"engine": "flux"}, 16000), ("generate_image", {}, 7000)):
        with gpu_lease.hold({"id": "j", "type": jtype, "params": params}, lambda: table, lease_fn=fake):
            pass
        assert got["vram_mb"] == mb, jtype
    with gpu_lease.hold({"id": "j", "type": "generate_image", "params": {}}, lambda: table, lease_fn=fake, resolve_image_engine=lambda: "qwen21"):
        pass
    assert got["vram_mb"] == 12000


def test_the_lease_opt_out_and_every_way_it_fails(monkeypatch):
    called = []
    job = {"id": "j", "type": "animate", "params": {}}
    monkeypatch.setenv("PROSPERO_GPU_LEASE", "0")
    with gpu_lease.hold(job, lambda: {}, lease_fn=lambda **kw: called.append(kw)) as held:
        assert held is None and called == []
    monkeypatch.setenv("PROSPERO_GPU_LEASE", "1")

    def refuse(**kw):
        raise RuntimeError("hub refused")

    ran = []
    with gpu_lease.hold(job, lambda: {}, lease_fn=refuse) as held:        # a lease that cannot be had never stops the job
        ran.append(held)
    assert ran == [None]

    class Slow:
        def __enter__(self):
            raise LeaseTimeout("still queued after 120 s", position=2)

        def __exit__(self, *a):
            pass

    with pytest.raises(WaitingForResources, match="GPU lease"):          # a long queue becomes the job's own waiting_gpu retry
        with gpu_lease.hold(job, lambda: {}, lease_fn=lambda **kw: Slow()):
            pass


def test_only_gpu_lane_jobs_run_under_the_lease(store, project, monkeypatch):
    monkeypatch.setenv("PROSPERO_GPU_LEASE", "1")
    log = []

    class Spy(FakeLease):
        def __enter__(self):
            log.append("enter")
            return super().__enter__()

        def __exit__(self, *exc):
            log.append("exit")
            return super().__exit__(*exc)

    queue = JobQueue(store, gpu_guard=lambda job: gpu_lease.hold(job, lambda: {"ace": 1}, lease_fn=lambda **kw: Spy(kw)))
    queue.register("compose_song", lambda j, p: (log.append("work"), {})[1])
    queue.register("render_timeline", lambda j, p: (log.append("cpu-work"), {})[1])
    queue.start()
    try:
        run(queue, "compose_song", "gpu", {}, project["id"])
        run(queue, "render_timeline", "cpu", {"timeline_id": "x"}, project["id"])
    finally:
        queue.stop()
    assert log == ["enter", "work", "exit", "cpu-work"]


def test_the_vram_classes():
    assert gpu_lease.vram_class({"type": "animate", "params": {}}) == "wan"
    assert gpu_lease.vram_class({"type": "edit_image", "params": {}}) == "qwen21"
    assert gpu_lease.vram_class({"type": "something_new", "params": {}}) == "sdxl"

    def down():
        raise OSError("ComfyUI is away")

    assert gpu_lease.vram_class({"type": "generate_image", "params": {}}, down) == "sdxl"


# ---------------------------------------------------------------- production_export_lumiere

def a_cut(store):
    pid = store.create_project("Cut")["id"]
    a, b = _asset(store, pid), _asset(store, pid)
    tl = store.create_timeline(pid, "Cut", "16:9", 24, tracks=[{"type": "visual", "clips": [
        {"asset_id": a, "kind": "image", "start_s": 0, "duration_s": 1}, {"asset_id": b, "kind": "image", "start_s": 1, "duration_s": 1}]}])
    return pid, tl


def test_the_cut_is_exported_and_handed_to_lumiere(client, monkeypatch):
    c, app, _ = client
    store = app.state.store
    pid, tl = a_cut(store)
    spec = tiny_spec()
    state = prod.create_production(store.data_dir, "Night Walk", spec, project_id=pid)
    state["done"] = {"timeline": {"timelines": {"16:9": {"timeline_id": tl["id"]}}}}
    prod.save_state(store.data_dir, state)
    calls = []

    def fake_call(app_id, tool, arguments=None, **kw):
        calls.append((app_id, tool, arguments))
        return {"ok": True, "app": app_id, "tool": tool, "status": 200,
                "result": {"ok": True, "project_id": "prj_1", "url": "http://127.0.0.1:5198/#/p/prj_1", "clips": 2, "skipped": []}}

    monkeypatch.setattr(family, "call", fake_call)
    r = c.post("/api/agent/call", json={"name": "production_export_lumiere", "arguments": {"production": state["slug"], "aspect": "16:9"}},
               headers=token(app))
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["project_id"] == "prj_1" and body["url"].endswith("/#/p/prj_1") and body["clips"] == 2
    (app_id, tool, arguments), = calls
    assert (app_id, tool) == ("lumiere", "project_from_timeline") and arguments["title"] == "Night Walk 16:9"
    xml = Path(arguments["fcpxml_path"])
    assert xml.is_file() and Path(body["files"]["edl"]).is_file() and store.data_dir in xml.parents
    text = xml.read_text(encoding="utf-8")
    assert text.startswith("<?xml") and "<xmeml" in text and text.count("<clipitem") == 2           # what exporters.to_xmeml makes
    again = c.post("/api/agent/production_export_lumiere", json={"production": state["slug"]})
    assert again.status_code == 200 and again.json()["ok"] is True                                  # without aspect: the first cut


def test_a_lumiere_that_is_away_leaves_the_files_and_says_why(client):
    c, app, _ = client
    store = app.state.store
    pid, tl = a_cut(store)
    state = prod.create_production(store.data_dir, "Night Walk", tiny_spec(), project_id=pid)
    state["done"] = {"timeline": {"timelines": {"9:16": {"timeline_id": tl["id"]}}}}
    prod.save_state(store.data_dir, state)
    r = c.post("/api/agent/production_export_lumiere", json={"production": state["slug"]})        # conftest's hub is unreachable
    body = r.json()
    assert r.status_code == 200 and body["ok"] is False and "hub not reachable" in body["error"] and "LUMIERE_FILE_ROOTS" in body["hint"]
    assert Path(body["files"]["xml"]).is_file()
    nocut = prod.create_production(store.data_dir, "Empty", tiny_spec(), project_id=pid)
    r2 = c.post("/api/agent/production_export_lumiere", json={"production": nocut["slug"]})
    assert r2.status_code == 400 and r2.json()["error"] == "no_cut_yet"


# ---------------------------------------------------------------- cast_import_character

def test_a_character_arrives_with_its_images_and_the_same_one_twice_is_the_same(client):
    c, app, allowed = client
    store = app.state.store
    img = allowed / "ana.png"
    Image.new("RGB", (64, 48), (200, 10, 10)).save(img)
    body = {"name": "Ana", "description": "A sailor who never learned to swim.", "look": "a weathered woman in a yellow raincoat",
            "images": [str(img)], "source_ref": "hoard://writer/character/ana"}
    r = c.post("/api/agent/cast_import_character", json=body)
    assert r.status_code == 200, r.text
    first = r.json()
    assert first["ok"] and first["character_id"] and first["existing"] is False and first["images"] == 1
    char = store.get_character(first["character_id"])
    assert char["prompt"].startswith("a weathered woman") and char["bio"].startswith("A sailor") and "hoard://writer/character/ana" in char["notes"]
    assert char["canonical_asset_id"] == char["reference_asset_ids"][0] and store.get_project(first["project_id"])["name"] == "Casting"
    again = c.post("/api/agent/cast_import_character", json=body).json()
    assert again["existing"] is True and again["character_id"] == first["character_id"]
    clash = c.post("/api/agent/cast_import_character", json={"name": "Ana", "source_ref": "hoard://writer/character/other"})
    assert clash.status_code == 400 and clash.json()["error"] == "name_taken"
    elsewhere = c.post("/api/agent/cast_import_character", json={"name": "Bea", "images": [str(allowed.parent / "outside.png")]})
    assert elsewhere.status_code in (400, 403, 404)
    plain = c.post("/api/agent/call", json={"name": "cast_import_character", "arguments": {"name": "Cy", "look": "a boy"}}, headers=token(app))
    assert plain.status_code == 200 and plain.json()["images"] == 0
    assert c.post("/api/agent/cast_import_character", json={"name": "  "}).status_code == 400


# ---------------------------------------------------------------- production_from_storyboard

def test_a_storyboard_becomes_a_draft_that_is_not_queued(client):
    c, app, allowed = client
    store = app.state.store
    pid = store.create_project("Board")["id"]
    still = _asset(store, pid)
    body = {"title": "Storm", "source_ref": "hoard://writer/storyboard/s1", "project": pid,
            "shots": [{"text": "waves hit the pier", "duration_s": 3, "image": still}, {"text": "a lighthouse light turns"},
                      {"text": "the boat comes home", "duration_s": 5.5}]}
    r = c.post("/api/agent/production_from_storyboard", json=body)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["ok"] and out["queued"] is False and out["shots"] == 3 and out["project_id"] == pid and "song" in out["next"]
    state = prod.load_state(store.data_dir, out["production"])
    shots = state["spec"]["shots"]
    assert [s["prompt"] for s in shots] == ["waves hit the pier", "a lighthouse light turns", "the boat comes home"]
    assert [(s["start_s"], s["end_s"]) for s in shots] == [(0.0, 3.0), (3.0, 7.0), (7.0, 12.5)]     # a missing duration is 4 s
    assert shots[0]["reuse_asset_ids"] == [still] and all(s["lead"] is False for s in shots) and state["spec"]["source_ref"] == "hoard://writer/storyboard/s1"
    assert state["status"] == "queued" and state.get("job_id") is None and store.list_jobs("active")["items"] == []
    with_song = c.post("/api/agent/call", json={"name": "production_from_storyboard", "arguments": {
        "title": "Quiet", "shots": [{"text": "one"}], "song_asset_id": _asset(store, pid, kind="audio")}}, headers=token(app))
    assert with_song.status_code == 200 and "continue" in with_song.json()["next"] and "song" not in with_song.json()["next"].split("continue")[0]
    assert prod.load_state(store.data_dir, with_song.json()["production"])["spec"]["song"]["asset_id"]


def test_a_storyboard_that_is_wrong_says_so(client):
    c, _, _ = client
    assert c.post("/api/agent/production_from_storyboard", json={"title": "x", "shots": []}).json()["error"] == "shots_required"
    assert c.post("/api/agent/production_from_storyboard", json={"title": "x", "shots": [{"text": " "}]}).json()["error"] == "bad_shot"
    assert c.post("/api/agent/production_from_storyboard", json={"title": "x", "shots": [{"text": "a", "duration_s": 9999}]}).json()["error"] == "bad_shot"
    assert c.post("/api/agent/production_from_storyboard", json={"title": " ", "shots": [{"text": "a"}]}).json()["error"] == "title_required"
    spec = family_tools.storyboard_spec([{"text": "a"}, {"text": "b"}], title="T", lead={"name": "L", "look": "l"}, source_ref="", reuse={})
    assert [("start_s" in s) for s in spec["shots"]] == [False, False]                             # no durations: no spans


# ---------------------------------------------------------------- voice_tts

class FakeTTS(ve.TTSEngine):
    id = "fake-tts"
    label = "Fake"
    capabilities = ve.EngineCapabilities(languages=["en"], cloning=False)
    spoken: list = []
    speeds: list = []

    def is_installed(self):
        return True

    def synthesize(self, text, voice_ref=None, speed=None, pitch=None, style=None, sample_path=None, language=None):
        FakeTTS.spoken.append((text, voice_ref, language))
        FakeTTS.speeds.append(speed)
        return ve.wav_bytes_mono16((np.sin(np.linspace(0, 10, max(1, len(text)) * 100)) * 0.2).astype("float32"), 16000)


def test_voice_tts_returns_the_path_of_a_wav(client, monkeypatch):
    c, app, _ = client
    monkeypatch.setattr("prosperos_hoard.api.ve.default_tts_engines", lambda **kw: [FakeTTS()])
    FakeTTS.spoken.clear()
    r = c.post("/api/agent/voice_tts", json={"text": "Hola mundo", "lang": "es"})
    assert r.status_code == 200, r.text
    out = r.json()
    path = Path(out["path"])
    assert out["ok"] and out["engine_id"] == "fake-tts" and path.is_file() and app.state.store.data_dir in path.parents
    with wave.open(str(path)) as w:
        assert w.getframerate() == 16000 and w.getnframes() > 0
    assert FakeTTS.spoken[-1] == ("Hola mundo", None, "es")
    named = c.post("/api/agent/call", json={"name": "voice_tts", "arguments": {"text": "x", "voice": "en_US-amy-medium"}}, headers=token(app))
    assert named.status_code == 200 and FakeTTS.spoken[-1][1] == "en_US-amy-medium"
    assert c.post("/api/agent/voice_tts", json={"text": " "}).json()["error"] == "empty_text"
    monkeypatch.setattr("prosperos_hoard.api.ve.default_tts_engines", lambda **kw: [])
    assert c.post("/api/agent/voice_tts", json={"text": "x"}).json()["error"] == "tts_not_installed"


class OtherTTS(FakeTTS):
    id = "other-tts"

    def is_installed(self):
        return False


def test_voice_tts_takes_an_engine_and_a_speed(client, monkeypatch):
    c, app, _ = client
    monkeypatch.setattr("prosperos_hoard.api.ve.default_tts_engines", lambda **kw: [FakeTTS(), OtherTTS()])
    FakeTTS.speeds.clear()
    ok = c.post("/api/agent/voice_tts", json={"text": "hola", "engine": "fake-tts", "speed": 1.5})
    assert ok.status_code == 200 and ok.json()["engine_id"] == "fake-tts" and FakeTTS.speeds[-1] == 1.5
    assert c.post("/api/agent/voice_tts", json={"text": "hola"}).status_code == 200 and FakeTTS.speeds[-1] is None
    for engine_name in ("nope-tts", "other-tts"):                           # unknown, or known but not installed
        bad = c.post("/api/agent/voice_tts", json={"text": "hola", "engine": engine_name})
        assert bad.status_code == 400 and bad.json()["error"] == "unknown_engine", bad.text
    for speed in (0.1, 3, "fast"):
        bad = c.post("/api/agent/voice_tts", json={"text": "hola", "speed": speed})
        assert bad.status_code in (400, 422), bad.text
    assert c.post("/api/agent/voice_tts", json={"text": "hola", "speed": 2.5}).json()["error"] == "bad_speed"
    voice = app.state.store.create_studio_voice("Own", "fake-tts", voice_ref="own-1")      # a library voice's own engine wins
    mixed = c.post("/api/agent/voice_tts", json={"text": "hola", "voice": voice["id"], "engine": "other-tts"})
    assert mixed.status_code == 200 and mixed.json()["engine_id"] == "fake-tts"


def test_voice_tts_uses_a_saved_voice_by_id_or_name(client, monkeypatch):
    c, app, _ = client
    store = app.state.store
    monkeypatch.setattr("prosperos_hoard.api.ve.default_tts_engines", lambda **kw: [FakeTTS()])
    voice = store.create_studio_voice("Narrator", "fake-tts", voice_ref="narr-1")
    FakeTTS.spoken.clear()
    for key in (voice["id"], "narrator"):
        r = c.post("/api/agent/voice_tts", json={"text": "hi", "voice": key})
        assert r.status_code == 200 and FakeTTS.spoken[-1][1] == "narr-1", r.text


def test_a_notice_the_hub_holds_for_quiet_hours_counts_as_delivered(store, notifier):
    n, hub = notifier
    slug = a_production(store)
    hub.notify = lambda *a, **k: {"ok": True, "held": "quiet"}
    n.production(production_job(slug, "done", {"status": "done"}), "Night Walk", URL)
    assert n.sent[-1]["ok"] is True and n.sent[-1]["error"] == ""
    assert n.router.via_status()["setting"] == "auto"
