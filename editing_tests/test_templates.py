"""Project templates with media slots: save a project as a template, fill the slots with media by name, keep everything else (titles,
captions, music, effects), and render the result. Also from edit plans, the MCP tools and the REST routes."""

import json
import subprocess
from pathlib import Path

import pytest
from PIL import Image

from conftest import FakeLink, fake_transcript, make_services, needs_ffmpeg, probe
from prosperos_hoard.editing import media as media_store
from prosperos_hoard.editing import plan as plan_mod
from prosperos_hoard.editing import projects as store
from prosperos_hoard.editing.agent_tools import call_tool, tool_catalog
from prosperos_hoard.editing.errors import LumiereError, NotFound

pytestmark = needs_ffmpeg


@pytest.fixture
def lab(tmp_path, media_dir):
    svc = make_services(tmp_path)
    svc.start()
    lib = {}
    for name in ("talk.mp4", "vert.mp4", "pic.png", "scenes.mp4", "clicks.wav"):
        lib[name.split(".")[0]] = media_store.import_path(svc, str(media_dir / name))["id"]
    yield svc, lib
    svc.stop()


def make_source(svc, lib):
    """The edit a creator reuses: a 2 s intro (blue picture), the main part (4 s of talk, in black and white), a 2 s outro (red), a title,
    karaoke captions and music under all of it."""
    pid = store.create(svc, "Mi formato", preset="hd720")["id"]
    res = store.edit(svc, pid, [{"op": "add_media", "media": lib["pic"], "length": 2000},
                                {"op": "add_media", "media": lib["talk"], "src_in": 0, "src_out": 4000},
                                {"op": "add_media", "media": lib["scenes"], "src_in": 0, "src_out": 2000},
                                {"op": "add_text", "text": "Mi canal", "start": 0, "length": 1500},
                                {"op": "add_text", "text": "Suscríbete", "start": 6200, "length": 1500},
                                {"op": "add_media", "media": lib["clicks"], "at": 0, "src_in": 0, "src_out": 8000},
                                {"op": "marker_add", "t": 6000, "label": "Final", "kind": "chapter"},
                                {"op": "captions", "enabled": True, "style": "karaoke"}])["results"]
    intro, main, outro = res[0]["clip"], res[1]["clip"], res[2]["clip"]
    store.edit(svc, pid, [{"op": "filter_add", "clips": [main], "type": "grayscale"}, {"op": "set", "clip": main, "props": {"volume_db": -3}}])
    return pid, {"intro": intro, "main": main, "outro": outro}


def test_saving_a_template_keeps_the_edit_and_lists_its_slots(lab):
    svc, lib = lab
    pid, clips = make_source(svc, lib)
    with pytest.raises(LumiereError, match="at least one slot"):
        store.save_template(svc, pid, "Sin huecos")
    t = store.save_template(svc, pid, "Formato canal", {clips["intro"]: "intro", clips["main"]: "main", clips["outro"]: "outro"})
    assert t["is_template"] and t["name"] == "Formato canal" and t["id"] != pid
    assert [(s["slot"], s["track_role"], s["length_ms"], s["rule"]) for s in t["slots"]] == [("intro", "main", 2000, "keep"), ("main", "main", 4000, "full"),
                                                                                              ("outro", "main", 2000, "keep")]
    assert [s["name"] for s in t["slots"]] == ["pic", "talk", "scenes"]  # the sample media each slot holds until it is filled
    assert not any(c.slot for _, c in store.doc(svc, pid).all_clips())  # the original project is untouched
    assert [x["id"] for x in store.list_templates(svc)] == [t["id"]] and store.list_projects(svc, templates=False)[0]["id"] == pid
    with pytest.raises(LumiereError, match="already on clip"):
        store.save_template(svc, pid, "Mal", {clips["intro"]: "x", clips["main"]: "x"})
    with pytest.raises(NotFound):
        store.save_template(svc, pid, "Mal", {"clp_nada": "x"})


def frame_at(video: Path, t: float, out: Path) -> tuple[float, float, float]:
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", f"{t}", "-i", str(video), "-frames:v", "1", str(out)], check=True)
    img = Image.open(out).convert("RGB").resize((16, 9))
    px = list(img.getdata())
    return tuple(sum(p[i] for p in px) / len(px) for i in range(3))


def test_filling_the_slots_replaces_the_media_keeps_the_rest_and_renders(lab, tmp_path):
    svc, lib = lab
    pid, clips = make_source(svc, lib)
    fake_transcript(svc, lib["vert"], [(300, 800, "hola"), (900, 1500, "mundo")])
    t = store.save_template(svc, pid, "Formato canal", {clips["intro"]: "intro", clips["main"]: "main", clips["outro"]: "outro"})
    # slots are filled with media by name (with or without extension) or by id; the new project is a plain project, not a template
    out = store.create_from_template(svc, "Episodio 7", t["name"], {"intro": "scenes.mp4", "main": "vert", "outro": lib["pic"]})
    assert out["name"] == "Episodio 7" and not out["is_template"] and out["unfilled"] == [] and out["template"] == t["id"]
    p = store.doc(svc, out["id"])
    main = sorted(p.main_track().clips, key=lambda c: c.start)
    assert [(c.slot, c.media, c.start, c.end) for c in main] == [("intro", lib["scenes"], 0, 2000), ("main", lib["vert"], 2000, 8000),
                                                                 ("outro", lib["pic"], 8000, 10000)]
    assert (main[0].src_in, main[0].src_out) == (0, 2000) and (main[1].src_in, main[1].src_out) == (0, 6000)  # main takes the media's full length
    # everything outside the slots stays; what sat after the main part moved with the outro
    assert [f.type for f in main[1].filters] == ["grayscale"] and main[1].volume_db == -3
    assert p.captions.enabled and p.captions.style == "karaoke"
    texts = {c.text: c.start for _, c in p.all_clips() if c.type == "text"}
    assert texts == {"Mi canal": 0, "Suscríbete": 8200}
    assert [m.t for m in p.markers] == [8000]
    music = next(c for tr in p.tracks if tr.role == "music" for c in tr.clips)
    assert music.media == lib["clicks"] and (music.start, music.end) == (0, 10000)  # the music follows the new end
    assert store.history(svc, out["id"])["items"][0]["label"].startswith("Desde plantilla")
    assert store.doc(svc, t["id"]).main_track().clips[1].media == lib["talk"]  # the template itself is not consumed
    # the render shows the new media where the slots are, and is as long as the filled edit
    job = svc.start_render(out["id"], preset="web")
    res = svc.jobs.get(job["id"])
    assert res["state"] == "done", res["error"]
    video = Path(res["result"]["path"])
    assert abs(res["result"]["qc"]["duration_ms"] - 10000) <= 80 and res["result"]["qc"]["ok"]
    assert any(s["codec_type"] == "audio" for s in probe(video)["streams"])
    red = frame_at(video, 1.7, tmp_path / "f1.png")            # intro: the red scene (after the title is gone)
    blue = frame_at(video, 9.0, tmp_path / "f2.png")           # outro: the blue picture
    gray = frame_at(video, 5.0, tmp_path / "f3.png")           # main: the vertical test card, still in black and white
    assert red[0] > 200 and red[1] < 70 and red[2] < 70, red
    assert blue[2] > 150 and blue[0] < 100, blue
    assert max(gray) - min(gray) < 14, gray
    # the unfilled-slot case: what is not named keeps the template's sample media and is reported
    half = store.create_from_template(svc, "Solo intro", t["id"], {"intro": {"media": "scenes", "length": 1000}})
    assert half["unfilled"] == ["main", "outro"]
    q = sorted(store.doc(svc, half["id"]).main_track().clips, key=lambda c: c.start)
    assert [(c.media, c.start, c.end) for c in q] == [(lib["scenes"], 0, 1000), (lib["talk"], 1000, 5000), (lib["scenes"], 5000, 7000)]


def test_slot_problems_are_explained(lab):
    svc, lib = lab
    pid, clips = make_source(svc, lib)
    t = store.save_template(svc, pid, "Formato", {clips["intro"]: "intro", clips["main"]: "main"})
    with pytest.raises(LumiereError, match="Unknown slot 'outro'. This template has: intro, main"):
        store.create_from_template(svc, "x", t["id"], {"outro": "pic"})
    with pytest.raises(NotFound, match="No media called 'nada'"):
        store.create_from_template(svc, "x", t["id"], {"main": "nada"})
    media_store.rename(svc, lib["vert"], "talk")  # two media with the same name cannot be told apart
    with pytest.raises(LumiereError, match="2 media match 'talk'"):
        store.create_from_template(svc, "x", t["id"], {"main": "talk"})
    with pytest.raises(NotFound, match="No template 'otra'. Templates: Formato"):
        store.create_from_template(svc, "x", "otra", {})
    with pytest.raises(LumiereError, match="no picture"):
        store.create_from_template(svc, "x", t["id"], {"main": lib["clicks"]})
    with pytest.raises(LumiereError, match="has no slots"):
        store.create_from_template(svc, "x", store.create(svc, "plano", preset="hd720")["id"], {})
    store.save_template(svc, pid, "Formato", {clips["outro"]: "outro"})
    with pytest.raises(LumiereError, match="2 templates are called 'formato'"):
        store.create_from_template(svc, "x", "formato", {})


def test_from_edit_plans_with_the_fill_slot_op(tmp_path, media_dir):
    answers = {}
    link = FakeLink(responder=lambda messages: answers["plan"])
    svc = make_services(tmp_path, link=link)
    svc.start()
    try:
        lib = {n.split(".")[0]: media_store.import_path(svc, str(media_dir / n))["id"] for n in ("talk.mp4", "vert.mp4", "pic.png", "scenes.mp4", "clicks.wav")}
        pid, clips = make_source(svc, lib)
        store.edit(svc, pid, [{"op": "set", "clip": clips["main"], "props": {"slot": "main"}}, {"op": "set", "clip": clips["intro"], "props": {"slot": "intro"}}])
        answers["plan"] = json.dumps({"steps": [
            {"kind": "op", "name": "fill_slot", "args": {"slot": "intro", "media": lib["scenes"]}, "explain": "Nueva intro"},
            {"kind": "op", "name": "fill_slot", "args": {"slot": "main", "media": lib["vert"]}, "explain": "El episodio nuevo"}], "notes": ""})
        plan = plan_mod.create(svc, pid, "cambia la intro por la escena roja y el cuerpo por el vídeo vertical")
        assert plan["source"] == "model" and [s["name"] for s in plan["steps"]] == ["fill_slot", "fill_slot"]
        system = link.calls[0][0]["content"]
        assert "fill_slot {slot, media" in system and "add_overlay {media, start, length" in system and "slot}}" in system  # the plan writer knows both ops
        res = plan_mod.apply(svc, plan["id"])
        assert res["applied"] and res["duration"] == "0:10.000"  # 2 s intro + the 6 s vertical clip + the 2 s outro
        main = sorted(store.doc(svc, pid).main_track().clips, key=lambda c: c.start)
        assert [(c.media, c.start, c.end) for c in main] == [(lib["scenes"], 0, 2000), (lib["vert"], 2000, 8000), (lib["scenes"], 8000, 10000)]
        # by hand, with an edited plan: a step naming a slot that does not exist fails the whole plan and changes nothing
        bad = plan_mod.update(svc, plan_mod.create(svc, pid, "x", use_model=False)["id"],
                              [{"kind": "op", "name": "fill_slot", "args": {"slot": "nope", "media": lib["pic"]}}])
        with pytest.raises(NotFound, match="Slots here: intro, main"):
            plan_mod.apply(svc, bad["id"])
    finally:
        svc.stop()


def test_tools_create_projects_from_templates(lab):
    svc, lib = lab
    pid, clips = make_source(svc, lib)
    names = {t["name"] for t in tool_catalog()}
    assert {"template_save", "template_list", "project_create"} <= names
    saved = call_tool(svc, "template_save", {"project": pid, "name": "Canal", "slots": {clips["intro"]: "intro", clips["main"]: "main"}})
    assert [s["slot"] for s in saved["slots"]] == ["intro", "main"]
    listed = call_tool(svc, "template_list", {})["templates"]
    assert listed[0]["name"] == "Canal" and listed[0]["slots"][1] == {"slot": "main", "kind": "video", "length": "0:04.000", "sample": "talk", "rule": "full"}
    made = call_tool(svc, "project_create", {"name": "Hoy", "template": "canal", "slots": {"main": "vert", "intro": "pic"}})
    assert made["name"] == "Hoy" and made["unfilled"] == [] and made["template"] == saved["id"] and made["duration"] == "0:10.000"
    assert [f["slot"] for f in made["filled"]] == ["intro", "main"] and made["filled"][1]["rule"] == "full"
    only = call_tool(svc, "project_create", {"name": "Solo plantilla", "template": saved["id"]})
    assert only["unfilled"] == ["intro", "main"] and only["duration"] == "0:08.000"
    with pytest.raises(LumiereError, match="not media"):
        call_tool(svc, "project_create", {"template": "canal", "media": [lib["talk"]]})
    with pytest.raises(LumiereError, match="only go with a template"):
        call_tool(svc, "project_create", {"slots": {"main": "vert"}})
    # slots can be set by hand on any clip with the set operation, and a project with slots works as a template too
    plain = store.create(svc, "Plano", preset="hd720", media=[lib["talk"]])
    clip = store.doc(svc, plain["id"]).main_track().clips[0]
    call_tool(svc, "timeline_edit", {"project": plain["id"], "ops": [{"op": "set", "clip": clip.id, "props": {"slot": "main"}}]})
    again = call_tool(svc, "project_create", {"name": "Copia", "template": plain["id"], "slots": {"main": "scenes"}})
    assert again["duration"] == "0:06.000"


def test_rest_routes_save_list_and_use_templates(client, media_dir):
    ids = {}
    for name in ("talk.mp4", "vert.mp4"):
        ids[name] = client.post("/api/media/import", json={"path": str(media_dir / name)}).json()["id"]
    pid = client.post("/api/projects", json={"name": "Base", "preset": "hd720", "media": [ids["talk.mp4"]]}).json()["id"]
    clip = client.get(f"/api/projects/{pid}").json()["doc"]["tracks"][0]["clips"][0]["id"]
    saved = client.post(f"/api/projects/{pid}/template", json={"name": "Plantilla API", "slots": {clip: "main"}})
    assert saved.status_code == 200 and saved.json()["slots"][0]["slot"] == "main"
    listing = client.get("/api/templates").json()["templates"]
    assert [t["name"] for t in listing] == ["Plantilla API"] and listing[0]["slots"][0]["rule"] == "full"
    made = client.post(f"/api/templates/{saved.json()['id']}/create", json={"name": "Desde API", "slots": {"main": "vert"}})
    assert made.status_code == 200 and made.json()["duration"] == "0:06.000" and made.json()["unfilled"] == []
    assert "Desde API" in [p["name"] for p in client.get("/api/projects", params={"templates": "false"}).json()["projects"]]
    bad = client.post(f"/api/templates/{saved.json()['id']}/create", json={"slots": {"intro": "vert"}})
    assert bad.status_code >= 400 and "Unknown slot" in json.dumps(bad.json())
