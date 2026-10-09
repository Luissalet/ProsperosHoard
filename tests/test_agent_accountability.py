"""Agent sessions on Prospero: mandatory reasons, the write journal, undoing a whole session and the token profiles, on the real app.

Agents reach Prospero through `POST /api/agent/call` (the shared contract, the bearer token in `data/mcp-token`). The web
interface calls the operations directly, so it needs no reason and leaves no line in the agent journal."""

from __future__ import annotations

import json

import pytest

from prosperos_hoard import productions as prod
from prosperos_hoard.hoard_link import tokens as link_tokens
from test_productions import tiny_spec
from test_project_trash import _image


class Agent:
    """A caller of `/api/agent/*` with a token, an agent id and a session."""

    def __init__(self, c, app):
        self.c, self.app = c, app
        self.store = app.state.store
        self.token = (self.store.data_dir / "mcp-token").read_text(encoding="utf-8").strip()

    def call(self, name, arguments=None, *, agent="agent-a", session="s1", reason="Because the person asked for it", token=None, **extra):
        headers = {"Authorization": f"Bearer {token or self.token}", "X-Agent-Id": agent, "X-Agent-Session": session}
        body = {"name": name, "arguments": arguments or {}}
        if reason is not None:
            body["reason"] = reason
        body.update(extra)
        return self.c.post("/api/agent/call", json=body, headers=headers)

    def ok(self, name, arguments=None, **kw):
        r = self.call(name, arguments, **kw)
        assert r.status_code == 200, r.text
        return r.json()

    def undo(self, session, *, agent="agent-a", token=None, **body):
        headers = {"Authorization": f"Bearer {token or self.token}", "X-Agent-Id": agent}
        if body.get("confirm"):
            body.setdefault("reason", "Taking the session back")
        return self.c.post("/api/agent/undo", json={"session": session, **body}, headers=headers)

    def undone(self, session, **body):
        r = self.undo(session, **body)
        assert r.status_code == 200, r.text
        return r.json()

    def journal(self, **params):
        return self.c.get("/api/agent/journal", params=params, headers={"Authorization": f"Bearer {self.token}"}).json()

    def mint(self, agent, profile):
        return link_tokens.mint_agent_token(self.store.data_dir, agent, profile)["token"]


@pytest.fixture
def agent(client):
    c, app, _ = client
    return Agent(c, app)


def project_row(store, pid):
    return store.conn.execute("SELECT * FROM projects WHERE id=?", (pid,)).fetchone()


@pytest.fixture
def made_production(agent):
    """A production made by the services (not by an agent), parked so no run touches it."""
    store = agent.store
    project = store.create_project("Video")
    state = prod.create_production(store.data_dir, "Clip", prod.normalise_spec(tiny_spec()), {}, project_id=project["id"])
    return project["id"], state["slug"]


def spec_of(store, slug):
    return prod.load_state(store.data_dir, slug)["spec"]


# ---------------------------------------------------------------- reasons

def test_a_write_without_a_reason_is_refused_with_a_hint(agent):
    r = agent.call("studio_create_project", {"name": "No reason"}, reason=None)
    assert r.status_code == 400 and r.json()["code"] == "reason_required" and r.json()["hint"]
    assert agent.call("studio_create_project", {"name": "No reason"}, reason="no").status_code == 400      # shorter than 3 characters
    assert agent.ok("studio_projects", reason=None)["items"] == []                                       # reads need no reason, and nothing was made


def test_a_reason_may_also_travel_among_the_arguments(agent):
    made = agent.ok("studio_create_project", {"name": "Inside", "reason": "Asked for a new project"}, reason=None)
    assert made["name"] == "Inside"
    assert agent.journal()["entries"][0]["reason"] == "Asked for a new project"


def test_the_web_ui_and_the_per_tool_routes_are_exempt_from_reasons(agent):
    r = agent.c.post("/api/projects", json={"name": "From the interface"})
    assert r.status_code in (200, 201) and r.json()["name"] == "From the interface"
    assert agent.journal()["entries"] == []                       # the interface is not an agent: nothing in the agent journal


def test_the_catalogue_tells_agents_about_reasons_and_draft_safe_tools(agent):
    body = agent.c.get("/api/agent/tools").json()
    tools = {t["name"]: t for t in body["tools"]}
    assert body["reasons_required"] is True and "reason" in body["instructions"]
    assert "reason" in tools["studio_cast"]["inputSchema"]["properties"] and "reason" in tools["studio_cast"]["inputSchema"]["required"]
    assert "reason" not in tools["studio_projects"]["inputSchema"]["properties"]
    for name in ("studio_create_project", "studio_graphic_shot", "studio_import", "studio_generate_image", "voice_speak"):
        assert tools[name]["annotations"]["draftSafeHint"] is True, name
    for name in ("studio_delete_assets", "studio_delete_project", "studio_trash", "studio_cast", "studio_style_cards", "studio_cancel_job",
                 "studio_production_shots", "studio_timeline", "studio_spaces", "studio_service_stop", "production_export_lumiere"):
        assert "draftSafeHint" not in tools[name]["annotations"], name
    assert "draftSafeHint" not in tools["studio_projects"]["annotations"]
    undo_tools = agent.journal()["undo_tools"]
    assert {"studio_cast", "studio_style_cards", "studio_delete_assets", "studio_delete_project", "studio_trash",
            "studio_production_shots", "studio_graphic_shot", "studio_create_project"} <= set(undo_tools)


def test_every_undo_hook_and_draft_safe_name_is_a_real_write_tool(agent):
    from prosperos_hoard import agent_undo
    tools = {t["name"]: t for t in agent.c.get("/api/agent/tools").json()["tools"]}
    for name in set(agent_undo.HOOKS) | set(agent_undo.DRAFT_SAFE):
        assert name in tools and tools[name]["annotations"]["readOnlyHint"] is False, name
    for name, hooks in agent_undo.HOOKS.items():
        assert "undo" in hooks, name                                       # a hook set always says how to take the write back


def test_an_unknown_tool_and_bad_arguments_keep_their_answers(agent):
    unknown = agent.call("nope")
    assert unknown.status_code == 404 and "studio_status" in unknown.json()["tools"]
    typo = agent.call("studio_create_project", {"name": "x", "bref": "b"})
    assert typo.status_code == 400 and typo.json()["code"] == "unknown_argument"
    assert agent.call("studio_status", reason=None, arguments=None).status_code == 200


# ---------------------------------------------------------------- projects, assets and the trash

def test_undoing_a_session_that_created_and_filled_a_project(agent):
    pid = agent.ok("studio_create_project", {"name": "Session project", "brief": "b"})["id"]
    ana = agent.ok("studio_cast", {"project": pid, "action": "create", "name": "Ana", "fields": {"prompt": "a tall woman"}})["id"]
    agent.ok("studio_cast", {"project": pid, "action": "update", "id": ana, "fields": {"prompt": "a short woman"}})
    entries = agent.journal(session="s1")["entries"]
    assert [e["tool"] for e in entries] == ["studio_create_project", "studio_cast", "studio_cast"]
    assert all(e["agent"] == "agent-a" and e["reason"] and e["ok"] and e["undoable"] for e in entries)

    plan = agent.undone("s1", dry_run=True)
    assert plan["complete"] is True and len(plan["would_undo"]) == 3 and project_row(agent.store, pid)["deleted_at"] is None
    refused = agent.undo("s1")                                                  # no confirm: nothing happens
    assert refused.status_code == 400 and refused.json()["code"] == "confirm_required"

    done = agent.undone("s1", confirm=True)
    assert len(done["undone"]) == 3 and not done["conflicts"] and not done["not_undoable"] and done["complete"] is True
    assert project_row(agent.store, pid)["deleted_at"]                          # the project is in the trash, not deleted for good
    assert pid in [p["id"] for p in agent.store.list_trashed_projects()]
    assert agent.undone("s1", confirm=True)["undone"] == []                     # a second go finds nothing left to do


def test_undo_brings_back_deleted_assets_and_a_deleted_project(agent):
    store = agent.store
    p = store.create_project("Keep")
    a, b = _image(store, p["id"])["id"], _image(store, p["id"])["id"]
    gone = agent.ok("studio_delete_assets", {"ids": [a, b]})
    assert sorted(gone["deleted"]) == sorted([a, b]) and store.list_assets(p["id"])["items"] == []
    agent.ok("studio_delete_project", {"project": p["id"]})
    assert project_row(store, p["id"])["deleted_at"]

    done = agent.undone("s1", confirm=True)
    assert len(done["undone"]) == 2 and done["complete"] is True
    assert project_row(store, p["id"])["deleted_at"] is None
    assert sorted(x["id"] for x in store.list_assets(p["id"])["items"]) == sorted([a, b])
    assert store.list_trash() == []


def test_undo_of_a_restore_puts_it_back_in_the_trash(agent):
    store = agent.store
    p = store.create_project("Other")
    a = _image(store, p["id"])["id"]
    store.trash_asset(a)
    agent.ok("studio_trash", {"action": "restore", "ids": [a]})
    assert [x["id"] for x in store.list_assets(p["id"])["items"]] == [a]
    done = agent.undone("s1", confirm=True)
    assert len(done["undone"]) == 1 and store.list_assets(p["id"])["items"] == [] and [t["id"] for t in store.list_trash()] == [a]


def test_emptying_the_trash_cannot_be_undone_and_a_list_changes_nothing(agent):
    store = agent.store
    p = store.create_project("Gone")
    a = _image(store, p["id"])["id"]
    store.trash_asset(a)
    agent.ok("studio_trash", {"action": "list"})
    agent.ok("studio_trash", {"action": "empty", "ids": [a]})
    done = agent.undone("s1", confirm=True)
    assert [n["tool"] for n in done["not_undoable"]] == ["studio_trash"] and done["complete"] is False
    assert [e["undoable"] for e in agent.journal(session="s1")["entries"] if e["kind"] == "write"] == [True, False]


def test_an_import_is_undone_by_moving_the_asset_to_the_trash(agent, client):
    c, app, allowed = client
    p = agent.store.create_project("Imports")
    from PIL import Image
    Image.new("RGB", (16, 16), (1, 2, 3)).save(allowed / "pic.png")
    made = agent.ok("studio_import", {"project": p["id"], "path": str(allowed / "pic.png")})
    assert [x["id"] for x in agent.store.list_assets(p["id"])["items"]] == [made["id"]]
    done = agent.undone("s1", confirm=True)
    assert len(done["undone"]) == 1 and agent.store.list_assets(p["id"])["items"] == [] and [t["id"] for t in agent.store.list_trash()] == [made["id"]]


# ---------------------------------------------------------------- cast and style cards

def test_cast_delete_restore_update_and_groups_are_undone(agent):
    store = agent.store
    p = store.create_project("Cast")
    pid = p["id"]
    ana = store.create_character(pid, "Ana", prompt="a tall woman", role="lead")
    group = store.create_group(pid, "The band", concept="loud")
    agent.ok("studio_cast", {"project": pid, "action": "update", "id": ana["id"], "fields": {"prompt": "a very different look", "role": "bass"}})
    agent.ok("studio_cast", {"project": pid, "action": "delete", "id": ana["id"]})
    agent.ok("studio_cast", {"project": pid, "action": "delete", "kind": "group", "id": group["id"]})
    agent.ok("studio_cast", {"project": pid, "action": "create", "kind": "location", "name": "Street", "fields": {"prompt": "wet street"}})
    agent.ok("studio_cast", {"project": pid, "action": "list"})
    assert [c["name"] for c in store.list_characters(pid)] == ["Street"] and store.list_groups(pid) == []

    plan = agent.undone("s1", dry_run=True)
    assert len(plan["would_undo"]) == 5 and plan["complete"] is True          # the list call is a no-op that is still reported as undone
    done = agent.undone("s1", confirm=True)
    assert len(done["undone"]) == 5 and done["complete"] is True
    back = store.get_character(ana["id"])
    assert back["prompt"] == "a tall woman" and back["role"] == "lead" and not back["deleted_at"]
    assert [g["name"] for g in store.list_groups(pid)] == ["The band"]
    assert [c["name"] for c in store.list_characters(pid)] == ["Ana"]
    assert [c["name"] for c in store.list_characters(pid, deleted=True)] == ["Street"]    # the created place is soft-deleted, restorable


def test_style_cards_created_edited_and_deleted_come_back(agent):
    store = agent.store
    mine = store.create_style_preset("Mine", prompt_prefix="warm", notes="first")
    created = agent.ok("studio_style_cards", {"action": "create", "name": "Fresh", "prompt_prefix": "cold"})
    agent.ok("studio_style_cards", {"action": "update", "id": mine["id"], "prompt_prefix": "very warm", "notes": "second"})
    agent.ok("studio_style_cards", {"action": "delete", "id": created["id"]})
    other = store.create_style_preset("Doomed", prompt_prefix="x")
    agent.ok("studio_style_cards", {"action": "delete", "id": other["id"]}, session="s2")
    assert [c["name"] for c in store.list_style_presets() if not c["is_builtin"]] == ["Mine"]

    done = agent.undone("s1", confirm=True)
    assert len(done["undone"]) == 3 and done["complete"] is True
    names = {c["name"]: c for c in store.list_style_presets() if not c["is_builtin"]}
    assert set(names) == {"Mine"}                                             # Fresh was created and deleted by the session: gone again
    assert names["Mine"]["prompt_prefix"] == "warm" and names["Mine"]["notes"] == "first"
    done2 = agent.undone("s2", confirm=True)
    assert len(done2["undone"]) == 1 and {c["name"] for c in store.list_style_presets() if not c["is_builtin"]} == {"Mine", "Doomed"}


# ---------------------------------------------------------------- productions

def test_production_edits_are_undone_from_a_copy_of_the_state(agent, made_production):
    pid, slug = made_production
    store = agent.store
    original = json.dumps(spec_of(store, slug), sort_keys=True)
    settings_before = prod.load_state(store.data_dir, slug)["settings"]
    agent.ok("studio_production_shots", {"production": slug, "changes": [{"key": "1", "prompt": "a different prompt"}], "run": False})
    agent.ok("studio_graphic_shot", {"production": slug, "grammar": "title_card", "data": {"title": "Hola"}, "start_s": 0.0, "end_s": 2.0, "run": False})
    agent.ok("studio_production_settings", {"production": slug, "animatic": False})
    assert spec_of(store, slug)["shots"][0]["prompt"] == "a different prompt" and any(s.get("kind") == "graphic" for s in spec_of(store, slug)["shots"])
    assert prod.load_state(store.data_dir, slug)["settings"]["animatic"] is False
    objects = [e["objects"] for e in agent.journal(session="s1")["entries"]]
    assert objects == [[f"production:{slug}"]] * 3

    plan = agent.undone("s1", dry_run=True)
    assert len(plan["would_undo"]) == 3 and plan["complete"] is True and spec_of(store, slug)["shots"][0]["prompt"] == "a different prompt"
    done = agent.undone("s1", confirm=True)
    assert len(done["undone"]) == 3 and done["complete"] is True
    assert json.dumps(spec_of(store, slug), sort_keys=True) == original
    assert prod.load_state(store.data_dir, slug)["settings"] == settings_before
    assert (store.data_dir / "agent_undo" / "productions" / slug).is_dir()


def test_a_production_that_changed_after_the_edit_is_a_conflict(agent, made_production):
    pid, slug = made_production
    store = agent.store
    agent.ok("studio_production_shots", {"production": slug, "changes": [{"key": "1", "prompt": "agent prompt"}], "run": False})
    # a person edits another shot through the interface: the journal never sees it
    r = agent.c.patch(f"/api/productions/{slug}/shots", json={"changes": [{"key": "2", "prompt": "person prompt"}], "run": False})
    assert r.status_code == 200, r.text
    done = agent.undone("s1", confirm=True)
    assert done["undone"] == [] and len(done["conflicts"]) == 1
    assert spec_of(store, slug)["shots"][0]["prompt"] == "agent prompt" and spec_of(store, slug)["shots"][1]["prompt"] == "person prompt"


def test_a_production_edit_that_queued_a_run_is_undone_and_the_run_cancelled(agent, made_production):
    pid, slug = made_production
    store = agent.store
    agent.app.state.queue.stop()                                          # no worker: the run stays queued
    original = spec_of(store, slug)["shots"][0]["prompt"]
    agent.ok("studio_production_shots", {"production": slug, "changes": [{"key": "1", "prompt": "queued change"}]})
    job_id = prod.load_state(store.data_dir, slug)["job_id"]
    assert store.get_job(job_id)["state"] == "queued"
    plan = agent.undone("s1", dry_run=True)
    assert plan["would_undo"][0]["detail"]["would_cancel_queued_job"] == job_id
    done = agent.undone("s1", confirm=True)
    assert len(done["undone"]) == 1 and done["undone"][0]["detail"]["cancelled_queued_job"] == job_id
    assert spec_of(store, slug)["shots"][0]["prompt"] == original and store.get_job(job_id)["state"] == "cancelled"


def test_a_created_production_goes_to_the_trash_folder_when_undone(agent):
    store = agent.store
    agent.app.state.queue.stop()
    made = agent.ok("studio_production_create", {"name": "Agent clip", "spec": tiny_spec()})
    slug = made["production"]["slug"]
    assert prod.production_dir(store.data_dir, slug).is_dir()
    done = agent.undone("s1", confirm=True)
    assert len(done["undone"]) == 1 and not prod.production_dir(store.data_dir, slug).exists()
    moved = list((store.data_dir / "trash" / "agent_undo" / "productions").glob(f"{slug}-*"))
    assert len(moved) == 1 and (moved[0] / "state.json").is_file()
    assert store.get_job(made["job"]["id"])["state"] == "cancelled"


# ---------------------------------------------------------------- sessions do not touch each other

def test_a_later_edit_by_another_session_is_a_conflict_and_is_left_alone(agent):
    store = agent.store
    p = store.create_project("Shared")
    ana = store.create_character(p["id"], "Ana", prompt="original")
    args = {"project": p["id"], "action": "update", "id": ana["id"]}
    agent.ok("studio_cast", {**args, "fields": {"prompt": "A was here"}}, session="sa", agent="agent-a")
    agent.ok("studio_cast", {**args, "fields": {"prompt": "B was here"}}, session="sb", agent="agent-b")

    plan = agent.undone("sa", dry_run=True)
    assert plan["would_undo"] == [] and len(plan["conflicts"]) == 1 and plan["complete"] is False
    assert plan["conflicts"][0]["reason"] == "later_write_by_other_session"
    done = agent.undone("sa", confirm=True)
    assert done["undone"] == [] and len(done["conflicts"]) == 1
    assert store.get_character(ana["id"])["prompt"] == "B was here"           # the other session's work survived

    # once B is undone, A's edit can be taken back as well
    agent.undone("sb", agent="agent-b", confirm=True)
    assert store.get_character(ana["id"])["prompt"] == "A was here"
    agent.undone("sa", confirm=True)
    assert store.get_character(ana["id"])["prompt"] == "original"


def test_a_change_made_outside_the_session_is_a_conflict_too(agent):
    store = agent.store
    p = store.create_project("Edited by hand")
    ana = store.create_character(p["id"], "Ana", prompt="original")
    agent.ok("studio_cast", {"project": p["id"], "action": "update", "id": ana["id"], "fields": {"prompt": "from the agent"}})
    r = agent.c.patch(f"/api/characters/{ana['id']}", json={"fields": {"prompt": "from the person"}})
    assert r.status_code == 200, r.text
    done = agent.undone("s1", confirm=True)
    assert done["undone"] == [] and len(done["conflicts"]) == 1 and done["conflicts"][0]["reason"] == "changed_since"
    assert store.get_character(ana["id"])["prompt"] == "from the person"


def test_tools_without_a_way_back_are_reported_not_undone(agent):
    store = agent.store
    p = store.create_project("Services")
    timeline = store.create_timeline(p["id"], "Cut")
    agent.ok("studio_create_project", {"name": "Mine"})
    agent.ok("studio_timeline", {"project": p["id"], "action": "get", "timeline_id": timeline["id"]})
    done = agent.undone("s1", confirm=True)
    assert [n["tool"] for n in done["not_undoable"]] == ["studio_timeline"] and done["not_undoable"][0]["reason"] == "no_handler"
    assert len(done["undone"]) == 1 and done["complete"] is False


# ---------------------------------------------------------------- tokens and profiles

def test_a_read_only_token_cannot_write(agent):
    token = agent.mint("reader", "read_only")
    assert agent.ok("studio_projects", token=token, agent="spoofed")["items"] == []
    r = agent.call("studio_create_project", {"name": "No"}, token=token)
    assert r.status_code == 403 and r.json()["code"] == "profile_forbidden"
    assert agent.ok("studio_projects")["items"] == []


def test_a_drafts_token_may_create_and_edit_drafts_but_not_delete(agent, made_production):
    pid, slug = made_production
    store = agent.store
    token = agent.mint("drafter", "drafts")
    made = agent.ok("studio_create_project", {"name": "Draft project"}, token=token)
    agent.ok("studio_graphic_shot", {"production": slug, "grammar": "title_card", "data": {"title": "Hola"}, "start_s": 0.0, "end_s": 2.0, "run": False}, token=token)
    for name, args in (("studio_delete_project", {"project": made["id"]}),
                       ("studio_delete_assets", {"ids": ["x"]}),
                       ("studio_cast", {"project": made["id"], "action": "create", "name": "Ana"}),
                       ("studio_trash", {"action": "empty"}),
                       ("studio_production_shots", {"production": slug, "changes": [{"key": "1", "delete": True}], "run": False})):
        r = agent.call(name, args, token=token)
        assert r.status_code == 403 and r.json()["code"] == "profile_forbidden", name
    assert project_row(store, made["id"])["deleted_at"] is None
    # the token fixes the agent: the journal says "drafter" whatever the headers claim
    assert {e["agent"] for e in agent.journal()["entries"]} == {"drafter"}
    # a drafts token may not undo (only the "all" profile can)
    assert agent.undo("s1", token=token, confirm=True).status_code == 403


def test_an_all_token_can_undo_its_own_session(agent):
    token = agent.mint("writer", "all")
    made = agent.ok("studio_create_project", {"name": "With a token"}, token=token)
    done = agent.undone("s1", token=token, confirm=True)
    assert len(done["undone"]) == 1 and project_row(agent.store, made["id"])["deleted_at"]


# ---------------------------------------------------------------- the journal itself

def test_the_journal_records_who_why_and_masks_secrets(agent):
    agent.ok("studio_create_project", {"name": "With a secret", "brief": "key sk-abcdefghijklmnopqrstuvwxyz0123456789 in the text"},
             reason="Prepare it; password=hunter2hunter2", agent="agent-z", session="zz")
    entry = agent.journal(agent="agent-z")["entries"][0]
    assert entry["tool"] == "studio_create_project" and entry["session"] == "zz" and entry["ok"] and entry["ids"]
    text = json.dumps(entry)
    assert "sk-abcdefghij" not in text and "hunter2hunter2" not in text
    assert len(entry["args_summary"]) <= 300
    assert agent.c.get("/api/agent/journal").status_code == 401


def test_failed_writes_are_journaled_but_never_undone(agent):
    r = agent.call("studio_cast", {"project": "ghost", "action": "create", "name": "Nobody"})
    assert r.status_code == 404
    entries = agent.journal(session="s1")["entries"]
    assert entries and entries[0]["ok"] is False
    assert agent.undone("s1", confirm=True)["undone"] == []
