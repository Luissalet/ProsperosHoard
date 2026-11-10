"""Parameters of HTML motion compositions ({{name}} + motion-props block) and variants made from data rows."""
import json

from fastapi.testclient import TestClient
import pytest

from conftest import FakeLink, make_config
from prosperos_hoard.editing import html_motion_props as hp
from prosperos_hoard.editing.errors import LumiereError
from prosperos_hoard.editing.html_motion import HtmlMotion
from prosperos_hoard.editing.main import create_app
from prosperos_hoard.editing.services import Services

TEMPLATE = """<!doctype html><html><meta charset='utf-8'>
<script type="application/json" id="motion-props">{"title": "Hola", "accent": "#b5583d", "count": 3, "loop": false}</script>
<style>h1{color:{{accent}}}</style><h1>{{ title }}</h1><p>{{city}}</p>
<script>const P = JSON.parse(document.getElementById('motion-props').textContent); window.seek = t => {};</script></html>"""


def test_props_lists_defaults_placeholders_and_types():
    info = hp.props(TEMPLATE)
    by = {p["name"]: p for p in info["props"]}
    assert set(by) == {"title", "accent", "count", "loop", "city"}
    assert by["accent"]["type"] == "color" and by["count"]["type"] == "number" and by["loop"]["type"] == "boolean"
    assert by["title"]["in_markup"] and by["title"]["in_script"] and not by["count"]["in_markup"]
    assert by["city"]["default"] is None and not by["city"]["in_script"]
    assert info["placeholders"] == ["accent", "title", "city"] and info["has_block"]


def test_apply_escapes_markup_types_csv_cells_and_rewrites_the_block():
    html, merged = hp.apply(TEMPLATE, {"title": "<b>Fin</b> & co", "city": "Sevilla", "count": "7", "loop": "sí"})
    assert "<h1>&lt;b&gt;Fin&lt;/b&gt; &amp; co</h1>" in html and "<p>Sevilla</p>" in html and "color:#b5583d" in html
    block = json.loads(html.split('id="motion-props">')[1].split("</script>")[0])
    assert block == {"title": "<b>Fin<\\/b> & co".replace("<\\/", "</"), "accent": "#b5583d", "count": 7, "loop": True, "city": "Sevilla"}
    assert merged["count"] == 7 and merged["loop"] is True
    assert "{{" not in html


def test_apply_refuses_unknown_and_missing_values():
    with pytest.raises(LumiereError, match="Unknown parameters: titel"):
        hp.apply(TEMPLATE, {"titel": "x", "city": "a"})
    with pytest.raises(LumiereError, match="No value for city"):
        hp.apply(TEMPLATE, {"title": "x"})
    with pytest.raises(LumiereError, match="not valid JSON"):
        hp.props('<script type="application/json" id="motion-props">{nope</script>')


def test_rows_from_csv_json_and_lists():
    assert hp.rows_from("city;title\nMadrid;Uno\nLeón;Dos") == [{"city": "Madrid", "title": "Uno"}, {"city": "León", "title": "Dos"}]
    assert hp.rows_from("city\nMadrid") == [{"city": "Madrid"}]
    assert hp.rows_from('[{"city": "Vigo"}]') == [{"city": "Vigo"}]
    assert hp.rows_from([{"city": "Lugo"}]) == [{"city": "Lugo"}]
    with pytest.raises(LumiereError):
        hp.rows_from("")
    with pytest.raises(LumiereError, match="At most"):
        hp.rows_from([{"city": str(i)} for i in range(hp.MAX_VARIANTS + 1)])


def test_variants_make_new_compositions_and_leave_the_template(tmp_path):
    svc = Services(make_config(tmp_path), link=FakeLink(available=False), inline_jobs=False)
    store = HtmlMotion(svc.config.data_dir)
    template = store.save(html=TEMPLATE, name="Cartel", width=320, height=180, fps=10, duration=1)
    out = hp.variants(store, svc, template["id"], "city,title\nMadrid,Uno\nBilbao,Dos", name_field="city")
    assert out["count"] == 2 and [v["name"] for v in out["variants"]] == ["Cartel · Madrid", "Cartel · Bilbao"]
    first = store.get(out["variants"][0]["composition"])
    assert "<p>Madrid</p>" in store.source(first) and first["width"] == 320 and first["duration"] == 1
    assert store.source(store.get(template["id"])) == TEMPLATE and store.get(template["id"])["revision"] == 1
    with pytest.raises(LumiereError):  # one bad row: nothing is saved
        hp.variants(store, svc, template["id"], [{"city": "Ok"}, {"cyty": "typo"}])
    assert len(store.list()["compositions"]) == 3
    queued = hp.variants(store, svc, template["id"], [{"_name": "rojo", "city": "Cádiz", "accent": "#ff0000"}], render=True)
    assert queued["variants"][0]["job"] and queued["variants"][0]["name"] == "Cartel · rojo" and queued["rendering"]
    svc.stop()


def test_api_and_tools(tmp_path):
    svc = Services(make_config(tmp_path), link=FakeLink(available=False), inline_jobs=False)
    app = create_app(svc.config, services=svc)
    with TestClient(app, base_url="http://127.0.0.1") as client:
        template = client.post("/api/html-motion", json={"html": TEMPLATE, "name": "Cartel", "width": 320, "height": 180, "fps": 10, "duration": 1}).json()
        info = client.get(f"/api/html-motion/{template['id']}/props").json()
        assert {p["name"] for p in info["props"]} == {"title", "accent", "count", "loop", "city"} and info["revision"] == 1
        made = client.post(f"/api/html-motion/{template['id']}/variants", json={"rows": [{"city": "Toledo"}, {"city": "Soria", "title": "Adiós"}]}).json()
        assert made["count"] == 2 and made["variants"][1]["values"]["title"] == "Adiós"
        bad = client.post(f"/api/html-motion/{template['id']}/variants", json={"rows": [{"nope": 1}]})
        assert bad.status_code >= 400
    from prosperos_hoard.editing.agent_tools import TOOLS
    names = {t.name for t in TOOLS}
    assert {"html_motion_props", "html_motion_variants"} <= names
    svc.stop()
