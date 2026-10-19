"""Highlights read by the local model: the prompt/answer plumbing, the merge with the sound and motion signals, and every way it can
fall back to the signals-only list."""

import pytest

from conftest import FakeLink, fake_transcript, make_services, needs_ffmpeg
from prosperos_hoard.editing import commands
from prosperos_hoard.editing import media as media_store
from prosperos_hoard.editing.analysis import moments as mo

# 8 sentences of 1.4 s over the 12 s test talk (tone with silences at 3-5 s and 8-9 s)
SENTENCES = ["Hola a todos.", "Hoy hablamos de algo importante.", "Primero un poco de contexto.", "Esto viene de lejos.",
             "Pero atención a lo que sigue.", "El secreto es no rendirse nunca.", "Y eso lo cambia todo.", "Gracias por ver el vídeo."]


def talk_words():
    words = []
    for i, sentence in enumerate(SENTENCES):
        parts = sentence.split()
        t0 = 100 + i * 1450
        step = 1300 // len(parts)
        for k, w in enumerate(parts):
            words.append((t0 + k * step, t0 + k * step + step - 40, w))
    return words


def test_sentences_chunks_and_the_answer_format():
    from prosperos_hoard.editing.analysis.moments import chunks_of, parse_answer, sentences_of

    words = [{"id": f"w{i}", "t0": i * 300, "t1": i * 300 + 250, "text": t} for i, t in enumerate(["Uno", "dos.", "Tres", "cuatro", "cinco?"])]
    sents = sentences_of({"words": words})
    assert [s["text"] for s in sents] == ["Uno dos.", "Tres cuatro cinco?"] and sents[1]["t0"] == 600
    # a pause ends a sentence too, and the transcript's own segments win when it has them
    paused = sentences_of({"words": [{"t0": 0, "t1": 200, "text": "a"}, {"t0": 1500, "t1": 1700, "text": "b"}]})
    assert [s["text"] for s in paused] == ["a", "b"]
    assert sentences_of({"segments": [{"t0": 0, "t1": 900, "text": "Frase."}], "words": words})[0]["text"] == "Frase."
    many = [{"i": i, "t0": i * 1000, "t1": i * 1000 + 900, "text": "palabra " * 20} for i in range(30)]
    chunks = chunks_of(many, limit=1500)
    assert len(chunks) > 3 and all(c for c in chunks)
    assert chunks[1][0]["i"] == chunks[0][-1]["i"] - 1  # chunks overlap by two sentences
    assert {s["i"] for c in chunks for s in c} == set(range(30))
    got = parse_answer("5-7|5|punchline|remate final\n**2 - 3 | 3 | Hook | gancho**\n9|4|tip|consejo\nblah\n40-41|5|hook|fuera de rango", set(range(10)))
    assert [(g["first"], g["last"], g["strength"], g["kind"]) for g in got] == [(5, 7, 5, "punchline"), (2, 3, 3, "hook"), (9, 9, 4, "tip")]
    assert parse_answer("-") == [] and parse_answer("") == []
    with pytest.raises(ValueError):
        parse_answer("Lo siento, no puedo ayudar con eso.")
    prompt = mo.prompt_for(sents, n=2)
    assert "at most 2 lines" in prompt[0]["content"].lower() or "At most 2 lines" in prompt[0]["content"]
    assert "0\t[0:00.000]\tUno dos." in prompt[1]["content"]


@pytest.fixture
def setup(tmp_path, media_dir):
    def make(responder=None, available=True):
        link = FakeLink(responder=responder, available=available)
        svc = make_services(tmp_path, link=link)
        svc.start()
        talk = media_store.import_path(svc, str(media_dir / "talk.mp4"))["id"]
        fake_transcript(svc, talk, talk_words())
        return svc, link, talk
    made = []

    def factory(*a, **kw):
        out = make(*a, **kw)
        made.append(out[0])
        return out
    yield factory
    for svc in made:
        svc.stop()


@needs_ffmpeg
def test_model_moments_lead_the_merged_ranking_with_reasons(setup):
    svc, link, talk = setup(lambda messages: "5-6|5|punchline|el remate: no rendirse\n1-2|2|hook|saludo flojo")
    base = commands.highlights(svc, talk, count=3, length_ms=3000, min_gap_ms=3000)
    assert "model" not in base and not link.calls  # the default never touches the model
    res = commands.highlights(svc, talk, count=3, length_ms=3000, min_gap_ms=3000, mode="model")
    assert res["model"]["used"] and res["model"]["moments"] == 2 and res["model"]["model"] == "fake-model" and "model" in res["signals"]
    top = res["highlights"][0]
    # sentences 5-6 run from 7.35 s to 10.1 s; the clip starts a hair before the first and ends after the last
    assert 7000 <= top["start_ms"] <= 7350 and top["end_ms"] >= 10100
    assert top["reasons"][0] == "punchline" and top["source"] in ("model", "both") and "remate" in top["why"] and top["range"]
    assert 0.65 <= top["score"] <= 1.0  # 65% the model's strength (5 of 5) plus up to 35% from how eventful its seconds are
    weak = next(h for h in res["highlights"] if h["start_ms"] < 2000 and h["source"] != "signals")
    assert weak["score"] < top["score"]
    # no two results overlap, and the list is ranked
    spans = [(h["start_ms"], h["end_ms"]) for h in res["highlights"]]
    assert all(a[1] <= b[0] or b[1] <= a[0] for i, a in enumerate(spans) for b in spans[i + 1:])
    assert [h["score"] for h in res["highlights"]] == sorted((h["score"] for h in res["highlights"]), reverse=True)
    # the prompt carried the timestamps and sentence numbers
    user = link.calls[0][1]["content"]
    assert "5\t[0:07.350]\tEl secreto es no rendirse nunca." in user
    # asking again (or for another count) reads from the cache, not from the model
    n = len(link.calls)
    again = commands.highlights(svc, talk, count=2, length_ms=3000, min_gap_ms=3000, mode="model")
    assert len(link.calls) == n and again["model"]["cached"] and again["highlights"][0]["start_ms"] == top["start_ms"]


@needs_ffmpeg
def test_without_a_reachable_model_the_signals_list_comes_back(setup):
    svc, link, talk = setup(available=False)
    commands.highlights(svc, talk, count=3, length_ms=3000, min_gap_ms=3000)  # queues the motion analysis (an inline job here)
    base = commands.highlights(svc, talk, count=3, length_ms=3000, min_gap_ms=3000)
    res = commands.highlights(svc, talk, count=3, length_ms=3000, min_gap_ms=3000, mode="model")
    assert res["highlights"] == base["highlights"] and res["signals"] == base["signals"]
    assert res["model"]["used"] is False and "not reachable" in res["model"]["reason"]


@needs_ffmpeg
def test_a_model_that_ignores_the_format_or_finds_nothing(setup):
    svc, link, talk = setup(lambda messages: "Claro, aquí tienes los mejores momentos del vídeo: son muy buenos.")
    res = commands.highlights(svc, talk, count=2, length_ms=3000, min_gap_ms=3000, mode="model")
    assert res["model"]["used"] is False and "expected form" in res["model"]["reason"] and res["highlights"]
    svc2, link2, talk2 = setup(lambda messages: "-")
    res = commands.highlights(svc2, talk2, count=2, length_ms=3000, min_gap_ms=3000, mode="model")
    assert res["model"]["used"] is False and "no moment" in res["model"]["reason"]
    # out-of-range sentence numbers are dropped, the good lines stay
    svc3, link3, talk3 = setup(lambda messages: "90-95|5|hook|no existe\n3-4|4|thought|una idea completa")
    res = commands.highlights(svc3, talk3, count=2, length_ms=3000, min_gap_ms=3000, mode="model")
    assert res["model"]["moments"] == 1 and res["highlights"][0]["reasons"][0] == "thought"


@needs_ffmpeg
def test_model_mode_without_a_transcript_queues_one(tmp_path, media_dir):
    link = FakeLink(responder=lambda m: "1-2|5|hook|x")
    svc = make_services(tmp_path, link=link)
    svc.start()
    try:
        talk = media_store.import_path(svc, str(media_dir / "talk.mp4"))["id"]
        res = commands.highlights(svc, talk, count=2, length_ms=3000, min_gap_ms=3000, mode="model")
        assert res["model"]["used"] is False and "no transcript" in res["model"]["reason"] and not link.calls
        assert "transcript" in media_store.get(svc, talk)["analysis"]  # inline jobs ran the (fake) transcription
        with pytest.raises(Exception, match="mode must be"):
            commands.highlights(svc, talk, mode="telepathy")
    finally:
        svc.stop()


@needs_ffmpeg
def test_the_tool_and_the_route_expose_the_mode(setup):
    from prosperos_hoard.editing.agent_tools import call_tool, tool_catalog

    svc, link, talk = setup(lambda messages: "5-6|5|punchline|remate")
    out = call_tool(svc, "highlights_find", {"media": talk, "count": 2, "length_s": 3, "mode": "model"})
    assert out["model"]["used"] and out["highlights"][0]["reasons"][0] == "punchline"
    schema = next(t for t in tool_catalog() if t["name"] == "highlights_find")["inputSchema"]
    assert schema["properties"]["mode"]["enum"] == ["signals", "model"]
