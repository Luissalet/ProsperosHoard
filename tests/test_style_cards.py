"""Style cards with graphic fields: the v12 migration, the built-in cards and the custom ones."""
from __future__ import annotations

import sqlite3

import pytest

from prosperos_hoard import db, motion_graphics as mg
from prosperos_hoard.store import BUILTIN_STYLE_PRESETS, Store

OLD_STYLE_PRESETS = """
CREATE TABLE style_presets (
    id TEXT PRIMARY KEY,
    project_id TEXT,
    name TEXT NOT NULL,
    prompt_prefix TEXT NOT NULL DEFAULT '',
    prompt_suffix TEXT NOT NULL DEFAULT '',
    negative TEXT NOT NULL DEFAULT '',
    defaults_json TEXT NOT NULL DEFAULT '{}',
    notes TEXT,
    is_builtin INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
"""


def test_an_old_database_gains_the_card_columns_and_keeps_its_custom_presets(tmp_path):
    folder = tmp_path / "old"
    folder.mkdir()
    raw = sqlite3.connect(folder / "prosperos.sqlite3")
    raw.executescript(db._SCHEMA)
    raw.executescript("DROP TABLE style_presets;" + OLD_STYLE_PRESETS)
    raw.execute("INSERT INTO schema_meta(key, value) VALUES ('version', '11')")
    raw.execute("INSERT INTO style_presets (id, name, prompt_prefix, defaults_json, is_builtin, created_at) VALUES "
                "('sp_mine', 'Mi estilo', 'a quiet harbour,', '{\"steps\": 20}', 0, '2026-01-01T00:00:00')")
    raw.execute("INSERT INTO style_presets (id, name, prompt_prefix, defaults_json, is_builtin, created_at) VALUES "
                "('sp_old', 'Studio portrait', 'old prefix', '{}', 1, '2026-01-01T00:00:00')")
    raw.commit()
    raw.close()
    assert "palette_json" not in {r[1] for r in sqlite3.connect(folder / "prosperos.sqlite3").execute("PRAGMA table_info(style_presets)")}

    store = Store(folder)
    columns = {r[1] for r in store.conn.execute("PRAGMA table_info(style_presets)")}
    assert {"technique", "palette_json", "motion_json", "signature_transition", "quality", "pitfalls", "typography_json"} <= columns
    assert store.conn.execute("SELECT value FROM schema_meta WHERE key='version'").fetchone()[0] == str(db.SCHEMA_VERSION)
    mine = store.get_style_preset("sp_mine")
    assert mine["prompt_prefix"] == "a quiet harbour," and mine["defaults"] == {"steps": 20}
    assert mine["palette"] == [] and mine["motion"] == {} and mine["quality"] == 0 and mine["signature_transition"] is None
    names = {p["name"] for p in store.list_style_presets()}
    assert {"Neón nocturno", "Papel recortado", "VHS terror", "Tipografía suiza", "Mi estilo"} <= names
    # an old built-in row is brought up to date, not duplicated
    assert [p["name"] for p in store.list_style_presets()].count("Studio portrait") == 1
    assert store.get_style_preset("sp_old")["prompt_prefix"] != "old prefix"


def test_a_second_start_changes_nothing(tmp_path):
    first = Store(tmp_path / "d")
    before = [(p["name"], p["palette"], p["quality"]) for p in first.list_style_presets()]
    db._local.conns = {}
    second = Store(tmp_path / "d")
    assert [(p["name"], p["palette"], p["quality"]) for p in second.list_style_presets()] == before


def test_the_four_music_video_cards_are_built_in_and_each_draws(store):
    cards = {p["name"]: p for p in store.list_style_presets()}
    for name in ("Neón nocturno", "Papel recortado", "VHS terror", "Tipografía suiza"):
        card = cards[name]
        assert card["is_builtin"] == 1
        assert len(card["palette"]) == 5 and all(c.startswith("#") for c in card["palette"])
        assert card["technique"] and card["pitfalls"]
        assert card["signature_transition"] in mg.TRANSITIONS
        assert 0 <= card["quality"] <= 3 and card["quality"] > 0
        look = mg.look_from_card(card)
        assert look["palette"] and look["transition"] == card["signature_transition"]
        spec = mg.normalise_graphic({"grammar": "title_card", "duration": 2, "data": {"title": "Prueba"}, "look": look})
        assert mg.determinism_check(spec, 360, 640)["deterministic"]
    assert len(BUILTIN_STYLE_PRESETS) >= 10


def test_custom_cards_can_be_created_changed_and_deleted_but_built_ins_are_read_only(store, project):
    fields = mg.clean_card_fields({"technique": "tiza sobre pizarra", "palette": ["#101010", "#fafafa", "#ffcc00"], "quality": 2,
                                   "signature_transition": "wipe", "motion": {"easing": "out_cubic"}})
    card = store.create_style_preset("Tiza", **fields)
    assert card["is_builtin"] == 0 and card["palette"] == ["#101010", "#fafafa", "#ffcc00"] and card["quality"] == 2
    assert any(p["id"] == card["id"] for p in store.list_style_presets())
    with pytest.raises(ValueError, match="already a style card"):
        store.create_style_preset("tiza", **fields)
    with pytest.raises(ValueError, match="already a style card"):
        store.create_style_preset("VHS terror")
    changed = store.update_style_preset(card["id"], quality=3, pitfalls="no sobre vídeo claro", palette=["#000", "#fff"])
    assert changed["quality"] == 3 and changed["pitfalls"] == "no sobre vídeo claro" and changed["palette"] == ["#000", "#fff"]
    with pytest.raises(ValueError, match="unknown style card field"):
        store.update_style_preset(card["id"], colour="red")
    builtin = next(p for p in store.list_style_presets() if p["name"] == "VHS terror")
    with pytest.raises(ValueError, match="built-in"):
        store.update_style_preset(builtin["id"], quality=1)
    with pytest.raises(ValueError, match="built-in"):
        store.delete_style_preset(builtin["id"])
    store.delete_style_preset(card["id"])
    assert not any(p["id"] == card["id"] for p in store.list_style_presets())
    scoped = store.create_style_preset("Solo aquí", project_id=project["id"])
    assert any(p["id"] == scoped["id"] for p in store.list_style_presets(project["id"]))
    assert not any(p["id"] == scoped["id"] for p in store.list_style_presets("other"))
