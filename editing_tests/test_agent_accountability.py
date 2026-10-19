"""Agent sessions: mandatory reasons, the write journal, undoing a whole session and the token profiles, on the real app."""
from __future__ import annotations

import json
import zlib

import pytest

from prosperos_hoard import mcp_server
from conftest import fake_transcript, needs_ffmpeg
from prosperos_hoard.editing import agent_tools, media as media_store, plan as plan_mod, projects, subtitles
from prosperos_hoard.editing.errors import NotFound
from prosperos_hoard.editing.hoard_link import tokens as link_tokens


def agent_call(client, name, arguments=None, *, agent="agent-a", session="s1", reason="Because the person asked for it", token=None, **extra):
    headers = {"Authorization": f"Bearer {token or client.svc.token}", "X-Agent-Id": agent, "X-Agent-Session": session}
    body = {"name": name, "arguments": arguments or {}}
    if reason is not None:
        body["reason"] = reason
    body.update(extra)
    return client.post("/api/agent/call", json=body, headers=headers)


def ok(response):
    assert response.status_code == 200, response.text
    return response.json()


def undo(client, session, *, agent="agent-a", token=None, **body):
    headers = {"Authorization": f"Bearer {token or client.svc.token}", "X-Agent-Id": agent}
    if body.get("confirm"):
        body.setdefault("reason", "Taking the session back")
    return client.post("/api/agent/undo", json={"session": session, **body}, headers=headers)


def journal(client, **params):
    return client.get("/api/agent/journal", params=params, headers={"Authorization": f"Bearer {client.svc.token}"}).json()


def dump(client, project_id):
    return projects.doc(client.svc, project_id).dump()


def texts(client, project_id):
    return sorted(c.text for _, c in projects.doc(client.svc, project_id).all_clips() if c.type == "text")


def add_text(text, start=None):
    """A title placed apart from the others (the start comes from the text), so that two titles never overwrite each other."""
    start = 2000 + 2000 * (zlib.crc32(text.encode()) % 40) if start is None else start
    return {"op": "add_text", "text": text, "start": start, "length": 1500}


@pytest.fixture
def prj(client):
    """A project with one title, made by the person through the services (not by an agent)."""
    made = projects.create(client.svc, "Montaje de prueba", preset="hd720")
    projects.edit(client.svc, made["id"], [add_text("Título original", 0)])
    return made["id"]


# ---------------------------------------------------------------- reasons

def test_a_write_without_a_reason_is_refused_with_a_hint(client, prj):
    r = agent_call(client, "timeline_edit", {"project": prj, "ops": [add_text("Sin motivo")]}, reason=None)
    assert r.status_code == 400 and r.json()["code"] == "reason_required" and r.json()["hint"]
    assert agent_call(client, "timeline_edit", {"project": prj, "ops": [add_text("Sin motivo")]}, reason="no").status_code == 400    # shorter than 3
    assert texts(client, prj) == ["Título original"]                                                                                  # nothing ran
    assert [p["id"] for p in ok(agent_call(client, "project_list", reason=None))["projects"]] == [prj]                               # reads need none
    assert journal(client)["entries"] == []


def test_the_reason_may_come_among_the_arguments_and_never_reaches_the_tool(client, prj):
    ok(agent_call(client, "timeline_edit", {"project": prj, "ops": [add_text("Con motivo")], "reason": "Add the opening title"}, reason=None))
    assert texts(client, prj) == ["Con motivo", "Título original"]
    assert journal(client)["entries"][0]["reason"] == "Add the opening title"


def test_the_web_ui_is_exempt_from_reasons(client, prj):
    r = client.post(f"/api/projects/{prj}/edit", json={"ops": [add_text("Desde la interfaz")]})
    assert r.status_code == 200, r.text
    assert client.post("/api/projects", json={"name": "Otro"}).status_code in (200, 201)
    assert journal(client)["entries"] == []                       # the interface is not an agent: nothing in the agent journal


def test_the_catalogue_tells_agents_about_reasons_and_draft_safe_tools(client):
    body = client.get("/api/agent/tools").json()
    tools = {t["name"]: t for t in body["tools"]}
    assert body["reasons_required"] is True and "reason" in body["instructions"]
    assert "reason" in tools["timeline_edit"]["inputSchema"]["properties"] and "reason" in tools["timeline_edit"]["inputSchema"]["required"]
    assert "reason" not in tools["project_get"]["inputSchema"].get("properties", {})
    for name in ("timeline_edit", "project_create", "media_import", "plan_create", "edit_command", "text_cut", "media_tag", "transcript_fix"):
        assert tools[name]["annotations"]["draftSafeHint"] is True, name
    for name in ("project_delete", "media_delete", "render_start", "plan_apply", "settings", "subtitles_export", "subtitles_translate",
                 "project_export_otio", "job_cancel", "creative_call", "creative_render_title_video"):
        assert "draftSafeHint" not in tools[name]["annotations"], name


def test_every_undo_handler_belongs_to_a_write_tool_and_has_its_hooks_together():
    by_name = {t.name: t for t in agent_tools.TOOLS}
    assert set(agent_tools.agent_undo.HOOKS) <= set(by_name)
    for name, tool in by_name.items():
        if tool.undo is not None:
            assert tool.annotations["readOnlyHint"] is False and tool.track is not None, name
        if tool.capture is not None:
            assert tool.undo is not None, name
    assert agent_tools.agent_undo.DRAFT_SAFE <= set(by_name)
    assert all(not by_name[n].annotations["readOnlyHint"] for n in agent_tools.agent_undo.DRAFT_SAFE)


def test_the_mcp_bridge_sends_the_agent_and_session_from_its_environment(monkeypatch):
    seen = {}

    class FakeResponse:
        status_code = 400

        def json(self):
            return {"error": "This tool changes data, so the call needs a reason.", "code": "reason_required", "hint": "Add a reason."}

    class FakeClient:
        def __init__(self, **kw):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json=None, headers=None):
            seen.update(url=url, headers=headers)
            return FakeResponse()

    import asyncio

    monkeypatch.setattr(mcp_server.httpx, "AsyncClient", FakeClient)
    monkeypatch.setenv("PROSPERO_TOKEN", "t" * 40)
    monkeypatch.setenv("HOARD_AGENT_ID", "agent-a")
    monkeypatch.setenv("HOARD_AGENT_SESSION", "session-1")
    bridge = mcp_server.mcp
    out = asyncio.run(bridge._studio_call_tool("timeline_edit", {}))
    assert seen["headers"]["X-Agent-Id"] == "agent-a" and seen["headers"]["X-Agent-Session"] == "session-1"
    assert json.loads(out[0].text)["code"] == "reason_required" and json.loads(out[0].text)["hint"]       # the refusal is explained to the agent


# ---------------------------------------------------------------- a session edits a project, then takes it all back

def test_undoing_a_session_restores_the_timeline_as_it_was(client, prj):
    original = dump(client, prj)
    ok(agent_call(client, "timeline_edit", {"project": prj, "ops": [add_text("Uno", 2000)], "label": "Primero"}))
    ok(agent_call(client, "timeline_edit", {"project": prj, "ops": [add_text("Dos", 4000), {"op": "marker_add", "t": 500, "label": "M"}]}))
    ok(agent_call(client, "timeline_edit", {"project": prj, "ops": [{"op": "canvas", "width": 1080, "height": 1920}]}))
    assert texts(client, prj) == ["Dos", "Título original", "Uno"] and dump(client, prj) != original
    rev_before = projects.view(client.svc, prj)["rev"]

    entries = journal(client, session="s1")["entries"]
    assert [e["tool"] for e in entries] == ["timeline_edit"] * 3
    assert all(e["agent"] == "agent-a" and e["reason"] and e["ok"] and e["undoable"] and e["objects"] == [f"project:{prj}"] for e in entries)

    dry = ok(undo(client, "s1", dry_run=True))
    assert dry["complete"] is True and len(dry["would_undo"]) == 3 and dump(client, prj) != original         # a dry run changes nothing
    refused = undo(client, "s1")
    assert refused.status_code == 400 and refused.json()["code"] == "confirm_required"
    assert texts(client, prj) == ["Dos", "Título original", "Uno"]

    done = ok(undo(client, "s1", confirm=True, reason="The person did not want these changes"))
    assert len(done["undone"]) == 3 and not done["conflicts"] and not done["not_undoable"] and done["complete"] is True
    assert dump(client, prj) == original
    # the way back is one more step in the history: nothing was lost and the editor's own undo still works
    view = projects.view(client.svc, prj)
    assert view["rev"] > rev_before and view["can_undo"] and view["undo_label"] == "Deshacer cambios del agente"
    projects.undo(client.svc, prj)
    assert texts(client, prj) == ["Título original", "Uno"]                          # each take-back is a step: one step back in the editor lands on the state before it
    kinds = [e["kind"] for e in journal(client, session="s1")["entries"]]
    assert kinds.count("undo") == 3 and kinds.count("write") == 3
    assert ok(undo(client, "s1", confirm=True))["undone"] == []                   # a second go finds nothing left to do


def test_an_agent_that_created_a_project_removes_it_with_the_undo(client):
    made = ok(agent_call(client, "project_create", {"name": "Hecho por el agente", "preset": "reels"}))
    pid = made["id"]
    ok(agent_call(client, "timeline_edit", {"project": pid, "ops": [add_text("Titular")]}))
    ok(agent_call(client, "plan_create", {"project": pid, "instruction": "pon el título «Hola»", "use_model": False}))
    plan = ok(undo(client, "s1", dry_run=True))
    assert plan["complete"] is True and len(plan["would_undo"]) == 3
    done = ok(undo(client, "s1", confirm=True))
    assert len(done["undone"]) == 3 and done["complete"] is True
    with pytest.raises(NotFound):
        projects.doc(client.svc, pid)
    assert client.svc.db.one("SELECT COUNT(*) c FROM plans")["c"] == 0


def test_a_created_project_that_the_person_took_up_is_kept(client):
    pid = ok(agent_call(client, "project_create", {"name": "Lo toca la persona"}))["id"]
    assert client.post(f"/api/projects/{pid}/edit", json={"ops": [add_text("Cosa de la persona")]}).status_code == 200
    done = ok(undo(client, "s1", confirm=True))
    assert done["undone"] == [] and len(done["conflicts"]) == 1 and "edited" in done["conflicts"][0]["message"]
    assert projects.doc(client.svc, pid)


def test_smart_edits_and_previews_are_taken_back_or_left_alone(client, prj):
    original = dump(client, prj)
    ok(agent_call(client, "edit_command", {"project": prj, "command": "captions", "args": {"enabled": True, "style": "pop"}, "preview": True}))
    assert dump(client, prj) == original
    ok(agent_call(client, "timeline_history", {"project": prj, "action": "list"}))
    ok(agent_call(client, "timeline_edit", {"project": prj, "ops": [add_text("Real")]}))
    plan = ok(undo(client, "s1", dry_run=True))
    assert len(plan["would_undo"]) == 3 and plan["complete"]
    ok(undo(client, "s1", confirm=True))
    assert dump(client, prj) == original


def test_history_moves_by_an_agent_are_taken_back_too(client, prj):
    projects.edit(client.svc, prj, [add_text("Segundo")])
    before = dump(client, prj)
    ok(agent_call(client, "timeline_history", {"project": prj, "action": "undo"}))
    assert texts(client, prj) == ["Título original"]
    ok(undo(client, "s1", confirm=True))
    assert dump(client, prj) == before


@needs_ffmpeg
def test_nesting_clips_is_undone_with_the_sequence_it_made(client, media_dir):
    mid = media_store.import_path(client.svc, str(media_dir / "talk.mp4"))["id"]
    prj = projects.create(client.svc, "Con vídeo", media=[mid])["id"]
    clip_ids = [c.id for _, c in projects.doc(client.svc, prj).all_clips() if c.type == "media"]
    before = dump(client, prj)
    made = ok(agent_call(client, "timeline_nest", {"project": prj, "action": "nest", "clips": clip_ids, "name": "Anidado"}))
    seq = made["sequence"]
    assert projects.doc(client.svc, seq) and dump(client, prj) != before
    done = ok(undo(client, "s1", confirm=True))
    assert len(done["undone"]) == 1 and done["complete"] is True
    assert dump(client, prj) == before
    with pytest.raises(NotFound):
        projects.doc(client.svc, seq)


# ---------------------------------------------------------------- sessions do not touch each other

def test_a_later_edit_by_another_session_is_a_conflict_and_is_left_alone(client, prj):
    original = dump(client, prj)
    ok(agent_call(client, "timeline_edit", {"project": prj, "ops": [add_text("De A")]}, session="sa", agent="agent-a"))
    ok(agent_call(client, "timeline_edit", {"project": prj, "ops": [add_text("De B", 3000)]}, session="sb", agent="agent-b"))

    plan = ok(undo(client, "sa", dry_run=True))
    assert plan["would_undo"] == [] and len(plan["conflicts"]) == 1 and plan["complete"] is False
    done = ok(undo(client, "sa", confirm=True))
    assert done["undone"] == [] and len(done["conflicts"]) == 1
    assert texts(client, prj) == ["De A", "De B", "Título original"]            # the other session's work survived
    assert [e["kind"] for e in journal(client, session="sb")["entries"]] == ["write"]

    # once B is undone, A's edit can be taken back as well
    ok(undo(client, "sb", agent="agent-b", confirm=True))
    assert texts(client, prj) == ["De A", "Título original"]
    ok(undo(client, "sa", confirm=True))
    assert dump(client, prj) == original


def test_an_edit_made_in_the_editor_is_a_conflict_too(client, prj):
    ok(agent_call(client, "timeline_edit", {"project": prj, "ops": [add_text("Del agente")]}))
    r = client.post(f"/api/projects/{prj}/edit", json={"ops": [add_text("De la persona", 5000)]})
    assert r.status_code == 200, r.text
    done = ok(undo(client, "s1", confirm=True))
    assert done["undone"] == [] and len(done["conflicts"]) == 1 and done["conflicts"][0]["reason"] == "changed_since"
    assert texts(client, prj) == ["De la persona", "Del agente", "Título original"]


def test_sessions_of_the_same_agent_are_separate(client, prj):
    ok(agent_call(client, "timeline_edit", {"project": prj, "ops": [add_text("Sesión 1")]}, session="one"))
    other = projects.create(client.svc, "Otro")["id"]
    ok(agent_call(client, "timeline_edit", {"project": other, "ops": [add_text("Sesión 2")]}, session="two"))
    ok(undo(client, "two", confirm=True))
    assert texts(client, prj) == ["Sesión 1", "Título original"] and texts(client, other) == []
    assert undo(client, "three", confirm=True).status_code == 404


def test_tools_without_a_way_back_are_reported_not_undone(client, prj):
    ok(agent_call(client, "timeline_edit", {"project": prj, "ops": [add_text("Antes de borrar")]}))
    ok(agent_call(client, "project_delete", {"project": prj, "confirm": True}))
    done = ok(undo(client, "s1", confirm=True))
    assert [n["tool"] for n in done["not_undoable"]] == ["project_delete"]
    assert done["not_undoable"][0]["reason"] == "no_handler" and done["complete"] is False
    assert len(done["conflicts"]) == 1                                 # the earlier edit cannot be restored into a project that is gone


# ---------------------------------------------------------------- plans

def test_applying_a_plan_is_taken_back_and_the_plan_is_a_draft_again(client, prj):
    original = dump(client, prj)
    plan = ok(agent_call(client, "plan_create", {"project": prj, "instruction": "pon el título «Hola»", "use_model": False}))
    assert plan["steps"]
    job = ok(agent_call(client, "plan_apply", {"plan": plan["id"]}))
    assert client.svc.jobs.get(job["job"])["state"] == "done"                       # the test services run jobs inline
    assert "Hola" in texts(client, prj)
    done = ok(undo(client, "s1", confirm=True))
    assert len(done["undone"]) == 2 and done["complete"] is True
    assert dump(client, prj) == original
    assert client.svc.db.one("SELECT COUNT(*) c FROM plans")["c"] == 0              # the plan the session made is gone, not just reopened


def test_a_plan_applied_by_an_agent_conflicts_when_the_timeline_moved_on(client, prj):
    plan = ok(agent_call(client, "plan_create", {"project": prj, "instruction": "pon el título «Hola»", "use_model": False}))
    ok(agent_call(client, "plan_apply", {"plan": plan["id"]}))
    projects.edit(client.svc, prj, [add_text("Después", 6000)])
    done = ok(undo(client, "s1", confirm=True))
    assert len(done["conflicts"]) >= 1 and "Hola" in texts(client, prj)


# ---------------------------------------------------------------- the library, transcripts, subtitles and settings

@needs_ffmpeg
def test_importing_media_is_undone_by_taking_it_out_of_the_library_and_never_touches_the_file(client, media_dir):
    path = media_dir / "talk.mp4"
    made = ok(agent_call(client, "media_import", {"path": str(path)}))
    assert made["existing"] is False and client.svc.db.one("SELECT COUNT(*) c FROM media")["c"] == 1
    again = ok(agent_call(client, "media_import", {"path": str(path)}, session="s2"))
    assert again["existing"] is True                                                    # nothing new: nothing to take back
    assert ok(undo(client, "s2", confirm=True))["undone"] and path.is_file()            # (it reports "unchanged", and changes nothing)
    assert client.svc.db.one("SELECT COUNT(*) c FROM media")["c"] == 1
    done = ok(undo(client, "s1", confirm=True))
    assert len(done["undone"]) == 1 and client.svc.db.one("SELECT COUNT(*) c FROM media")["c"] == 0
    assert path.is_file()


@needs_ffmpeg
def test_media_a_project_uses_stays_in_the_library(client, media_dir):
    mid = ok(agent_call(client, "media_import", {"path": str(media_dir / "talk.mp4")}))["id"]
    pid = projects.create(client.svc, "Usa el medio", media=[mid])["id"]
    done = ok(undo(client, "s1", confirm=True))
    assert done["undone"] == [] and "used in project" in done["conflicts"][0]["message"] and media_store.lookup(client.svc, mid)
    assert projects.doc(client.svc, pid)


@needs_ffmpeg
def test_labels_and_transcript_corrections_come_back(client, media_dir):
    mid = media_store.import_path(client.svc, str(media_dir / "talk.mp4"))["id"]
    fake_transcript(client.svc, mid, [(0, 400, "hola"), (400, 800, "mundo"), (800, 1200, "feo")])
    ok(agent_call(client, "media_tag", {"media": mid, "tags": ["entrevista", "calle"]}))
    ok(agent_call(client, "transcript_fix", {"media": mid, "changes": [{"id": "w3", "text": "bello"}]}))
    ok(agent_call(client, "speakers_edit", {"media": mid, "action": "assign", "speaker": "Ana", "word_ids": ["w1", "w2"]}))
    assert media_store.get(client.svc, mid)["tags"] == ["entrevista", "calle"]
    done = ok(undo(client, "s1", confirm=True))
    assert len(done["undone"]) == 3 and done["complete"] is True
    words = media_store.get_analysis(client.svc, mid, "transcript")["words"]
    assert [w["text"] for w in words] == ["hola", "mundo", "feo"] and not any("speaker" in w for w in words)
    assert media_store.get(client.svc, mid)["tags"] == []
    assert media_store.get_analysis(client.svc, mid, "speakers") is None


@needs_ffmpeg
def test_a_voice_separation_job_cannot_be_taken_back(client, media_dir):
    mid = media_store.import_path(client.svc, str(media_dir / "talk.mp4"))["id"]
    fake_transcript(client.svc, mid, [(0, 400, "hola"), (400, 800, "mundo")])
    ok(agent_call(client, "speakers_edit", {"media": mid, "action": "diarize"}))
    done = ok(undo(client, "s1", confirm=True))
    assert done["undone"] == [] and done["not_undoable"][0]["reason"] == "undo_failed" and "background job" in done["not_undoable"][0]["message"]


def test_a_translation_fix_and_a_setting_are_restored(client, prj):
    cue = {"i": 0, "start": 0, "end": 1000, "source": "hola", "text": "hello"}
    subtitles._put_record(client.svc, prj, "en", {"language": "en", "name": "English", "signature": "sig", "cues": [cue], "updated_ts": 1.0})
    ok(agent_call(client, "subtitles_translate", {"project": prj, "language": "en", "action": "fix", "changes": [{"n": 1, "text": "hi there"}]}))
    ok(agent_call(client, "subtitles_translate", {"project": prj, "language": "en", "action": "show"}))               # a read in disguise: nothing to undo
    ok(agent_call(client, "settings", {"patch": {"hwdec": "off"}}))
    assert subtitles.get_record(client.svc, prj, "en")["cues"][0]["text"] == "hi there" and client.svc.get_settings()["hwdec"] == "off"
    done = ok(undo(client, "s1", confirm=True))
    assert done["complete"] is True and len(done["undone"]) == 3
    assert subtitles.get_record(client.svc, prj, "en")["cues"][0]["text"] == "hello" and client.svc.get_settings()["hwdec"] == "auto"
    ok(agent_call(client, "subtitles_translate", {"project": prj, "language": "en", "action": "delete"}, session="s2"))
    assert subtitles.get_record(client.svc, prj, "en") is None
    ok(undo(client, "s2", confirm=True))
    assert subtitles.get_record(client.svc, prj, "en")["cues"][0]["text"] == "hello"


# ---------------------------------------------------------------- tokens and profiles

def mint(client, agent, profile):
    return link_tokens.mint_agent_token(client.svc.config.data_dir, agent, profile)["token"]


def test_a_read_only_token_cannot_write(client, prj):
    token = mint(client, "reader", "read_only")
    assert ok(agent_call(client, "project_list", token=token, agent="spoofed", reason=None))["projects"]
    assert ok(agent_call(client, "project_get", {"project": prj}, token=token, reason=None))["tracks"]
    r = agent_call(client, "timeline_edit", {"project": prj, "ops": [add_text("No")]}, token=token)
    assert r.status_code == 403 and r.json()["code"] == "profile_forbidden"
    assert texts(client, prj) == ["Título original"]


def test_a_drafts_token_may_edit_drafts_but_not_delete_export_or_publish(client, prj):
    token = mint(client, "drafter", "drafts")
    ok(agent_call(client, "timeline_edit", {"project": prj, "ops": [add_text("Borrador")]}, token=token))
    made = ok(agent_call(client, "project_create", {"name": "Otro borrador"}, token=token))
    for name, args in (("project_delete", {"project": prj, "confirm": True}),
                       ("render_start", {"project": prj}),
                       ("settings", {"patch": {"hwdec": "off"}}),
                       ("subtitles_export", {"project": prj, "format": "srt"}),
                       ("project_export_otio", {"project": prj}),
                       ("job_cancel", {"job": "job_x"}),
                       ("media_delete", {"media": "med_x", "confirm": True})):
        r = agent_call(client, name, args, token=token)
        assert r.status_code == 403 and r.json()["code"] == "profile_forbidden", name
    assert texts(client, prj) == ["Borrador", "Título original"] and projects.doc(client.svc, made["id"])
    # the token fixes the agent: the journal says "drafter" whatever the headers claim
    assert {e["agent"] for e in journal(client)["entries"]} == {"drafter"}
    # a drafts token may not undo (only the "all" profile can)
    assert undo(client, "s1", token=token, confirm=True).status_code == 403


def test_an_all_token_can_undo_its_own_session(client, prj):
    token = mint(client, "writer", "all")
    ok(agent_call(client, "timeline_edit", {"project": prj, "ops": [add_text("Con token")]}, token=token))
    done = ok(undo(client, "s1", token=token, confirm=True))
    assert len(done["undone"]) == 1 and texts(client, prj) == ["Título original"]


# ---------------------------------------------------------------- the journal itself

def test_the_journal_records_who_why_and_masks_secrets(client):
    ok(agent_call(client, "project_create", {"name": "Con secreto sk-abcdefghijklmnopqrstuvwxyz0123456789"},
                  reason="Prepare the project; password=hunter2hunter2", agent="agent-z", session="zz"))
    entry = journal(client, agent="agent-z")["entries"][0]
    assert entry["tool"] == "project_create" and entry["session"] == "zz" and entry["ok"] and entry["ids"]
    text = json.dumps(entry)
    assert "sk-abcdefghij" not in text and "hunter2hunter2" not in text
    assert len(entry["args_summary"]) <= 300
    assert client.get("/api/agent/journal").status_code == 401


def test_failed_writes_are_journaled_but_never_undone(client, prj):
    r = agent_call(client, "timeline_edit", {"project": "prj_ghost", "ops": [add_text("x")]})
    assert r.status_code == 404
    entries = journal(client, session="s1")["entries"]
    assert entries and entries[0]["ok"] is False
    assert ok(undo(client, "s1", confirm=True))["undone"] == []


def test_the_errors_keep_their_codes_and_statuses(client, prj):
    assert agent_call(client, "project_delete", {"project": prj}).json()["code"] == "confirm_required"
    bad = agent_call(client, "timeline_edit", {"project": prj, "ops": [{"op": "nonsense"}]})
    assert bad.status_code == 400 and bad.json()["error"]
    stale = agent_call(client, "timeline_edit", {"project": prj, "ops": [add_text("x")], "base_rev": 99})
    assert stale.status_code == 409 and stale.json()["code"] == "version_conflict"
    assert agent_call(client, "no_such_tool").status_code == 404
    assert client.post("/api/agent/call", json={"name": "project_list"}).status_code == 401


@needs_ffmpeg
def test_a_big_folder_import_is_undone_in_full_even_when_the_answer_was_cut(client, tmp_path, monkeypatch):
    from PIL import Image

    folder = tmp_path / "pics"
    folder.mkdir()
    for n in range(30):
        Image.new("RGB", (16 + n, 16), (n * 8, 40, 90)).save(folder / f"p{n:02d}.png")
    existing = media_store.import_path(client.svc, str(folder / "p00.png"))["id"]                    # one of them was already in the library
    monkeypatch.setattr(agent_tools.cap_result, "__defaults__", (1500,))                              # the answer an agent gets is cut at 1.5 KB here
    made = ok(agent_call(client, "media_import", {"folder": str(folder)}))
    assert "truncated" in made and len(made["imported"]) < 30
    assert client.svc.db.one("SELECT COUNT(*) c FROM media")["c"] == 30
    done = ok(undo(client, "s1", confirm=True))
    assert done["complete"] is True and done["undone"][0]["detail"]["removed_from_library"] == 29
    assert [r["id"] for r in client.svc.db.query("SELECT id FROM media")] == [existing]


@needs_ffmpeg
def test_a_file_received_from_another_app_is_taken_back_with_its_project(client, media_dir):
    made = ok(agent_call(client, "media_receive", {"path": str(media_dir / "vert.mp4"), "create_project": "Desde otra app"}))
    pid = made["project"]["id"]
    assert made["project"]["action"] == "created" and projects.doc(client.svc, pid)
    mid = made["id"]
    base = projects.create(client.svc, "Ya existía", preset="hd720")["id"]
    before = dump(client, base)
    ok(agent_call(client, "media_receive", {"path": str(media_dir / "pic.png"), "project": base}))
    assert dump(client, base) != before
    plan = ok(undo(client, "s1", dry_run=True))
    assert plan["complete"] is True and len(plan["would_undo"]) == 2
    done = ok(undo(client, "s1", confirm=True))
    assert done["complete"] is True and len(done["undone"]) == 2
    assert dump(client, base) == before
    with pytest.raises(NotFound):
        projects.doc(client.svc, pid)
    assert media_store.lookup(client.svc, mid) is None and client.svc.db.one("SELECT COUNT(*) c FROM media")["c"] == 0
