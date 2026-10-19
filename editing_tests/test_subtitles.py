"""Translated subtitles: one answer per cue, the original timing, repair of sloppy answers, staleness, exports and burn-in."""

import re
import subprocess

import pytest

from conftest import FakeLink, fake_transcript, make_services, needs_ffmpeg
from prosperos_hoard.editing import agent_tools
from prosperos_hoard.editing import media as media_store
from prosperos_hoard.editing import projects as store
from prosperos_hoard.editing import subtitles as subs
from prosperos_hoard.editing.errors import LumiereError, ModelUnavailable
from prosperos_hoard.editing.render import runner
from prosperos_hoard.editing.render.ass import build_ass, raw_cues

NUMBERED = re.compile(r"^(\d+)\|(.*)$")


def numbered(messages):
    """The {n: text} lines of the user message a translation request carries."""
    out = {}
    for line in messages[-1]["content"].splitlines():
        m = NUMBERED.match(line.strip())
        if m:
            out[int(m.group(1))] = m.group(2)
    return out


def faithful(messages):
    return "\n".join(f"{n}|[en] {t}" for n, t in numbered(messages).items())


SENTENCES = ["Hola qué tal.", "Vuelvo ahora mismo.", "Gracias por venir.", "Esto es una prueba.", "Seguimos con lo siguiente.", "Otra frase más.",
             "Casi lo tenemos.", "Un último detalle.", "Y con esto termino.", "Nos vemos pronto."]


def speech(svc, media_id, n=10):
    """n sentences of three words, one per second, in the media's first 11 s."""
    words, t = [], 100
    for s in SENTENCES[:n]:
        parts = s.split()
        for w in parts:
            words.append((t, t + 250, w))
            t += 300
        t += 100
    fake_transcript(svc, media_id, words, language="es")


@pytest.fixture
def setup(tmp_path, media_dir):
    def make(responder=faithful, **kw):
        link = FakeLink(responder, available=kw.pop("available", True))
        svc = make_services(tmp_path, link=link)
        svc.start()
        mid = media_store.import_path(svc, str(media_dir / "talk.mp4"), prepare=False)["id"]
        pid = store.create(svc, "Charla", preset="hd720", media=[mid])["id"]
        speech(svc, mid, kw.pop("n", 10))
        store.edit(svc, pid, [{"op": "captions", "enabled": True, "style": "clean", "props": {"max_words": 6}}])
        return svc, link, mid, pid

    return make


def test_translation_copies_the_original_timing(setup):
    svc, link, mid, pid = setup()
    cues = subs.source_cues(svc, store.doc(svc, pid))
    assert len(cues) == 10
    job = subs.start_translation(svc, pid, "inglés")
    job = svc.jobs.get(job["id"])
    assert job["state"] == "done", job["error"]
    assert job["result"]["cues"] == 10 and job["result"]["translated"] == 10 and job["result"]["language"] == "en"
    rec = subs.get_record(svc, pid, "en")
    assert [(c["start"], c["end"]) for c in rec["cues"]] == [(a, b) for a, b, _ in cues]
    assert [c["text"] for c in rec["cues"]] == [f"[en] {t}" for _, _, t in cues]
    assert rec["model"] == "fake-model"
    # compact numbered lines, effort off, one prompt for the whole batch of ten
    assert len(link.calls) == 1
    system = link.calls[0][0]["content"]
    assert "English" in system and "Spanish" in system and "exactly 10 lines" in system
    assert "1|Hola qué tal." in link.calls[0][-1]["content"]
    assert "lumiere.subtitles.translated" in svc._emit.types()
    status = subs.status(svc, pid)
    assert status["translations"][0]["fresh"] and status["source_language"] == "es"


def test_exports_srt_vtt_ass_and_dual_keep_the_cue_times(setup):
    svc, _, mid, pid = setup()
    subs.translate(svc, pid, "en")
    plain, _ = runner.subtitles_export(svc, pid, "srt")
    en, _ = runner.subtitles_export(svc, pid, "srt", language="en")
    stamps = lambda text: re.findall(r"\d\d:\d\d:\d\d,\d{3} --> \d\d:\d\d:\d\d,\d{3}", text)  # noqa: E731
    assert stamps(en) == stamps(plain) and len(stamps(en)) == 10
    assert "[en] Hola qué tal." in en and "[en]" not in plain
    dual, _ = runner.subtitles_export(svc, pid, "srt", language="en", dual=True)
    block = dual.split("\n\n")[0].splitlines()
    assert block[2] == "Hola qué tal." and block[3] == "[en] Hola qué tal."
    vtt, mime = runner.subtitles_export(svc, pid, "vtt", language="en")
    assert vtt.startswith("WEBVTT") and mime == "text/vtt" and "[en] Gracias por venir." in vtt
    ass, _ = runner.subtitles_export(svc, pid, "ass", language="en", dual=True)
    dialogues = [line for line in ass.splitlines() if line.startswith("Dialogue")]
    assert len(dialogues) == 10 and "Hola qué tal.\\N{" in dialogues[0] and dialogues[0].endswith("[en] Hola qué tal.")
    # the dual line is smaller than the first one
    size = int(re.search(r"Style: Cap,[^,]+,(\d+),", ass).group(1))
    assert f"\\fs{int(size * 0.78)}" in dialogues[0]
    # upper-casing follows the captions setting for translations too
    store.edit(svc, pid, [{"op": "captions", "props": {"uppercase": True}}])
    up, _ = runner.subtitles_export(svc, pid, "srt", language="en")
    assert "[EN] HOLA QUÉ TAL." in up


def test_model_that_skips_and_merges_lines_is_repaired(setup):
    state = {"n": 0}

    def sloppy(messages):
        state["n"] += 1
        lines = numbered(messages)
        if state["n"] > 1:
            return faithful(messages)
        out = []
        for n, t in lines.items():
            if n == 3:
                continue                                  # skipped
            if n == 5:
                out.append(f"5|[en] {t} {lines[6]}")      # 6 glued into 5
                continue
            if n == 6:
                continue
            out.append(f"{n}. [en] {t}" if n == 8 else f"{n}|[en] {t}")   # another numbering style
        return "```\n" + "\n".join(out) + "\n```\n\nHope this helps!"

    svc, link, mid, pid = setup(sloppy)
    out = subs.translate(svc, pid, "en")
    rec = subs.get_record(svc, pid, "en")
    src = [t for _, _, t in subs.source_cues(svc, store.doc(svc, pid))]
    assert [c["text"] for c in rec["cues"]] == [f"[en] {t}" for t in src]
    assert out["fallback"] == [] and "warning" not in out
    assert len(link.calls) == 2                       # one round to repair 3, 5 and 6
    again = numbered(link.calls[1])
    assert sorted(again) == [3, 5, 6]                  # only what was wrong is asked again, with the same numbers
    assert "skipped or merged" in link.calls[1][-1]["content"]


def test_wrapped_translations_and_stray_lines_are_parsed(setup):
    def wrapped(messages):
        lines = numbered(messages)
        out = []
        for n, t in lines.items():
            out.append(f"{n}|[en] {t}" if n % 2 else f"{n}|[en] {t.split()[0]}\n{' '.join(t.split()[1:])}")
        return "Here you go:\n" + "\n".join(out)

    svc, link, mid, pid = setup(wrapped)
    subs.translate(svc, pid, "en")
    rec = subs.get_record(svc, pid, "en")
    assert rec["cues"][1]["text"] == "[en] Vuelvo ahora mismo."      # the wrapped line was joined back
    assert not any(c.get("fallback") for c in rec["cues"]) and len(link.calls) == 1


def test_an_unusable_model_leaves_the_cues_untranslated_and_says_so(setup):
    svc, link, mid, pid = setup(lambda messages: "I cannot help with that.", n=3)
    out = subs.translate(svc, pid, "en")
    assert out["fallback"] == [1, 2, 3] and "could not be translated" in out["warning"]
    rec = subs.get_record(svc, pid, "en")
    assert all(c["text"] == c["source"] for c in rec["cues"])
    assert len(link.calls) <= subs.ROUNDS + 3          # bounded: rounds, then one request per cue


def test_without_a_model_it_fails_clearly(setup):
    svc, link, mid, pid = setup(available=False)
    with pytest.raises(ModelUnavailable) as err:
        subs.translate(svc, pid, "fr")
    assert "No local model is available" in str(err.value) and err.value.code == "model_unavailable"
    job = svc.jobs.get(subs.start_translation(svc, pid, "fr")["id"])
    assert job["state"] == "failed" and "No local model" in job["error"]
    assert subs.status(svc, pid)["translations"] == []
    # an export asks for the translation first
    with pytest.raises(LumiereError) as miss:
        runner.subtitles_export(svc, pid, "srt", language="fr")
    assert miss.value.code == "translation_missing"
    # and the tool says the same
    res = agent_tools.call_tool(svc, "subtitles_translate", {"project": pid, "language": "fr", "wait_s": 5})
    assert res["state"] == "failed" and "No local model" in res["error"]


def test_needs_a_transcript_and_a_different_language(tmp_path, media_dir):
    svc = make_services(tmp_path, link=FakeLink(faithful))
    mid = media_store.import_path(svc, str(media_dir / "talk.mp4"), prepare=False)["id"]
    pid = store.create(svc, "Sin texto", preset="hd720", media=[mid])["id"]
    with pytest.raises(LumiereError) as err:
        subs.start_translation(svc, pid, "en")
    assert err.value.code == "no_transcript"
    speech(svc, mid, 2)
    with pytest.raises(LumiereError) as same:
        subs.translate(svc, pid, "es")
    assert same.value.code == "same_language"
    with pytest.raises(LumiereError):
        subs.translate(svc, pid, "")


def test_language_names():
    assert subs.resolve_language("inglés") == ("en", "English")
    assert subs.resolve_language("EN-us") == ("en", "English")
    assert subs.resolve_language("Français") == ("fr", "French")
    assert subs.resolve_language("alemán")[0] == "de" and subs.resolve_language("catalán")[0] == "ca"
    assert subs.resolve_language("Klingon") == ("klingon", "Klingon")


def test_a_translation_goes_stale_when_the_cuts_change_and_reuses_what_did_not(setup):
    svc, link, mid, pid = setup()
    subs.translate(svc, pid, "en")
    calls = len(link.calls)
    assert subs.translate(svc, pid, "en")["cached"] and len(link.calls) == calls
    before = subs.get_record(svc, pid, "en")
    store.edit(svc, pid, [{"op": "delete_range", "start": 0, "end": 500}])       # cuts the first words
    assert subs.status(svc, pid)["translations"][0]["stale"]
    with pytest.raises(LumiereError) as stale:
        runner.subtitles_export(svc, pid, "srt", language="en")
    assert stale.value.code == "translation_stale" and "translate again" in str(stale.value)
    # the cue text changed for the first cue only ("Hola qué tal." lost words), the other nine are reused
    out = subs.translate(svc, pid, "en")
    assert not out["cached"] and out["reused"] >= 8 and out["translated"] <= 2
    asked = numbered(link.calls[-1])
    assert len(asked) <= 2
    after = subs.get_record(svc, pid, "en")
    assert [c["text"] for c in after["cues"][-5:]] == [c["text"] for c in before["cues"][-5:]]
    assert after["cues"][-1]["start"] == before["cues"][-1]["start"] - 500       # the timing follows the cut
    assert runner.subtitles_export(svc, pid, "srt", language="en")[0]


def test_glossary_and_do_not_translate_names_reach_the_model(setup):
    svc, link, mid, pid = setup()
    out = subs.translate(svc, pid, "en", glossary={"prueba": "test"}, keep=["Hola"])
    system = link.calls[0][0]["content"]
    assert "Do not translate these names or terms, copy them exactly as written: Hola." in system and "prueba => test" in system
    assert out["glossary"] == {"prueba": "test"} and out["keep"] == ["Hola"]
    n = len(link.calls)
    assert subs.translate(svc, pid, "en", glossary={"prueba": "test"}, keep=["Hola"])["cached"] and len(link.calls) == n
    subs.translate(svc, pid, "en", glossary={"prueba": "trial"}, keep=["Hola"])      # other terms: translated again, not reused
    assert len(link.calls) == n + 1 and numbered(link.calls[-1]).keys() == set(range(1, 11))


def test_fix_show_delete_and_the_tool(setup):
    svc, link, mid, pid = setup()
    res = agent_tools.call_tool(svc, "subtitles_translate", {"project": pid, "language": "en", "wait_s": 5, "keep": ["Hola"]})
    assert res["state"] == "done" and res["result"]["cues"] == 10
    shown = agent_tools.call_tool(svc, "subtitles_translate", {"project": pid, "language": "en", "action": "show", "limit": 2})
    assert shown["total"] == 10 and shown["items"][0]["n"] == 1 and shown["items"][0]["text"] == "[en] Hola qué tal."
    fixed = agent_tools.call_tool(svc, "subtitles_translate", {"project": pid, "language": "en", "action": "fix",
                                                               "changes": [{"n": 1, "text": "Hi, how are you?"}]})
    assert fixed["changed"] == 1
    assert "Hi, how are you?" in runner.subtitles_export(svc, pid, "srt", language="en")[0]
    # a correction by hand survives translating again after a cut elsewhere
    store.edit(svc, pid, [{"op": "delete_range", "start": 9000, "end": 11000}])
    subs.translate(svc, pid, "en", keep=["Hola"])
    assert "Hi, how are you?" in runner.subtitles_export(svc, pid, "srt", language="en")[0]
    exported = agent_tools.call_tool(svc, "subtitles_export", {"project": pid, "language": "en", "format": "vtt"})
    assert exported["language"] == "en" and exported["text"].startswith("WEBVTT")
    status = agent_tools.call_tool(svc, "subtitles_translate", {"project": pid, "action": "status"})
    assert status["translations"][0]["language"] == "en" and status["translations"][0]["edited"] == [1]
    assert agent_tools.call_tool(svc, "subtitles_translate", {"project": pid, "language": "en", "action": "delete"})["deleted"]
    assert subs.status(svc, pid)["translations"] == []


def test_translating_over_http_and_downloading_files(setup, tmp_path):
    from fastapi.testclient import TestClient

    from prosperos_hoard.editing.main import create_app

    svc, link, mid, pid = setup()
    with TestClient(create_app(svc.config, svc), base_url="http://127.0.0.1") as c:
        job = c.post(f"/api/projects/{pid}/subtitles/translate", json={"language": "fr"}).json()
        assert c.get(f"/api/jobs/{job['id']}").json()["state"] == "done"
        status = c.get(f"/api/projects/{pid}/subtitles").json()
        assert status["translations"][0]["language"] == "fr" and status["translations"][0]["fresh"]
        r = c.get(f"/api/projects/{pid}/subtitles.srt", params={"language": "fr", "dual": "true"})
        assert r.status_code == 200 and "subtitulos.fr.srt" in r.headers["content-disposition"] and "[en] Hola" in r.text
        assert c.get(f"/api/projects/{pid}/subtitles.srt", params={"language": "de"}).status_code == 400
        assert c.get(f"/api/projects/{pid}/subtitles/fr", params={"limit": 3}).json()["items"][2]["n"] == 3
        assert c.patch(f"/api/projects/{pid}/subtitles/fr", json={"changes": [{"n": 2, "text": "Je reviens."}]}).json()["changed"] == 1
        assert c.delete(f"/api/projects/{pid}/subtitles/fr").json()["deleted"]


def test_translations_are_removed_with_their_project(setup):
    svc, link, mid, pid = setup()
    subs.translate(svc, pid, "en")
    store.delete(svc, pid)
    assert svc.db.one("SELECT COUNT(*) c FROM translations")["c"] == 0


# ---------------------------------------------------------------- burned in (real frames)

def frame(path, t_s, dest):
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-ss", str(t_s), "-i", str(path), "-frames:v", "1", str(dest)], check=True)
    from PIL import Image

    return Image.open(dest).convert("RGB")


def lower_diff(a, b):
    from PIL import ImageChops, ImageStat

    h = a.size[1]
    low = ImageChops.difference(a.crop((0, int(h * 0.6), a.size[0], h)), b.crop((0, int(h * 0.6), b.size[0], h)))
    high = ImageChops.difference(a.crop((0, 0, a.size[0], int(h * 0.5))), b.crop((0, 0, b.size[0], int(h * 0.5))))
    return sum(ImageStat.Stat(low).mean) / 3, sum(ImageStat.Stat(high).mean) / 3


@needs_ffmpeg
def test_translated_captions_are_burned_in_at_the_cue_times(tmp_path, media_dir):
    svc = make_services(tmp_path, link=FakeLink(faithful))
    svc.start()
    mid = media_store.import_path(svc, str(media_dir / "talk.mp4"))["id"]
    pid = store.create(svc, "Burn", preset="hd720", media=[mid])["id"]
    speech(svc, mid, 3)           # three cues, the last one ends at 3.2 s with its 250 ms hold
    store.edit(svc, pid, [{"op": "captions", "enabled": False, "style": "bold"}])   # off in the project: the translation burns anyway

    def render(**kw):
        job = svc.jobs.get(svc.start_render(pid, preset="preview", end=4500, **kw)["id"])
        assert job["state"] == "done", job["error"]
        return job["result"]

    plain = render(filename="plain")
    assert svc.db.one("SELECT COUNT(*) c FROM translations")["c"] == 0
    burned = render(filename="burned", captions_language="en", subtitles=True)         # translated on the fly: no translation existed
    assert svc.db.one("SELECT COUNT(*) c FROM translations")["c"] == 1
    cues = subs.get_record(svc, pid, "en")["cues"]
    inside = (cues[0]["start"] + cues[0]["end"]) / 2000
    outside = 3.8        # after the last cue
    d = tmp_path / "f"
    d.mkdir()
    on_plain, on_burned = frame(plain["path"], inside, d / "a.png"), frame(burned["path"], inside, d / "b.png")
    low, high = lower_diff(on_plain, on_burned)
    assert low > 3 and low > 4 * high, (low, high)           # text in the lower third; elsewhere only encoder noise
    off_plain, off_burned = frame(plain["path"], outside, d / "c.png"), frame(burned["path"], outside, d / "d.png")
    low_off, _ = lower_diff(off_plain, off_burned)
    assert low_off < 1.5 and low > 3 * low_off, (low, low_off)   # no text after the last cue
    assert burned["subtitles"].endswith("burned.en.srt")
    assert "[en] Hola qué tal." in open(burned["subtitles"], encoding="utf-8").read()
    # dual: two lines are drawn, more of the picture changes than with one
    dual = render(filename="dual", captions_language="en", captions_dual=True)
    low_dual, _ = lower_diff(on_plain, frame(dual["path"], inside, d / "e.png"))
    assert low_dual > low
    # the project itself still has captions off and no translation was forced on it
    assert store.doc(svc, pid).captions.enabled is False


@needs_ffmpeg
def test_burn_in_of_a_range_shifts_the_cues_and_needs_a_model_when_missing(tmp_path, media_dir):
    svc = make_services(tmp_path, link=FakeLink(available=False))
    svc.start()
    mid = media_store.import_path(svc, str(media_dir / "talk.mp4"))["id"]
    pid = store.create(svc, "Rango", preset="hd720", media=[mid])["id"]
    speech(svc, mid, 6)
    job = svc.jobs.get(svc.start_render(pid, preset="preview", end=2000, captions_language="en")["id"])
    assert job["state"] == "failed" and "No local model" in job["error"]
    svc.link_sync.available = True
    svc.link_sync.responder = faithful
    subs.translate(svc, pid, "en")
    cues = [c for c in subs.get_record(svc, pid, "en")["cues"]]
    start = cues[2]["start"] - 100
    shifted = subs.shift_cues(subs.translated_cues(svc, pid, "en"), start, 2500)
    third = next(s for s in shifted if s[2] == [cues[2]["text"]])
    assert third[0] == cues[2]["start"] - start and all(0 <= x < y <= 2500 for x, y, _ in shifted)
    plain = svc.jobs.get(svc.start_render(pid, preset="preview", start=start, end=start + 2500)["id"])["result"]
    burned = svc.jobs.get(svc.start_render(pid, preset="preview", start=start, end=start + 2500, captions_language="en")["id"])
    assert burned["state"] == "done", burned["error"]
    d = tmp_path / "f"
    d.mkdir()
    t = (third[0] + third[1]) / 2000                 # the middle of the third cue, in the range's own time
    low, high = lower_diff(frame(plain["path"], t, d / "a.png"), frame(burned["result"]["path"], t, d / "b.png"))
    assert low > 3 and low > 4 * high, (low, high)


def test_ass_cues_override_the_words_exactly(setup):
    svc, _, mid, pid = setup()
    p = store.doc(svc, pid)
    cues = [(1000, 2000, ["Hello there"]), (3000, 3500, ["Bye", "Adiós"])]
    ass, counts = build_ass(p, lambda m: None, cues)
    lines = [line for line in ass.splitlines() if line.startswith("Dialogue")]
    assert counts["captions"] == 2 and "0:00:01.00,0:00:02.00" in lines[0] and "0:00:03.00,0:00:03.50" in lines[1]
    assert lines[1].endswith("Adiós") and "Bye\\N{" in lines[1]
    assert raw_cues(p, subs.words_for(svc))[0][2] == "Hola qué tal."
