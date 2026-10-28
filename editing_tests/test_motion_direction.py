"""Direction of HTML animations: treatment, cues, cuecheck, readcheck, facts, review modes, creation from a style card."""
import importlib.util
import json
from io import BytesIO

import pytest
from fastapi.testclient import TestClient
from PIL import Image

from conftest import make_services
from prosperos_hoard import style_cards
from prosperos_hoard.editing import motion_direction as direction, motion_scaffold
from prosperos_hoard.editing.agent_tools import call_tool, tool_catalog
from prosperos_hoard.editing.errors import LumiereError, NotFound
from prosperos_hoard.editing.html_motion import HtmlMotion, browser_executable
from prosperos_hoard.editing.main import create_app

BROWSER = pytest.mark.skipif(importlib.util.find_spec("playwright") is None or not browser_executable(), reason="optional motion runtime and system browser not installed")

#: two steps of the picture, a flash that decays, and three texts with very different lives
PAGE = """<!doctype html><meta charset=utf-8><style>
body{margin:0;background:#111}#s{position:relative;width:640px;height:360px;overflow:hidden;background:#222;color:#eee;font:28px sans-serif}
#a{position:absolute;left:20px;top:20px}#b{position:absolute;left:20px;top:120px}#c{position:absolute;left:600px;top:240px;white-space:nowrap}
#f{position:absolute;inset:0;background:#fff;opacity:0}</style>
<div id=s><div id=a></div><div id=b></div><div id=c>Overflowing text</div><div id=f></div></div>
<script>window.seek=t=>{
 s.style.background=t>=2?'#4a8':'#222';
 f.style.opacity=t>=1?Math.max(0,.9*Math.exp(-(t-1)*10)).toFixed(4):0;
 a.textContent='Hi'; a.style.opacity=(t>=0.3&&t<2.6)?1:0;
 b.textContent='This sentence has more than sixty characters and is far too long to read'; b.style.opacity=(t>=1&&t<1.7)?1:0;
};seek(0);</script>"""
CUES = [{"t": 1.0, "visual": "white flash", "sound": "hit"}, {"t": 2.0, "visual": "ground turns teal", "sound": "tap"}]


def treatment(duration=8, **over):
    base = {"card": "pixel-rpg",
            "structures": [{"name": "Straight", "idea": "One idea told start to end."}, {"name": "Turn", "idea": "Break the expectation halfway."},
                           {"name": "Return", "idea": "Open and close on the same frame."}],
            "chosen": 1, "reason": "The turn gives the middle shot something to do.",
            "shots": [{"t": 0, "what": "The ground, empty", "why": "Sets the material before anything happens."},
                      {"t": 4, "what": "The title lands", "why": "The one thing to remember, alone on screen."}],
            "cue_map": [{"t": 0.2, "visual": "ground settles", "sound": "tap"}, {"t": 4, "visual": "title lands", "sound": "hit"}]}
    return {**base, **over}


# ------------------------------------------------------------------------------------------------ treatment (no browser)
def test_a_treatment_needs_three_different_structures_shots_with_why_and_a_cue_map():
    assert direction.check_treatment(treatment(), 8)["ok"]
    bad = direction.check_treatment({**treatment(), "structures": treatment()["structures"][:2]}, 8)
    assert not bad["ok"] and any("structures" in p for p in bad["problems"])
    same = treatment(structures=[{"name": "Alpha", "idea": "The same idea twice over."}] * 3)
    assert any("different" in p for p in direction.check_treatment(same, 8)["problems"])
    no_why = treatment(shots=[{"t": 0, "what": "The ground, empty", "why": "x"}])
    assert not direction.check_treatment(no_why, 8)["ok"]
    backwards = treatment(shots=[{"t": 4, "what": "A shot", "why": "Because it opens."}, {"t": 1, "what": "Another", "why": "Because it closes."}])
    assert any("increasing" in p for p in direction.check_treatment(backwards, 8)["problems"])
    assert any("past the end" in p for p in direction.check_treatment(treatment(cue_map=[{"t": 9, "visual": "too late", "sound": None}]), 8)["problems"])
    assert any("explosion" in p for p in direction.check_treatment(treatment(cue_map=[{"t": 1, "visual": "boom", "sound": "explosion"}]), 8)["problems"])
    assert not direction.check_treatment(treatment(chosen=3), 8)["ok"] and not direction.check_treatment(treatment(cue_map=[]), 8)["ok"]


def test_reading_time_follows_the_rule_in_latin_and_cjk():
    assert direction.reading_need("Hello") == pytest.approx(5 / 15 + 1.5, abs=1e-3)
    assert direction.reading_need("你好吗") == pytest.approx(3 / 4.5 + 1.5, abs=1e-3)
    assert direction.reading_need("Un título algo más largo") > direction.reading_need("Hola")


def test_a_draft_from_a_card_is_a_valid_treatment_marked_as_draft():
    card = style_cards.find("lemo-opuscar/archive-lighttable")
    draft = direction.draft_treatment(card, 12, "Dos segundos")
    assert draft["draft"] and len(draft["structures"]) == 3 and direction.check_treatment(draft, 12)["ok"]
    assert draft["shots"][0]["text"] == "Dos segundos" and len(draft["shots"]) == 3 and draft["card"] == "lemo-opuscar/archive-lighttable"
    assert direction.check_treatment(direction.draft_treatment(card, 3), 3)["ok"]


def test_the_scaffold_fits_its_captions_to_the_reading_rule_and_follows_the_card():
    assert motion_scaffold.caption_fits(2.0, 30) <= 3 and motion_scaffold.caption_fits(4.0, 30) >= 28
    assert motion_scaffold.caption_of({"what": "A caption that is much too long for a short shot"}, 20) == "A caption that is"
    assert motion_scaffold.caption_of({"what": "Averyveryveryverylongword"}, 6) == "Averyv"
    assert motion_scaffold.caption_of({"text": "The author's words are never cut, whatever the room"}, 6) == "The author's words are never cut, whatever the room"
    light = motion_scaffold.palette_of(style_cards.find("archive-lighttable"))
    assert light["light"] and motion_scaffold._luma(light["ground"]) > motion_scaffold._luma(light["ink"])
    dark = motion_scaffold.palette_of(style_cards.find("pixel-rpg"))
    assert motion_scaffold._luma(dark["ground"]) != motion_scaffold._luma(dark["ink"]) and dark["accent"].startswith("#")


def test_the_new_tools_are_in_the_catalogue_with_short_first_lines():
    names = {t["name"]: t for t in tool_catalog()} if isinstance(tool_catalog(), list) else tool_catalog()
    for name in ("motion_styles", "motion_treatment", "motion_cues", "motion_cuecheck", "motion_readcheck", "motion_facts", "motion_from_style"):
        assert name in names, name


# ------------------------------------------------------------------------------------------------ cues and sound
def test_cues_own_the_sound_events_and_the_refusal_says_what_to_use(tmp_path):
    svc = make_services(tmp_path)
    store = HtmlMotion(svc.config.data_dir)
    cid = store.save(html=PAGE, name="Cues", width=640, height=360, fps=30, duration=4)["id"]
    assert call_tool(svc, "motion_cues", {"composition": cid})["cues"] == []
    with pytest.raises(LumiereError, match="expected_revision"):
        call_tool(svc, "motion_cues", {"composition": cid, "action": "set", "cues": CUES})
    done = call_tool(svc, "motion_cues", {"composition": cid, "action": "set", "expected_revision": 1,
                                          "cues": [{"t": 2.0, "visual": "ground turns teal", "sound": "tap"}, {"t": 1.0, "visual": "white flash", "sound": "hit"},
                                                   {"t": 3.0, "visual": "silent beat"}]})
    assert done["revision"] == 2 and [c["id"] for c in done["cues"]] == ["c1", "c2", "c3"] and [c["t"] for c in done["cues"]] == [1.0, 2.0, 3.0]
    assert done["events"] == 2, "a cue without sound makes no event"
    sound = store.get(cid)["sound"]
    assert [(e["t"], e["role"]) for e in sound["events"]] == [(1.0, "hit"), (2.0, "land.light")]
    with pytest.raises(LumiereError) as refused:
        call_tool(svc, "motion_sound_events", {"composition": cid, "action": "add", "expected_revision": 2, "events": [{"t": 3.5, "role": "clic"}]})
    assert refused.value.code == "cues_own_sound" and "motion_cues" in str(refused.value)
    kept = call_tool(svc, "motion_sound_events", {"composition": cid, "expected_revision": 2, "lufs": -16})
    assert kept["changed"] and kept["events"] == 2, "loudness and music can still be changed"
    with pytest.raises(LumiereError, match="past the end"):
        call_tool(svc, "motion_cues", {"composition": cid, "action": "set", "expected_revision": 3, "cues": [{"t": 9, "visual": "late"}]})
    with pytest.raises(LumiereError):
        call_tool(svc, "motion_cues", {"composition": cid, "action": "set", "expected_revision": 3, "cues": [{"t": 1, "visual": "xx", "sound": "explosion"}]})
    cleared = call_tool(svc, "motion_cues", {"composition": cid, "action": "clear", "expected_revision": 3})
    assert cleared["cues"] == [] and call_tool(svc, "motion_cues", {"composition": cid})["cues"] == []
    ok = call_tool(svc, "motion_sound_events", {"composition": cid, "action": "add", "expected_revision": 4, "events": [{"t": 3.5, "role": "clic"}]})
    assert ok["events"] == 1, "without cues the events are free again"
    svc.stop()


def test_treatment_facts_and_cues_are_kept_next_to_the_composition(tmp_path):
    svc = make_services(tmp_path)
    store = HtmlMotion(svc.config.data_dir)
    cid = store.save(html=PAGE, name="Kept", width=640, height=360, fps=30, duration=8)["id"]
    assert call_tool(svc, "motion_treatment", {"composition": cid})["treatment"] is None
    with pytest.raises(Exception):
        call_tool(svc, "motion_treatment", {"composition": cid, "action": "set", "treatment": {**treatment(), "structures": []}})
    saved = call_tool(svc, "motion_treatment", {"composition": cid, "action": "set", "treatment": treatment()})
    assert saved["treatment"]["rev"] == 1 and call_tool(svc, "motion_treatment", {"composition": cid})["check"]["ok"]
    assert call_tool(svc, "motion_treatment", {"composition": cid, "action": "set", "treatment": treatment(chosen=0)})["treatment"]["rev"] == 2
    dry = call_tool(svc, "motion_treatment", {"action": "check", "duration": 8, "treatment": treatment(shots=[{"t": 9, "what": "after the end", "why": "It should not be here."}])})
    assert not dry["check"]["ok"]
    draft = call_tool(svc, "motion_treatment", {"action": "draft", "card": "pixel-rpg", "duration": 8})
    assert draft["treatment"]["draft"] and draft["check"]["ok"]
    assert call_tool(svc, "motion_facts", {"composition": cid})["facts"] is None
    facts = call_tool(svc, "motion_facts", {"composition": cid, "action": "set", "content": "- 57 seconds: the telegram. Source: the archive.\n- 59 seconds: the negatives."})
    assert facts["numbers"] == ["57", "59"]
    assert call_tool(svc, "motion_facts", {"composition": cid, "action": "set", "content": ""})["facts"] is None
    svc.stop()


def test_a_page_cannot_be_started_from_a_card_without_a_treatment(tmp_path):
    svc = make_services(tmp_path)
    with pytest.raises(LumiereError) as refused:
        call_tool(svc, "motion_from_style", {"card": "pixel-rpg"})
    assert refused.value.code == "treatment_required" and "three structures" in str(refused.value)
    with pytest.raises(LumiereError) as unusable:
        call_tool(svc, "motion_from_style", {"card": "pixel-rpg", "treatment": {**treatment(), "shots": [{"t": 0, "what": "One shot", "why": "A reason to be."}], "cue_map": [{"t": 99, "visual": "late", "sound": None}]}})
    assert "past the end" in str(unusable.value)
    with pytest.raises(NotFound, match="No style card"):
        call_tool(svc, "motion_from_style", {"card": "no-such-style", "draft_treatment": True})
    styles = call_tool(svc, "motion_styles", {"query": "archive loupe"})
    assert styles["items"][0]["slug"] == "archive-lighttable" and styles["count"] >= 1
    card = call_tool(svc, "motion_styles", {"action": "get", "id": "lemo-opuscar/pixel-rpg", "section": "color"})
    assert card["section"]["key"] == "color" and card["license"].startswith("MIT") and "LemoLab" in card["attribution"]
    with pytest.raises(LumiereError, match="no section"):
        call_tool(svc, "motion_styles", {"action": "get", "id": "pixel-rpg", "section": "nope"})
    svc.stop()


# ------------------------------------------------------------------------------------------------ the checks, on the real page
@BROWSER
def test_cuecheck_measures_each_cue_against_the_frame_where_the_picture_hits(tmp_path):
    svc = make_services(tmp_path)
    store = HtmlMotion(svc.config.data_dir)
    cid = store.save(html=PAGE, name="Hits", width=640, height=360, fps=30, duration=3)["id"]
    call_tool(svc, "motion_cues", {"composition": cid, "action": "set", "expected_revision": 1, "cues": CUES})
    good = call_tool(svc, "motion_cuecheck", {"composition": cid})
    assert good["ok"], good
    assert [c["status"] for c in good["cues"]] == ["ok", "ok"] and all(abs(c["offset_ms"]) <= 34 for c in good["cues"])
    assert good["scanned_frames"] == 91 and good["unmapped_hits"] == []
    # a sound that lands after the picture, a cue on nothing, and a hit that has no cue
    call_tool(svc, "motion_cues", {"composition": cid, "action": "set", "expected_revision": 2,
                                   "cues": [{"t": 1.15, "visual": "white flash", "sound": "hit"}, {"t": 0.5, "visual": "nothing here", "sound": "tap"}]})
    bad = call_tool(svc, "motion_cuecheck", {"composition": cid})
    by = {c["id"]: c for c in bad["cues"]}
    assert not bad["ok"] and by["c1"]["status"] == "no_hit" and "fix" in bad
    assert by["c2"]["status"] == "sound_late" and by["c2"]["suggest_t"] == pytest.approx(1.0, abs=0.04) and by["c2"]["offset_ms"] < -100
    assert any(abs(u["t"] - 2.0) < 0.05 for u in bad["unmapped_hits"]), "the ground change at 2 s has no cue"
    assert by["c1"]["suggest_t"] == pytest.approx(2.0, abs=0.05) and any(u.get("cue") == "c1" for u in bad["unmapped_hits"]), "the hit with no cue is offered to the cue with no hit"
    # retime moves the cue that really is off to the frame where the picture hits
    fixed = call_tool(svc, "motion_cuecheck", {"composition": cid, "retime": True, "expected_revision": 3})
    assert fixed["retimed"]["cues"] == ["c2"]
    moved = {c["id"]: c["t"] for c in call_tool(svc, "motion_cues", {"composition": cid})["cues"]}
    assert abs(moved["c2"] - 1.0) < 0.05
    call_tool(svc, "motion_cues", {"composition": cid, "action": "set", "expected_revision": 4,
                                   "cues": [{"t": 0.9, "visual": "white flash", "sound": "hit"}, {"t": 2.0, "visual": "ground turns teal", "sound": "tap"}]})
    early = call_tool(svc, "motion_cuecheck", {"composition": cid})
    assert early["cues"][0]["status"] == "sound_early" and early["cues"][0]["offset_ms"] > 60 and early["cues"][1]["status"] == "ok"
    only = call_tool(svc, "motion_cuecheck", {"composition": cid, "scan": False})
    assert only["scanned_frames"] is None and only["checked_frames"] < 91
    with pytest.raises(LumiereError, match="no cues"):
        other = store.save(html=PAGE, name="None", width=640, height=360, fps=30, duration=3)["id"]
        call_tool(svc, "motion_cuecheck", {"composition": other})
    svc.stop()


@BROWSER
def test_readcheck_finds_texts_that_vanish_too_soon_or_are_never_whole(tmp_path):
    svc = make_services(tmp_path)
    store = HtmlMotion(svc.config.data_dir)
    cid = store.save(html=PAGE, name="Reading", width=640, height=360, fps=30, duration=3)["id"]
    result = call_tool(svc, "motion_readcheck", {"composition": cid})
    assert result["source"] == "dom" and not result["ok"]
    short = {r["text"][:12]: r for r in result["too_short"]}
    assert "This sentenc" in short and short["This sentenc"]["need_s"] > 5 and short["This sentenc"]["dwell_s"] < 1
    assert "Hi" not in short, "two seconds is enough for a two-letter text"
    assert any(n["text"] == "Overflowing text" and "frame" in n["reason"] for n in result["never_readable"])
    hi = next(r for r in result["texts"] if r["text"] == "Hi")
    assert 2.2 < hi["dwell_s"] < 2.5 and hi["ok"]
    custom = PAGE.replace("seek(0);</script>", "window.TEXTS=t=>t>=1&&t<2?[{id:'x',text:'Hello',x0:10,y0:10,x1:90,y1:40}]:[];seek(0);</script>")
    cid2 = store.save(html=custom, name="Custom", width=640, height=360, fps=30, duration=3)["id"]
    own = call_tool(svc, "motion_readcheck", {"composition": cid2})
    assert own["source"] == "TEXTS" and own["texts"][0]["dwell_s"] == pytest.approx(1.0, abs=0.04) and own["texts"][0]["need_s"] == pytest.approx(1.833, abs=0.01)
    assert own["too_short"][0]["short_by_s"] > 0.7
    svc.stop()


@BROWSER
def test_review_modes_show_the_motion_and_the_detail(tmp_path):
    svc = make_services(tmp_path)
    cid = HtmlMotion(svc.config.data_dir).save(html=PAGE, name="Review", width=640, height=360, fps=30, duration=3)["id"]
    plain = call_tool(svc, "html_motion_storyboard", {"composition": cid, "times": [0, 1, 2, 3], "show": False})
    assert "mode" not in plain and plain["width"] == 1024, "the default sheet is the one that always was"
    strip = call_tool(svc, "html_motion_storyboard", {"composition": cid, "mode": "strip", "span": [0.9, 1.5], "show": True})
    assert strip["mode"] == "strip" and len(strip["frames"]) == 19 and strip["every_frames"] == 1 and strip["times"][1] == pytest.approx(0.9333, abs=1e-3)
    sheet = Image.open(BytesIO(__import__("base64").b64decode(strip["_image"]["data"])))
    assert sheet.width == 8 * 240 and sheet.height > 3 * 135
    long_strip = call_tool(svc, "html_motion_storyboard", {"composition": cid, "mode": "strip", "show": False})
    assert len(long_strip["frames"]) <= 48 and long_strip["every_frames"] == 2
    point = call_tool(svc, "html_motion_storyboard", {"composition": cid, "mode": "crop-at", "times": [0.5, 1.2], "x": 100, "y": 60, "size": [160, 90], "scale": 3, "show": False})
    assert point["crops"][0] == {"at_s": 0.5, "x0": 20, "y0": 15, "x1": 180, "y1": 105} and point["width"] == 2 * 480
    followed = call_tool(svc, "html_motion_storyboard", {"composition": cid, "mode": "crop-at", "times": [0.5], "selector": "#a", "show": False})
    assert followed["crops"][0]["x0"] <= 20 and followed["crops"][0]["x1"] > 20
    with pytest.raises(LumiereError, match="No element matches"):
        call_tool(svc, "html_motion_storyboard", {"composition": cid, "mode": "crop-at", "times": [0.5], "selector": "#nothing", "show": False})
    with pytest.raises(LumiereError, match="point"):
        call_tool(svc, "html_motion_storyboard", {"composition": cid, "mode": "crop-at", "times": [0.5], "show": False})
    svc.stop()


@BROWSER
def test_a_film_made_from_an_imported_card_passes_cuecheck_and_readcheck(tmp_path):
    svc = make_services(tmp_path)
    plan = treatment(duration=12, card="lemo-opuscar/archive-lighttable",
                     shots=[{"t": 0, "what": "The light table, empty", "why": "Sets the cool ground before anything is placed on it.", "text": "Two seconds short"},
                            {"t": 4, "what": "The telegram arrives", "why": "The claim the whole film tests.", "text": "The telegram says 57"},
                            {"t": 8, "what": "The negative says otherwise", "why": "The reveal; it needs the longest shot.", "text": "The glass says 59"}],
                     cue_map=[{"t": 0.3, "visual": "title settles", "sound": "tap"}, {"t": 4, "visual": "telegram lands", "sound": "hit"},
                              {"t": 8, "visual": "negative flips", "sound": "whoosh"}])
    made = call_tool(svc, "motion_from_style", {"card": "archive-lighttable", "name": "Two seconds short", "duration": 12, "treatment": plan})
    assert made["card"] == "lemo-opuscar/archive-lighttable" and "LemoLab" in made["attribution"] and made["events"] == 3 and not made["draft_treatment"]
    cid = made["id"]
    store = HtmlMotion(svc.config.data_dir)
    html = store.source(store.get(cid))
    assert "<!-- Style card: Archive Light Table" in html and "lumiere-cues" in html and "window.seek" in html
    assert call_tool(svc, "motion_treatment", {"composition": cid})["treatment"]["chosen"] == 1
    cues = call_tool(svc, "motion_cues", {"composition": cid})
    assert [c["id"] for c in cues["cues"]] == ["c1", "c2", "c3"] and cues["events"] == 3
    cut = call_tool(svc, "motion_cuecheck", {"composition": cid})
    assert cut["ok"], cut
    read = call_tool(svc, "motion_readcheck", {"composition": cid})
    assert read["ok"], read
    assert [r["text"] for r in read["texts"]] == ["Two seconds short", "The telegram says 57", "The glass says 59"]
    call_tool(svc, "motion_facts", {"composition": cid, "action": "set", "content": "- 57 seconds: the telegram.\n- 59 seconds: the glass negatives."})
    assert call_tool(svc, "motion_facts", {"composition": cid, "action": "check"})["ok"]
    call_tool(svc, "motion_facts", {"composition": cid, "action": "set", "content": "- 57 seconds: the telegram."})
    unsourced = call_tool(svc, "motion_facts", {"composition": cid, "action": "check"})
    assert not unsourced["ok"] and unsourced["unsourced"][0]["number"] == "59"
    drafted = call_tool(svc, "motion_from_style", {"card": "pixel-rpg", "duration": 12, "draft_treatment": True, "caption": "Hola"})
    assert drafted["draft_treatment"] and call_tool(svc, "motion_cuecheck", {"composition": drafted["id"]})["ok"]
    assert call_tool(svc, "motion_readcheck", {"composition": drafted["id"]})["ok"]
    svc.stop()


@BROWSER
def test_the_routes_serve_the_same_tools_to_the_screen(tmp_path):
    svc = make_services(tmp_path)
    with TestClient(create_app(svc.config, services=svc), base_url="http://127.0.0.1") as client:
        found = client.get("/api/html-motion/styles", params={"q": "pixel"}).json()
        assert found["items"][0]["slug"] == "pixel-rpg"
        assert client.get("/api/html-motion/styles/lemo-opuscar/pixel-rpg", params={"section": "color"}).json()["section"]["key"] == "color"
        draft = client.post("/api/html-motion/from-style", json={"card": "pixel-rpg", "duration": 8, "draft_treatment": True}).json()
        cid = draft["id"]
        assert client.get(f"/api/html-motion/{cid}/treatment").json()["treatment"]["draft"]
        assert client.get(f"/api/html-motion/{cid}/cues").json()["cues"]
        assert client.post(f"/api/html-motion/{cid}/cuecheck", json={"composition": cid}).json()["ok"]
        assert client.post(f"/api/html-motion/{cid}/readcheck", json={"composition": cid}).json()["ok"]
        assert client.get(f"/api/html-motion/{cid}/facts").json()["facts"] is None
        sheet = client.post(f"/api/html-motion/{cid}/storyboard", json={"composition": cid, "mode": "strip", "span": [0, 1]}).json()
        assert sheet["mode"] == "strip" and client.get(sheet["url"]).status_code == 200
        assert client.post(f"/api/html-motion/{cid}/treatment", json={"composition": cid, "action": "draft", "card": "hud"}).json()["treatment"]["draft"]
    svc.stop()
