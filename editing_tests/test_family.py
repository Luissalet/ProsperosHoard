"""The family side: media arriving from sibling apps, and the events that tell the hub what happened."""

import re
import threading
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from conftest import FakeLink, ROOT, fake_transcript, make_services, needs_ffmpeg
from prosperos_hoard.editing import agent_tools, family_events
from prosperos_hoard.editing import media as media_store
from prosperos_hoard.editing import projects as store
from prosperos_hoard.editing.errors import LumiereError, NotFound, Refused

pytestmark = needs_ffmpeg


class Quiet(SimpleHTTPRequestHandler):
    def log_message(self, *args):
        pass


@pytest.fixture
def sibling(media_dir):
    """Another app on this machine serving a file over HTTP."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), partial(Quiet, directory=str(media_dir)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    yield f"http://127.0.0.1:{server.server_address[1]}"
    server.shutdown()
    server.server_close()


def events(svc, type_):
    return [d for t, d in svc._emit.events if t == type_]


# ---------------------------------------------------------------- receiving

def test_media_arrives_by_path_and_starts_a_project_shaped_for_it(services, media_dir):
    res = family_events.receive_media(services, path=str(media_dir / "vert.mp4"), create_project=True, source="prospero", transcribe=True)
    assert res["kind"] == "video" and not res["existing"] and res["project"]["action"] == "created"
    assert res["project"]["canvas"].startswith("1080x1920")                      # vertical media: a vertical project
    p = store.doc(services, res["project"]["id"])
    assert [c.media for c in p.main_track().clips] == [res["id"]]
    assert res["transcribe"][0]["kind"] == "transcript"
    got = events(services, "lumiere.media.received")
    assert got == [{"id": res["id"], "name": res["name"], "kind": "video", "source": "prospero", "project": res["project"]["id"], "existing": False}]
    # the same file again is recognised, and a named project with a chosen canvas works too
    again = family_events.receive_media(services, path=str(media_dir / "vert.mp4"), create_project="Mi corto", preset="square")
    assert again["existing"] and again["id"] == res["id"] and again["project"]["name"] == "Mi corto" and again["project"]["canvas"].startswith("1080x1080")


def test_media_extends_an_existing_project_as_one_undo_step(services, media_dir):
    pid = store.create(services, "Mio", preset="hd720")["id"]
    a = family_events.receive_media(services, path=str(media_dir / "talk.mp4"), project=pid, name="Charla recibida", source="scribe")
    b = family_events.receive_media(services, path=str(media_dir / "pic.png"), project=pid)
    assert a["name"] == "Charla recibida" and a["project"]["action"] == "extended"
    clips = store.doc(services, pid).main_track().clips
    assert [c.media for c in clips] == [a["id"], b["id"]]
    hist = store.history(services, pid)["items"]
    assert [h["actor"] for h in hist[:2]] == ["family", "family"] and hist[0]["label"] == "Medio de otra app"
    assert hist[1]["label"] == "Medio de scribe"
    store.undo(services, pid)
    assert len(store.doc(services, pid).main_track().clips) == 1


def test_file_and_local_urls(services, media_dir, sibling):
    by_file = family_events.receive_media(services, url=(media_dir / "clicks.wav").as_uri())
    assert by_file["kind"] == "audio" and by_file["path"] == str((media_dir / "clicks.wav").resolve())
    got = family_events.receive_media(services, url=f"{sibling}/scenes.mp4", name="Escenas del vecino", create_project=True)
    assert got["name"] == "Escenas del vecino" and got["kind"] == "video" and Path(got["path"]).parent == services.config.uploads_dir
    assert media_store.get(services, got["id"])["origin"] == "upload" and got["project"]["action"] == "created"
    with pytest.raises(Refused) as far:
        family_events.receive_media(services, url="http://example.org/clip.mp4")
    assert far.value.code == "not_local"
    with pytest.raises(Refused):
        family_events.receive_media(services, url="file://other-host/etc/clip.mp4")
    with pytest.raises(LumiereError):
        family_events.receive_media(services, url="ftp://127.0.0.1/clip.mp4")
    with pytest.raises(LumiereError) as gone:
        family_events.receive_media(services, url=f"{sibling}/not-there.mp4")
    assert gone.value.code == "fetch_failed"


def test_what_cannot_be_received_is_refused_cleanly(tmp_path, media_dir):
    (tmp_path / "ok").mkdir()
    svc = make_services(tmp_path, file_roots=(tmp_path / "ok",))
    with pytest.raises(Refused) as outside:
        family_events.receive_media(svc, path=str(media_dir / "talk.mp4"))
    assert outside.value.code == "outside_roots"
    with pytest.raises(Refused):
        family_events.receive_media(svc, url=(media_dir / "talk.mp4").as_uri())
    for kw in ({}, {"path": "a", "url": "file:///a"}, {"path": str(media_dir / "talk.mp4"), "project": "prj_x", "create_project": True}):
        with pytest.raises(LumiereError):
            family_events.receive_media(svc, **kw)
    with pytest.raises(NotFound):
        family_events.receive_media(svc, path=str(media_dir / "talk.mp4"), project="prj_missing")
    assert svc.db.one("SELECT COUNT(*) c FROM media")["c"] == 0           # nothing was imported by the failed attempts
    assert events(svc, "lumiere.media.received") == []


def test_the_event_and_the_tool_reach_the_same_code(client, media_dir):
    svc = client.svc
    auth = {"Authorization": f"Bearer {svc.token}"}
    body = {"type": "lumiere.media.import", "source": "hoard-hub", "data": {"path": str(media_dir / "talk.mp4"), "create_project": "Desde el hub"}}
    assert client.post("/api/family/events", json=body).status_code == 401
    assert client.post("/api/family/events", json=body, headers={"Authorization": "Bearer nope"}).status_code == 401
    r = client.post("/api/family/events", json=body, headers=auth)
    assert r.status_code == 200 and r.json()["handled"] and r.json()["result"]["project"]["name"] == "Desde el hub"
    assert events(svc, "lumiere.media.received")[0]["source"] == "hoard-hub"
    other = client.post("/api/family/events", json={"type": "prospero.render.done", "source": "prospero", "data": {}}, headers=auth)
    assert other.status_code == 200 and other.json() == {"handled": False, "type": "prospero.render.done", "accepts": ["lumiere.media.import"]}
    bad = client.post("/api/family/events", json={"type": "lumiere.media.import", "data": {"path": "/no/such/file.mp4"}}, headers=auth)
    assert bad.status_code == 404
    # the tool (what the hub's proxy calls) and the route agree
    via_tool = client.post("/api/agent/call", json={"name": "media_receive", "arguments": {"path": str(media_dir / "vert.mp4"), "project": r.json()["result"]["project"]["id"]},
                                 "reason": "Receive a file like the hub does"},
                           headers=auth)
    assert via_tool.status_code == 200 and via_tool.json()["project"]["action"] == "extended"
    contract = client.get("/api/family/contract").json()
    assert {e["type"] for e in contract["emits"]} >= {"lumiere.render.done", "lumiere.render.failed", "lumiere.media.transcribed"}
    assert contract["accepts"][0]["type"] == "lumiere.media.import" and contract["accepts"][0]["tool"] == "media_receive"
    assert set(client.get("/api/status").json()["family"]["emits"]) == set(family_events.EMITS)


# ---------------------------------------------------------------- sending

@pytest.fixture
def lib(services, media_dir):
    return {"talk": media_store.import_path(services, str(media_dir / "talk.mp4"))["id"]}


def test_a_finished_render_says_where_it_is_and_how_it_checked_out(services, lib):
    pid = store.create(services, "Hecho", preset="hd720", media=[lib["talk"]])["id"]
    job = services.jobs.get(services.start_render(pid, preset="preview", end=1500)["id"])
    assert job["state"] == "done", job["error"]
    (event,) = events(services, "lumiere.render.done")
    assert event["path"] == job["result"]["path"] and event["project"] == pid and event["preset"] == "preview" and event["job"] == job["id"]
    assert event["duration_ms"] == 1500 and (event["width"], event["height"]) == (960, 540) and event["ok"] is True and event["problems"] == []
    assert event["bytes"] > 1000 and "variant" not in event
    assert events(services, "lumiere.render.failed") == []


def test_a_render_that_fails_says_so(services, media_dir, tmp_path):
    mine = tmp_path / "mine.mp4"
    mine.write_bytes((media_dir / "talk.mp4").read_bytes())          # a private copy: the shared test media must stay
    pid = store.create(services, "Roto", preset="hd720", media=[media_store.import_path(services, str(mine))["id"]])["id"]
    mine.rename(tmp_path / "mine.gone")
    job = services.jobs.get(services.start_render(pid, preset="preview")["id"])
    assert job["state"] == "failed"
    (event,) = events(services, "lumiere.render.failed")
    assert event["job"] == job["id"] and event["project"] == pid and event["preset"] == "preview" and "missing" in event["error"]
    assert events(services, "lumiere.render.done") == []
    assert events(services, "lumiere.job.failed") == []             # a render's failure is sent once: the hub reads render.failed as job.failed
    assert event["job_id"] == job["id"] and event["kind"] == "render" and event["title"] == "Roto"


def test_a_lossless_cut_is_announced_too(services, lib):
    pid = store.create(services, "Copia", preset="hd720", media=[lib["talk"]])["id"]
    job = services.jobs.get(services.start_render(pid, mode="copy")["id"])
    assert job["state"] == "done", job["error"]
    (event,) = events(services, "lumiere.render.done")
    assert event["preset"] == "copy" and event["path"] == job["result"]["path"] and event["job"] == job["id"]


def test_transcriptions_announce_their_end(services, lib):
    from prosperos_hoard.editing import analyze

    def words(path, **kw):
        return {"language": "es", "language_p": 1.0, "model": "tiny", "device": "cpu", "segments": [],
                "words": [{"id": "w1", "t0": 0, "t1": 300, "text": "hola", "p": 0.9}]}

    services.transcriber = words
    job = analyze.schedule(services, lib["talk"], ["transcript"])[0]["job"]
    assert services.jobs.get(job)["state"] == "done"
    (event,) = events(services, "lumiere.media.transcribed")
    assert event == {"id": lib["talk"], "name": "talk", "words": 1, "language": "es", "duration_ms": 12000, "model": "tiny", "job": job}

    def broken(path, **kw):
        raise RuntimeError("no GPU memory")

    services.transcriber = broken
    job = analyze.schedule(services, lib["talk"], ["transcript"], force=True)[0]["job"]
    assert services.jobs.get(job)["state"] == "failed"
    (failed,) = events(services, "lumiere.media.transcription_failed")
    assert failed["id"] == lib["talk"] and failed["job"] == job and "no GPU memory" in failed["error"]


def test_translation_announces_itself(tmp_path, media_dir):
    svc = make_services(tmp_path, link=FakeLink(lambda m: "\n".join(f"{l.split('|')[0]}|x" for l in m[-1]["content"].splitlines() if "|" in l)))
    mid = media_store.import_path(svc, str(media_dir / "talk.mp4"), prepare=False)["id"]
    pid = store.create(svc, "T", preset="hd720", media=[mid])["id"]
    fake_transcript(svc, mid, [(100, 400, "Hola"), (450, 800, "qué"), (850, 1300, "tal.")])
    from prosperos_hoard.editing import subtitles

    subtitles.translate(svc, pid, "en")
    assert events(svc, "lumiere.subtitles.translated") == [{"project": pid, "language": "en", "cues": 1, "fallback": 0}]


# ---------------------------------------------------------------- documented

def test_every_event_the_code_sends_is_in_the_contract_and_in_the_instructions():
    sent = set()
    for f in (ROOT / "prosperos_hoard" / "editing").rglob("*.py"):
        if "hoard_link" in f.parts:
            continue
        sent |= set(re.findall(r'(?:emit|_send)\(\s*"(lumiere\.[a-z_.]+)"', f.read_text(encoding="utf-8")))
    assert sent and sent <= set(family_events.EMITS), sent - set(family_events.EMITS)
    assert set(family_events.EMITS) - sent == set(), "documented but never sent"
    for name in (*family_events.EMITS, *family_events.ACCEPTS):
        assert name in agent_tools.AGENT_INSTRUCTIONS, name
    assert "media_receive" in agent_tools.AGENT_INSTRUCTIONS and "formats" in agent_tools.AGENT_INSTRUCTIONS
    assert "subtitles_translate" in agent_tools.AGENT_INSTRUCTIONS
    tools = {t.name: t for t in agent_tools.TOOLS}
    for name in ("subtitles_translate", "media_receive", "render_start", "subtitles_export"):
        assert len(tools[name].description.splitlines()[0]) <= 110 and "Keywords" in tools[name].description
