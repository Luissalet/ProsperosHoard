import json

from prosperos_hoard.editing.analysis import script as sc

SCRIPT = """# Título

**Tema:** algo que no se lee

---

## Segmento 1
Hay un libro de Terry Pratchett que tiene dos autores.

## Segmento 2
Se llama El pueblo de la alfombra.

## Segmento 3
Y lo más raro es que los dos son la misma persona.
"""


def words_from(spoken):
    out, t = [], 0
    for i, w in enumerate(spoken.split()):
        out.append({"id": f"w{i + 1}", "t0": t, "t1": t + 300, "text": w})
        t += 350
    return out


def test_parse_markdown_plan_and_paragraphs():
    segs = sc.parse_script(SCRIPT)
    assert [s.title for s in segs] == ["Segmento 1", "Segmento 2", "Segmento 3"]
    assert segs[0].tokens[:3] == ["hay", "un", "libro"]
    plan = json.dumps({"segments": [{"title": "A", "script": "Hola mundo"}, {"title": "B", "script": "Adiós"}]})
    assert [s.title for s in sc.parse_script(plan)] == ["A", "B"]
    assert len(sc.parse_script("Primer párrafo aquí.\n\nSegundo párrafo.")) == 2


def test_assemble_picks_the_last_complete_take_and_skips_noise():
    spoken = ("hay un libro de terry eh perdón " +
              "hay un libro de Terry Pratchett que tiene dos autores " +
              "vale otra vez " +
              "se llama el pueblo de la alfombra " +
              "y lo más raro es que los dos son la misma persona")
    words = words_from(spoken)
    res = sc.assemble(sc.parse_script(SCRIPT), words)
    segs = res["segments"]
    assert [s["segment"] for s in segs] == [1, 2, 3] and not res["missing"]
    first = segs[0]
    assert first["coverage"] == 1.0
    assert first["src_in"] >= words[7]["t0"] - 120  # the second take, not the stumble
    assert segs[2]["src_out"] >= words[-1]["t1"]


def test_best_mode_and_missing_segment():
    words = words_from("hay un libro de terry pratchett que tiene dos autores se llama el pueblo de la")
    res = sc.assemble(sc.parse_script(SCRIPT), words, take="best")
    assert [m["segment"] for m in res["missing"]] == [3]
    assert res["segments"][1]["coverage"] >= 0.6


def test_failed_global_model_transport_is_not_replayed_or_reselected():
    from prosperos_hoard.editing import generate

    calls = []

    class Flaky:
        def chat(self, messages, **kw):
            calls.append(kw)
            if len(calls) == 1:
                raise RuntimeError("HTTP 404 from a server picked while the usual one was busy")
            return type("R", (), {"text": "hola", "model": "m"})()

    forgotten = []

    class Svc:
        link_sync = Flaky()

        def forget_model_probes(self):
            forgotten.append(1)

    import pytest
    from prosperos_hoard.editing.errors import ModelUnavailable
    with pytest.raises(ModelUnavailable, match='HTTP 404'):
        generate.chat_text(Svc(), [{"role": "user", "content": "x"}])
    assert len(calls) == 1 and forgotten == []
