"""B-roll suggestions: keyword matching of the main track's sentences against clip names, tags and the clips' own transcripts, the
optional model keywords, and placing a suggestion as a muted cover overlay (checked on a rendered frame)."""

import shutil

import pytest
from PIL import Image

from conftest import FakeLink, fake_transcript, make_services, needs_ffmpeg
from prosperos_hoard.editing import broll, commands
from prosperos_hoard.editing import media as media_store
from prosperos_hoard.editing import projects as store
from prosperos_hoard.editing.agent_tools import call_tool
from prosperos_hoard.editing.render import runner

MAIN_WORDS = [  # (t0, t1, text): four sentences over the 12 s talk
    (200, 600, "Vamos"), (700, 900, "a"), (1000, 1200, "la"), (1300, 1800, "playa"), (1900, 2200, "esta"), (2300, 2800, "tarde."),
    (3000, 3400, "La"), (3500, 4100, "ciudad"), (4200, 4500, "nos"), (4600, 5200, "esperaba"), (5300, 5500, "de"), (5600, 6000, "noche."),
    (6500, 7000, "Mañana"), (7100, 7700, "hablamos"), (7800, 8000, "del"), (8100, 8900, "bosque."),
    (9500, 10000, "Hmm."),
]


def test_tokens_and_stemming():
    assert broll.token_pairs("Las playas de MAR_azul-2024.mp4") == [("play", "playas"), ("mar", "mar"), ("azul", "azul"), ("mp4", "mp4")]
    assert broll.stem("coches") == broll.stem("coche") and broll.stem("cars") == broll.stem("car") and broll.stem("running") == "runn"
    assert broll.stem("playas") == broll.stem("playa") and broll.stem("luces") != "" and broll.stem("ciudades") == broll.stem("ciudad")
    assert broll.tokens_of("el la de que y a") == []  # stop words and very short words carry no meaning
    assert broll.tokens_of("PlayaAtardecer") == [broll.stem("playa"), broll.stem("atardecer")]  # file names: camelCase and separators split


@pytest.fixture
def lab(tmp_path, media_dir):
    link = FakeLink(responder=lambda messages: "0: arena, mar, olas\n1: luces, edificios\n2: arboles\n3: -")
    svc = make_services(tmp_path, link=link)
    svc.start()
    lib = tmp_path / "lib"
    lib.mkdir()
    shutil.copy(media_dir / "scenes.mp4", lib / "playa_atardecer.mp4")
    shutil.copy(media_dir / "scenes.mp4", lib / "clip_c.mp4")
    shutil.copy(media_dir / "vert.mp4", lib / "montana_nieve.mp4")
    shutil.copy(media_dir / "vert.mp4", lib / "mar_olas.mp4")
    shutil.copy(media_dir / "pic.png", lib / "Edificios_Luces.png")
    ids = {"talk": media_store.import_path(svc, str(media_dir / "talk.mp4"))["id"]}
    for name in ("playa_atardecer", "clip_c", "montana_nieve", "mar_olas", "Edificios_Luces"):
        ids[name] = media_store.import_path(svc, str(next(lib.glob(name + ".*"))))["id"]
    fake_transcript(svc, ids["talk"], MAIN_WORDS)
    # clip_c talks about a city at night in its second scene (2-4 s of the clip)
    fake_transcript(svc, ids["clip_c"], [(2200, 2700, "la"), (2800, 3300, "ciudad"), (3400, 3700, "por"), (3800, 4300, "la"), (4300, 4800, "noche.")])
    media_store.put_analysis(svc, ids["clip_c"], "scenes", {"cuts": [{"t": 2000, "score": 90}, {"t": 4000, "score": 90}]})
    media_store.set_tags(svc, ids["montana_nieve"], ["bosque", "Árboles nevados"])
    pid = store.create(svc, "Charla", preset="hd720", media=[ids["talk"]])["id"]
    yield svc, ids, pid, link
    svc.stop()


def best(res, i):
    return res["suggestions"][i]["options"][0]


@needs_ffmpeg
def test_each_sentence_gets_clips_matching_its_words_names_tags_and_speech(lab):
    svc, ids, pid, link = lab
    res = broll.suggest(svc, pid)
    sug = res["suggestions"]
    assert [s["sentence"] for s in sug][:3] == ["Vamos a la playa esta tarde.", "La ciudad nos esperaba de noche.", "Mañana hablamos del bosque."]
    assert res["library_checked"] == 5 and res["model"] == {"requested": False, "used": False, "name": None, "note": None}  # the main clip itself is left out
    a = best(res, 0)
    assert a["media"] == ids["playa_atardecer"] and a["via"] == "name" and a["matched"] == ["playa"] and a["src_in"] == 0
    assert a["length"] == min(2800 - 200, 6000) and sug[0]["start_ms"] == 200 and sug[0]["end_ms"] == 2800
    # what a clip says beats nothing at all: clip_c talks about the city at night, and the shot starts where its scene does (2 s)
    b = best(res, 1)
    assert b["media"] == ids["clip_c"] and b["via"] == "transcript" and set(b["matched"]) == {"ciudad", "noche"} and b["src_in"] == 2000
    assert b["src_out"] - b["src_in"] == 3000 and b["at"] == "0:02.000–0:05.000"
    # tags are labels: the word is not in the file name at all
    c = best(res, 2)
    assert c["media"] == ids["montana_nieve"] and c["matched"] == ["bosque"] and c["via"] == "name"
    # a sentence without content words gets no options
    assert sug[3]["options"] == [] and sug[3]["sentence"] == "Hmm."
    assert res["with_options"] == 3 and all(0 < o["score"] <= 1 for s in sug for o in s["options"])
    assert not link.calls  # the model is only used when asked
    # options are sorted by score and each media appears once per sentence
    for s in sug:
        scores = [o["score"] for o in s["options"]]
        assert scores == sorted(scores, reverse=True) and len({o["media"] for o in s["options"]}) == len(s["options"])


@needs_ffmpeg
def test_a_range_is_one_query_and_the_model_adds_visual_keywords(lab):
    svc, ids, pid, link = lab
    res = broll.suggest(svc, pid, start=3000, end=6200)
    assert len(res["suggestions"]) == 1 and res["suggestions"][0]["range"] == "0:03.000–0:06.200"
    assert best(res, 0)["media"] == ids["clip_c"]
    plain = broll.suggest(svc, pid, per_sentence=5)
    assert ids["mar_olas"] not in [o["media"] for o in plain["suggestions"][0]["options"]]  # "playa" says nothing about a clip called mar_olas
    smart = broll.suggest(svc, pid, per_sentence=5, use_model=True)
    assert smart["model"]["used"] and smart["model"]["name"] == "fake-model" and len(link.calls) == 1
    assert "0: Vamos a la playa esta tarde." in link.calls[0][1]["content"]
    first = [o["media"] for o in smart["suggestions"][0]["options"]]
    assert ids["playa_atardecer"] in first[:2] and ids["mar_olas"] in first  # keywords "mar, olas" found the sea clip
    assert ids["Edificios_Luces"] in [o["media"] for o in smart["suggestions"][1]["options"]]  # "luces, edificios"
    # an unreachable model changes nothing but a note
    svc.link_sync = FakeLink(available=False)
    down = broll.suggest(svc, pid, use_model=True)
    assert down["model"]["used"] is False and "not reachable" in down["model"]["note"] and best(down, 0)["media"] == ids["playa_atardecer"]


@needs_ffmpeg
def test_requirements_are_explained(lab, tmp_path, media_dir):
    svc, ids, pid, link = lab
    with pytest.raises(Exception, match="no speech"):
        broll.suggest(svc, store.create(svc, "Vacío", preset="hd720")["id"])
    lonely = make_services(tmp_path / "other")
    lonely.start()
    try:
        talk = media_store.import_path(lonely, str(media_dir / "talk.mp4"))["id"]
        p2 = store.create(lonely, "x", preset="hd720", media=[talk])["id"]
        with pytest.raises(commands.NeedsAnalysis):
            broll.suggest(lonely, p2)  # no transcript yet: queued
        fake_transcript(lonely, talk, MAIN_WORDS)
        with pytest.raises(Exception, match="no other videos or images"):
            broll.suggest(lonely, p2)
    finally:
        lonely.stop()
    with pytest.raises(Exception, match="place must be"):
        broll.suggest(svc, pid, place="all")


def mean_color(path):
    img = Image.open(path).convert("RGB").resize((16, 9))
    px = list(img.getdata())
    return tuple(sum(p[i] for p in px) / len(px) for i in range(3))


@needs_ffmpeg
def test_placing_the_best_options_makes_muted_cover_overlays_you_can_see(lab):
    svc, ids, pid, link = lab
    before = store.doc(svc, pid)
    out = broll.suggest(svc, pid, place="best")
    assert [p["start"] for p in out["placed"]] == [200, 3000, 6500] and out["rev"]
    p = store.doc(svc, pid)
    track = next(t for t in p.tracks if t.name == "B-roll")
    assert track.kind == "video" and track.role == "overlay" and p.tracks.index(track) == len(p.tracks) - 1
    clips = sorted(track.clips, key=lambda c: c.start)
    assert [(c.start, c.end, c.media) for c in clips] == [(200, 2800, ids["playa_atardecer"]), (3000, 6000, ids["clip_c"]),
                                                          (6500, 8900, ids["montana_nieve"])]
    assert all(c.mute and c.transform.fit == "cover" for c in clips) and clips[1].src_in == 2000
    assert p.duration == before.duration  # overlays do not change the length of the edit
    assert store.history(svc, pid)["items"][0]["label"] == "B-roll sugerido"
    # what the viewer sees: the playa clip (red scene) over the first sentence, clip_c's green scene over the second, the talk elsewhere
    red = mean_color(runner.render_frame(svc, pid, 1200, width=320))
    green = mean_color(runner.render_frame(svc, pid, 3500, width=320))
    talk = mean_color(runner.render_frame(svc, pid, 10500, width=320))
    assert red[0] > 200 and red[1] < 60 and red[2] < 60, red
    assert green[1] > green[0] + 60 and green[1] > green[2] + 60, green
    assert not (talk[0] > 200 and talk[1] < 60), talk
    store.undo(svc, pid)
    assert not [t for t in store.doc(svc, pid).tracks if t.name == "B-roll"]


@needs_ffmpeg
def test_one_suggestion_can_be_placed_by_hand_and_the_tools_expose_it(lab):
    svc, ids, pid, link = lab
    res = call_tool(svc, "broll_suggest", {"project": pid, "start": "0:03", "end": "0:06"})
    opt = res["suggestions"][0]["options"][0]
    placed = call_tool(svc, "timeline_edit", {"project": pid, "ops": [{"op": "add_overlay", "media": opt["media"], "start": 3000, "length": 3000,
                                                                       "src_in": opt["src_in"], "fade_in": 200, "fade_out": 200}]})
    c = store.doc(svc, pid).find(placed["results"][0]["clip"])[1]
    assert c.mute and c.fade_in == 200 and c.src_in == 2000
    first = call_tool(svc, "media_tag", {"media": ids["clip_c"], "tags": ["Nueva York", "taxis"]})
    assert first["tags"] == ["Nueva York", "taxis"]
    res = call_tool(svc, "broll_suggest", {"project": pid, "start": 0, "end": 2800})
    assert res["with_options"] == 1
    own = call_tool(svc, "broll_suggest", {"project": store.create(svc, "y", preset="hd720", media=[ids["clip_c"]])["id"]})
    assert own["suggestions"] and ids["clip_c"] not in [o["media"] for s in own["suggestions"] for o in s["options"]]  # never itself
