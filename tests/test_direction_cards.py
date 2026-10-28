"""Direction style cards: the packaged libraries, their sections and partition, variants, search and import."""
from __future__ import annotations

from pathlib import Path

import pytest

from prosperos_hoard import db, style_cards as sc
from prosperos_hoard.store import Store

FAKE_STYLE = """# Chalk Board — Style Prompt

> A chalk board that writes itself.

## 1. Essence, and what it is not

- Chalk on slate. Not a whiteboard.

## 2. Materials & rendering

Slate texture with #1F2A24 and #F1F1E8 dust.

## 3. Colour logic

Slate #1F2A24, chalk #F1F1E8, one signal #FFD166.

## 7. Sound palette

Chalk scratches.

## 11. Variation space

Free.
"""

FAKE_PROMPT = """# 粉笔字动画 Chalk Lettering

**原片**：Chalk「粉笔」 · 1920×1080 · 30 fps · 10 秒 · AI 评审终评 7.00 / 10

```text
# Style
粉笔字动画 Chalk Lettering

# Output (fixed)
- 1920×1080, 30 fps, exactly 10.000 s.

# Suggested technical route
Canvas.

# Creative seed
Chalk words appear. palette #101010 #F5F5F5.

You are the director: you may change the concept.

# Signature features and key techniques (the result is judged against these)
Chalk dust.

# How to work
1. Treatment.
```
"""


def test_the_packaged_libraries_are_complete_and_carry_their_licence():
    library = sc.load_library()
    cards = library["cards"]
    assert sum(c["source"] == "lemo-opuscar" for c in cards) == 59 and sum(c["source"] == "mg-styles-15" for c in cards) == 15
    ids = [sc.card_id(c) for c in cards]
    assert len(ids) == len(set(ids))
    for card in cards:
        assert card["license"].startswith("MIT") and "github.com" in card["attribution"] and card["source_url"].startswith("https://github.com/")
        assert "films" in card["license"] or "prompts" in card["license"], "the licence says what is not included"
        assert card["sections"], card["slug"]
        if card["source"] == "lemo-opuscar":
            assert {"essence", "materials", "color", "type", "sound", "pitfalls", "variation"} <= set(card["sections"]), card["slug"]
            assert card["variants"]["safe"]["sections"] and "essence" in card["variants"]["essence"]["sections"]
            assert card["category"] and card["line"]
        else:
            assert set(card["sections"]) == {"output", "route", "seed", "signature"}, card["slug"]
            assert card["variants"]["essence"]["sections"] == ["signature"]
    assert library["common"]["mg-styles-15"]["rules"], "the common blocks of the second library are kept once"
    # the cards are text: no film, still or poster travels with them
    assert not list(sc.LIBRARY_DIR.glob("*.mp4")) and not list(sc.LIBRARY_DIR.glob("*.jpg"))
    assert sc.find("archive-lighttable")["name"] == "Archive Light Table" and sc.find("lemo-opuscar/pixel-rpg")
    assert sc.find("Flat / Vector 2D Motion Graphics")["source"] == "mg-styles-15"


def test_search_finds_a_style_by_what_it_is_for():
    assert sc.search("archive loupe")[0]["slug"] == "archive-lighttable"
    names = [c["slug"] for c in sc.search("", source="mg-styles-15")]
    assert len(names) == 15
    assert sc.search("evidence", category="Evidence")[0]["category"] == "Evidence Films"
    assert sc.search("zzzznotaword") == []
    assert sc.brief(sc.find("pixel-rpg"))["sections"][0] == "essence"


def test_the_store_seeds_the_libraries_read_only_and_keeps_the_old_list_short(tmp_path):
    store = Store(tmp_path / "d")
    older = {c["name"] for c in store.list_style_presets()}
    assert {"Neón nocturno", "VHS terror"} <= older and len(older) < 20, "the pickers that existed keep their list"
    everything = store.list_style_presets(library=True)
    assert len(everything) == len(older) + 74
    assert len(store.list_style_presets(library="lemo-opuscar")) == len(older) + 59
    card = next(c for c in everything if c.get("slug") == "archive-lighttable")
    assert card["is_builtin"] == 1 and card["source"] == "lemo-opuscar" and card["sections"]["essence"]["text"] and card["variants"]["safe"]["sections"]
    assert card["meta"]["line"] and card["meta"]["swatches"]
    with pytest.raises(ValueError, match="built-in"):
        store.update_style_preset(card["id"], quality=1)
    with pytest.raises(ValueError, match="built-in"):
        store.delete_style_preset(card["id"])
    first = [c["id"] for c in everything]
    db._local.conns = {}
    again = Store(tmp_path / "d")
    assert [c["id"] for c in again.list_style_presets(library=True)] == first, "a second start changes nothing"
    found = again.search_style_cards("dive loupe archive")
    assert found[0]["slug"] == "archive-lighttable"


def test_a_variant_keeps_its_licence_and_asks_before_changing_the_character(client):
    c, _app, _ = client
    ask = lambda **body: c.post("/api/agent/studio_style_cards", json=body)
    searched = ask(action="search", query="archive loupe").json()
    assert searched["items"][0]["slug"] == "archive-lighttable" and searched["items"][0]["section_names"][0] == "essence"
    full = ask(action="get", id="lemo-opuscar/archive-lighttable").json()
    assert full["license"].startswith("MIT") and "LemoLab" in full["attribution"] and set(full["variants"]) == {"safe", "character", "essence", "free"}
    one = ask(action="get", id="archive-lighttable", section="sound").json()
    assert one["section"]["key"] == "sound" and "hum" in one["section"]["text"] and "sections" not in one
    assert ask(action="get", id="archive-lighttable", section="nope").status_code == 400

    safe = ask(action="create", name="Archive, warm", from_card="archive-lighttable", changes={"color": "Warm paper ground #F2E6D0, ink #2B2118, one signal #C8442B."})
    assert safe.status_code == 200, safe.text
    mine = safe.json()
    assert mine["based_on"] and not mine["builtin"] and mine["attribution"] == full["attribution"] and mine["changes"]["safe"] == ["color"]
    assert mine["changes"]["changes_character"] == [] and mine["changes"]["breaks_essence"] == []
    stored = ask(action="get", id=mine["id"]).json()
    assert "Warm paper" in stored["sections"]["color"]["text"] and "Light comes from below" in stored["sections"]["essence"]["text"]

    character = ask(action="create", name="Archive, plastic", from_card="archive-lighttable", changes={"materials": "Everything is moulded plastic."})
    assert character.status_code == 400 and "character" in character.text and "accept_character_change" in character.text
    accepted = ask(action="create", name="Archive, plastic", from_card="archive-lighttable", changes={"materials": "Everything is moulded plastic."},
                   accept_character_change=True)
    assert accepted.status_code == 200 and accepted.json()["changes"]["changes_character"] == ["materials"]
    essence = ask(action="create", name="Not archive", from_card="archive-lighttable", changes={"essence": "A neon street."})
    assert essence.status_code == 400 and "different style" in essence.text
    assert ask(action="create", name="Bad", from_card="archive-lighttable", changes={"mood": "x"}).status_code == 400
    assert ask(action="create", name="Bad", changes={"color": "x"}).status_code == 400
    # the variants are in the ordinary list (they are the user's), the library cards are not
    listed = ask(action="list").json()["items"]
    assert any(i["name"] == "Archive, warm" for i in listed) and not any(i.get("slug") == "pixel-rpg" and i["source"] for i in listed if not i["name"].startswith("Archive"))
    assert len(ask(action="list", library=True).json()["items"]) > 70
    edited = ask(action="update", id=mine["id"], changes={"sound": "Only room tone."}).json()
    assert edited["changes"]["safe"] == ["sound"]
    assert ask(action="delete", id=mine["id"]).json()["deleted"] == mine["id"]
    assert c.get("/api/style-presets", params={"q": "pixel"}).json()["items"][0]["slug"] == "pixel-rpg"
    assert c.get("/api/style-presets/lemo-opuscar/pixel-rpg").status_code in (200, 404)
    assert c.get("/api/style-presets/pixel-rpg", params={"section": "color"}).json()["section"]["key"] == "color"


def test_cards_are_imported_from_a_checkout_of_either_library(tmp_path, store):
    lemo = tmp_path / "lemo"
    (lemo / "styles" / "chalk-board").mkdir(parents=True)
    (lemo / "styles" / "_template").mkdir()
    (lemo / "styles" / "_template" / "STYLE.md").write_text("# template", encoding="utf-8")
    (lemo / "styles" / "chalk-board" / "STYLE.md").write_text(FAKE_STYLE, encoding="utf-8")
    (lemo / "styles" / "chalk-board" / "style.json").write_text('{"slug":"chalk-board","en":"Chalk Board","cn":"粉笔板","category_en":"Hand-drawn","line":"A board writes itself."}', encoding="utf-8")
    (lemo / "styles" / "archive-lighttable").mkdir()
    (lemo / "styles" / "archive-lighttable" / "STYLE.md").write_text(FAKE_STYLE, encoding="utf-8")
    parsed = sc.parse_lemo_folder(lemo)
    assert [p["slug"] for p in parsed] == ["archive-lighttable", "chalk-board"]
    chalk = parsed[1]
    assert chalk["name"] == "Chalk Board" and chalk["sections"]["color"]["text"].startswith("Slate") and chalk["meta"]["swatches"][:2] == ["#1F2A24", "#F1F1E8"]
    assert chalk["variants"]["safe"]["sections"] == ["color", "sound"] and chalk["variants"]["essence"]["sections"] == ["essence"]
    done = store.import_direction_cards(parsed)
    assert done["added"] == ["chalk-board"] and done["skipped"] == ["archive-lighttable"], "a packaged style is not copied again"
    assert store.import_direction_cards(parsed)["updated"] == ["chalk-board"]
    mine = next(c for c in store.list_style_presets(library=True) if c.get("slug") == "chalk-board")
    assert mine["is_builtin"] == 0 and mine["source"] == "lemo-opuscar" and mine["sections"]["essence"]["text"].startswith("- Chalk")
    store.delete_style_preset(mine["id"])

    mg = tmp_path / "mg" / "prompts"
    mg.mkdir(parents=True)
    (mg / "30-chalk.md").write_text(FAKE_PROMPT, encoding="utf-8")
    cards, common = sc.parse_mg_folder(tmp_path / "mg")
    assert cards[0]["slug"] == "chalk" and cards[0]["name"] == "Chalk Lettering" and cards[0]["name_alt"] == "粉笔字动画"
    assert cards[0]["meta"]["size"] == "1920×1080" and cards[0]["meta"]["fps"] == 30 and cards[0]["meta"]["dur"] == 10
    assert "You are the director" not in cards[0]["sections"]["seed"]["text"] and cards[0]["meta"]["swatches"] == ["#101010", "#F5F5F5"]
    assert common["how"].startswith("1. Treatment")


def test_a_card_can_be_used_in_a_graphic_shot_by_its_slug(store):
    from prosperos_hoard import graphic_shots
    assert graphic_shots.style_card(store, None, "pixel-rpg")["source"] == "lemo-opuscar"
    assert graphic_shots.style_card(store, None, "mg-styles-15/hud")["slug"] == "hud"
    assert graphic_shots.style_card(store, None, "VHS terror")["name"] == "VHS terror"
