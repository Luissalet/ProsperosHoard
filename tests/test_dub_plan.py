"""Dubbing plan and quality check: the pure functions in `dub_plan` and how a
dub job and `resynthesize_segment` record them (fakes for TTS, STT and the LLM)."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from prosperos_hoard import dub_plan as dp
from prosperos_hoard import dubbing as db
from prosperos_hoard import voice_engines as ve
from prosperos_hoard.hoard_link.errors import Unavailable
from prosperos_hoard.jobs import Progress
from prosperos_hoard.store import Store

from test_dubbing import HAS_FFMPEG, FakeBackend, FakeSTT, FakeTTS, _tiny_video


# ------------------------------------------------------------- estimation --

def test_normalize_lang_accepts_names_codes_and_regions():
    assert dp.normalize_lang("Spanish") == "es"
    assert dp.normalize_lang("es-ES") == "es"
    assert dp.normalize_lang("castellano") == "es"
    assert dp.normalize_lang("JA") == "ja"
    assert dp.normalize_lang("zho") == "zh"
    assert dp.normalize_lang("Klingon") is None
    assert dp.normalize_lang(None) is None


def test_estimate_duration_uses_language_table_and_default():
    assert dp.estimate_duration_s("x" * 145, "es") == pytest.approx(10.0)
    assert dp.estimate_duration_s("x" * 140, "English") == pytest.approx(10.0)
    assert dp.estimate_duration_s("x" * 135, "Klingon") == pytest.approx(10.0)  # default 13.5
    assert dp.estimate_duration_s("x" * 75, "ja") == pytest.approx(10.0)
    assert dp.estimate_duration_s("", "es") == 0.0
    # whitespace runs count once
    assert dp.estimate_duration_s("a  b\n c", "es") == dp.estimate_duration_s("a b c", "es")


def test_estimate_duration_prefers_calibration():
    assert dp.estimate_duration_s("x" * 100, "es", calibration=10.0) == pytest.approx(10.0)


def test_calibrate_needs_three_samples():
    assert dp.calibrate([("hello world", 1.0), ("another line", 1.0)]) is None
    assert dp.calibrate([]) is None
    # unusable samples (empty text, no duration) do not count
    assert dp.calibrate([("hello world", 1.0), ("", 1.0), ("hello", 0.0), ("hello", -1.0)]) is None


def test_calibrate_is_median_and_ignores_outliers():
    samples = [("x" * 140, 10.0), ("x" * 150, 10.0), ("x" * 130, 10.0), ("x" * 140, 60.0), ("x" * 900, 10.0)]
    assert dp.calibrate(samples) == pytest.approx(14.0)


# ------------------------------------------------------------------- plan --

def _segs(*triples: tuple[float, float, str]) -> list[dict[str, Any]]:
    return [{"index": i, "start_s": a, "end_s": b, "translated_text": t} for i, (a, b, t) in enumerate(triples)]


def test_plan_classes_without_gap():
    # es: 14.5 cps. Slot 2.0 s -> 29 chars fit; 36 chars = x1.24 tight; 60 chars impossible.
    segs = _segs((0, 2, "x" * 29), (2, 4, "x" * 36), (4, 6, "x" * 60))
    plan = dp.plan_segments(segs, "es")
    assert [p["class"] for p in plan] == ["fits", "tight", "impossible"]
    assert plan[0]["slot_s"] == 2.0 and plan[0]["gap_s"] == 0.0
    assert plan[1]["factor"] == pytest.approx(1.241, abs=0.001)
    assert plan[2]["beyond_hard"] is True
    assert plan[2]["char_budget"] == 29


def test_plan_borrows_gap_up_to_cap():
    # the second line starts 1.5 s after the first ends: only 0.6 s may be borrowed
    segs = _segs((0, 2, "x" * 38), (3.5, 5.5, "x" * 10))
    plan = dp.plan_segments(segs, "es")
    assert plan[0]["gap_s"] == 0.6 and plan[0]["slot_s"] == 2.6
    assert plan[0]["class"] == "tight"  # 38 / 14.5 = 2.62 s against 2.6: the cap leaves it just over
    assert plan[0]["factor"] == pytest.approx(1.008, abs=0.001)


def test_plan_gap_makes_a_line_fit():
    segs = _segs((0, 2, "x" * 33), (2.3, 4.3, "x"))
    plan = dp.plan_segments(segs, "es")  # 33 chars = 2.28 s; slot 2.0 + 0.3 gap
    assert plan[0]["gap_s"] == pytest.approx(0.3)
    assert plan[0]["class"] == "fits"
    no_gap = dp.plan_segments(_segs((0, 2, "x" * 33), (2.0, 4.0, "x")), "es")
    assert no_gap[0]["class"] == "tight"


def test_plan_overlap_gives_no_gap_and_last_line_has_tail_gap_zero():
    segs = _segs((0, 2, "x" * 20), (1.5, 3, "x" * 20))
    plan = dp.plan_segments(segs, "es")
    assert plan[0]["gap_s"] == 0.0
    assert plan[1]["gap_s"] == 0.0
    assert dp.plan_segments(segs, "es", tail_gap_s=0.4)[1]["gap_s"] == 0.4


def test_plan_thresholds_and_calibration_are_configurable():
    segs = _segs((0, 2, "x" * 36), (2, 4, "x" * 36))
    assert dp.plan_segments(segs, "es", max_speedup=1.5)[0]["class"] == "tight"
    assert dp.plan_segments(segs, "es", max_speedup=1.1)[0]["class"] == "impossible"
    slow = dp.plan_segments(segs, "es", calibration=10.0)  # a slower voice: 3.6 s of speech
    assert slow[0]["class"] == "impossible" and slow[0]["cps"] == 10.0


# -------------------------------------------------------------- shortening --

def _impossible_plan():
    return dp.plan_segments(_segs((0, 2, "x" * 20), (2, 4, "y" * 80), (4, 6, "z" * 29)), "es")


def test_shorten_lines_replaces_impossible_line_and_stops_when_it_fits():
    calls = []

    def chat(messages):
        calls.append(messages)
        return '"' + "w" * 25 + '"'

    out = dp.shorten_lines(chat, _impossible_plan(), "Spanish")
    assert len(calls) == 1  # fit on the first try, no second attempt
    assert out[1]["text"] == "w" * 25
    assert out[1]["shortened_from"] == "y" * 80
    assert out[1]["class"] == "fits"
    assert out[1]["shorten"]["status"] == "shortened"
    assert out[0]["text"] == "x" * 20 and "shorten" not in out[0]
    assert "shorten" not in out[2]
    system = calls[0][0]["content"]
    assert "Return only the line" in system and "29 characters" in system
    assert calls[0][1]["content"] == "y" * 80


def test_shorten_lines_keeps_best_of_max_attempts():
    replies = iter(["a" * 70, "b" * 60, "c" * 65])  # a third reply must never be asked for

    def chat(messages):
        return next(replies)

    out = dp.shorten_lines(chat, _impossible_plan(), "Spanish", max_attempts=2)
    assert out[1]["text"] == "b" * 60
    assert out[1]["class"] == "impossible"
    assert [a["chars"] for a in out[1]["shorten"]["attempts"]] == [70, 60]
    assert out[1]["shorten"]["status"] == "shortened"


def test_shorten_lines_keeps_original_when_replies_are_longer():
    out = dp.shorten_lines(lambda m: "z" * 200, _impossible_plan(), "Spanish")
    assert out[1]["text"] == "y" * 80
    assert out[1]["shorten"]["status"] == "unchanged"
    assert "shortened_from" not in out[1]
    assert len(out[1]["shorten"]["attempts"]) == 2


def test_shorten_lines_without_llm_marks_no_llm():
    out = dp.shorten_lines(None, _impossible_plan(), "Spanish")
    assert out[1]["text"] == "y" * 80
    assert out[1]["shorten"] == {"status": "no_llm", "attempts": []}


def test_shorten_lines_survives_llm_going_away_or_failing():
    def gone(messages):
        raise Unavailable("llm", ["down"])

    out = dp.shorten_lines(gone, _impossible_plan(), "Spanish")
    assert out[1]["shorten"]["status"] == "no_llm"

    def broken(messages):
        raise RuntimeError("boom")

    out = dp.shorten_lines(broken, _impossible_plan(), "Spanish")
    assert out[1]["shorten"]["status"] == "error"
    assert out[1]["text"] == "y" * 80


def test_shorten_lines_ignores_empty_reply_and_does_not_mutate_input():
    plan = _impossible_plan()
    before = [dict(p) for p in plan]
    out = dp.shorten_lines(lambda m: "  ", plan, "Spanish")
    assert out[1]["text"] == "y" * 80 and out[1]["shorten"]["status"] == "unchanged"
    assert plan == before


# --------------------------------------------------------------------- QC --

def test_word_error_rate_ignores_case_punctuation_and_spacing():
    assert dp.word_error_rate("Hola, ¿qué tal?", "hola  que tal") == pytest.approx(1 / 3, abs=0.001)  # qué != que
    assert dp.word_error_rate("Hello, World!", "hello world") == 0.0
    assert dp.word_error_rate("don't stop", "Dont stop.") == 0.0
    assert dp.word_error_rate("a b c d", "a x c") == pytest.approx(0.5)  # one substitution + one deletion
    assert dp.word_error_rate("a b", "") == 1.0
    assert dp.word_error_rate("", "") == 0.0
    assert dp.word_error_rate("", "something") == 1.0
    assert dp.word_error_rate("one", "one two three") == 2.0


@pytest.mark.parametrize("n,es,en", [
    (0, "cero", "zero"), (1, "uno", "one"), (16, "dieciséis", "sixteen"), (17, "diecisiete", "seventeen"),
    (21, "veintiuno", "twenty one"), (35, "treinta y cinco", "thirty five"), (100, "cien", "one hundred"),
    (101, "ciento uno", "one hundred one"), (555, "quinientos cincuenta y cinco", "five hundred fifty five"),
    (1000, "mil", "one thousand"), (2024, "dos mil veinticuatro", "two thousand twenty four"),
    (21000, "veintiún mil", "twenty one thousand"), (1000000, "un millón", "one million"),
    (3000000, "tres millones", "three million"),
])
def test_numbers_are_spelled_in_spanish_and_english(n, es, en):
    assert dp.spell_numbers(str(n), "es") == es
    assert dp.spell_numbers(str(n), "en") == en


def test_digits_heard_as_words_are_not_errors():
    assert dp.word_error_rate("Tengo 17 años", "tengo diecisiete años", "es") == 0.0
    assert dp.word_error_rate("I am seventeen", "I am 17", "en") == 0.0
    assert dp.word_error_rate("Back in 2024", "back in two thousand twenty four", "English") == 0.0
    # a wrong number is still an error, and the plain comparison still counts
    assert dp.word_error_rate("Tengo 17 años", "tengo dieciocho años", "es") == pytest.approx(1 / 3, abs=0.001)
    # without a language (or one with no speller) nothing changes
    assert dp.word_error_rate("I am seventeen", "I am 17") == pytest.approx(1 / 3, abs=0.001)
    assert dp.word_error_rate("Ich bin 17", "ich bin siebzehn", "de") == pytest.approx(1 / 3, abs=0.001)
    # tokens that are not plain digits (ordinals, mixed) are left alone
    assert dp.spell_numbers("the 1st of 3d", "en") == "the 1st of 3d"


def test_qc_line_uses_the_target_language_for_numbers():
    line = dp.qc_line("Son las 5", "son las cinco", "es")
    assert line["wer"] == 0.0 and not line["flagged"]


def test_char_error_rate_for_unspaced_scripts():
    assert dp.char_error_rate("今日は天気がいい", "今日は天気がいい。") == 0.0
    assert dp.char_error_rate("今日は天気がいい", "今日は天気が悪い") == pytest.approx(1 / 8)
    assert dp.char_error_rate("你好 世界", "你好世界") == 0.0
    assert dp.char_error_rate("", "x") == 1.0


@pytest.mark.parametrize("lang,metric", [("es", "wer"), ("Spanish", "wer"), ("ja", "cer"), ("zh-CN", "cer"),
                                          ("Korean", "cer"), ("th", "cer"), ("xx", "wer"), (None, "wer")])
def test_metric_by_language(lang, metric):
    assert dp.metric_for(lang) == metric


def test_qc_segments_flag_over_threshold():
    rows = dp.qc_segments([("hola mundo cruel", "hola mundo cruel"), ("hola mundo cruel", "adios"),
                           ("a b c d e f g h", "a b c d e f g x")], "es")
    assert [r["flagged"] for r in rows] == [False, True, False]  # 1/8 = 0.125 under 0.35
    assert rows[0]["metric"] == "wer" and rows[0]["wer"] == 0.0 and rows[0]["hyp"] == "hola mundo cruel"
    assert rows[1]["wer"] == pytest.approx(1.0)
    assert rows[1]["threshold"] == dp.DEFAULT_WER_THRESHOLD


def test_qc_segments_use_cer_threshold_for_cjk_and_custom_threshold():
    rows = dp.qc_segments([("今日は天気がいい", "今日は天気が悪い")], "ja")
    assert rows[0]["metric"] == "cer" and rows[0]["cer"] == pytest.approx(0.125) and not rows[0]["flagged"]
    assert dp.qc_segments([("今日は天気がいい", "今日は天気が悪い")], "ja", threshold=0.1)[0]["flagged"]
    assert dp.qc_segments([("a b c d", "a b c x")], "en", threshold=0.2)[0]["flagged"]  # 0.25 > 0.2
    assert not dp.qc_segments([("a b c d", "a b c x")], "en", threshold=0.3)[0]["flagged"]


def test_qc_line_without_hypothesis_is_skipped_not_flagged():
    row = dp.qc_line("hola", None, "es")
    assert row == {"status": "skipped", "flagged": False}


def test_summarize_counts():
    segs = [
        {"plan": {"class": "fits"}, "qc": {"status": "ok", "flagged": False}},
        {"plan": {"class": "tight"}, "qc": {"status": "ok", "flagged": True}},
        {"plan": {"class": "impossible", "shortened_from": "x"}, "qc": {"status": "skipped", "flagged": False}},
        {"plan": {"class": "impossible"}},
        {},
    ]
    assert dp.summarize(segs) == {"fits": 1, "tight": 1, "impossible": 2, "shortened": 1, "flagged": 1,
                                  "qc_skipped": 1, "untranslated": 0}


# ------------------------------------------------------------ job level --

LONG_LINE = "general kenobi " + "palabra de relleno " * 6  # far beyond a 1.5 s slot


class HearingSTT(FakeSTT):
    """The source transcript for the video; for a synthetic segment clip it
    'hears' whatever `heard` says for that segment index."""

    id = "hearing-stt"

    def __init__(self, heard: dict[int, str] | None = None, fail_on_clips: bool = False):
        self.heard = heard or {}
        self.fail_on_clips = fail_on_clips
        self.clip_calls: list[tuple[str, Any]] = []

    def transcribe(self, path, language=None, word_timestamps=True):
        name = Path(path).name
        if not name.startswith("seg"):
            return super().transcribe(path, language, word_timestamps)
        self.clip_calls.append((name, language))
        if self.fail_on_clips:
            raise ve.EngineNotInstalled(self.id, "pip install faster-whisper")
        index = int(name[3:7])
        return {"language": language, "text": self.heard.get(index, ""), "segments": []}


def _dub_chat(shorten_reply: str | None = "kenobi corto", unavailable_on_shorten: bool = False):
    def chat_fn(messages: list[dict[str, Any]]) -> str:
        if "shorten" in messages[0]["content"]:
            if unavailable_on_shorten:
                raise Unavailable("llm", ["gone"])
            return shorten_reply or ""
        user = messages[1]["content"].lower()
        return "hola ahi" if "hello" in user else LONG_LINE
    return chat_fn


def _setup(tmp_path, monkeypatch, stt, chat_fn):
    store = Store(tmp_path / "data")
    video = tmp_path / "clip.mp4"
    _tiny_video(video)
    monkeypatch.setattr(ve, "default_stt_engines", lambda: [stt])
    monkeypatch.setattr(ve, "default_tts_engines", lambda **kw: [FakeTTS()])
    return store, video, FakeBackend(chat_fn)


def _run(store, backend, video, **extra):
    params = {"video_path": str(video), "target_language": "Spanish", "voice": {"engine_id": "fake-tts"}, **extra}
    job = store.create_job("dub", "cpu", params)
    return db.dub_job(store, backend, job, Progress(store, job["id"]))


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_dub_job_records_plan_and_qc_in_manifest(tmp_path, monkeypatch):
    stt = HearingSTT({0: "hola ahi", 1: "algo completamente distinto"})
    store, video, backend = _setup(tmp_path, monkeypatch, stt, _dub_chat())
    outputs = _run(store, backend, video)

    first, second = outputs["segments"]
    assert first["plan"]["class"] == "fits" and "shortened_from" not in first["plan"]
    assert first["qc"]["status"] == "ok" and first["qc"]["wer"] == 0.0 and first["qc"]["flagged"] is False
    # the long line was impossible for its 1.5 s slot and was shortened by the model
    assert second["plan"]["shortened_from"] == LONG_LINE.strip()
    assert second["plan"]["shorten"] == "shortened"
    assert second["translated_text"] == "kenobi corto"
    assert second["plan"]["est_s"] == pytest.approx(len("kenobi corto") / 14.5, abs=0.001)
    assert first["plan"]["slot_s"] == pytest.approx(1.5 + 0.5, abs=0.001)  # 0.5 s of silence follows line 0
    assert second["plan"]["slot_s"] == pytest.approx(1.5, abs=0.001)  # the last line has no gap to borrow
    assert second["qc"]["flagged"] is True and second["qc"]["hyp"] == "algo completamente distinto"
    assert outputs["summary"]["flagged"] == 1 and outputs["summary"]["shortened"] == 1
    assert outputs["summary"]["fits"] == 2
    # QC listens in the target language
    assert {lang for _name, lang in stt.clip_calls} == {"es"}

    manifest = db.load_manifest(tmp_path / "data" / outputs["work_dir"])
    assert manifest["segments"][1]["plan"]["shortened_from"] == LONG_LINE.strip()
    assert manifest["segments"][1]["qc"]["flagged"] is True
    assert manifest["summary"]["flagged"] == 1
    assert manifest["options"] == {"plan": True, "qc": True, "qc_threshold": None, "stt_engine_id": "hearing-stt",
                                   "translation_mode": "adapt", "fit_mode": "audio"}


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_dub_job_without_llm_keeps_impossible_line_and_marks_no_llm(tmp_path, monkeypatch):
    stt = HearingSTT({0: "hola ahi", 1: LONG_LINE})
    store, video, backend = _setup(tmp_path, monkeypatch, stt, _dub_chat(unavailable_on_shorten=True))
    outputs = _run(store, backend, video)
    second = outputs["segments"][1]
    assert second["translated_text"] == LONG_LINE.strip()
    assert second["plan"]["class"] == "impossible"
    assert second["plan"]["shorten"] == "no_llm"
    assert outputs["summary"]["impossible"] == 1 and outputs["summary"]["shortened"] == 0


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_dub_job_missing_transcriber_for_qc_is_skipped_not_failed(tmp_path, monkeypatch):
    stt = HearingSTT(fail_on_clips=True)
    store, video, backend = _setup(tmp_path, monkeypatch, stt, _dub_chat())
    outputs = _run(store, backend, video)
    assert outputs["final_video"]
    for seg in outputs["segments"]:
        assert seg["qc"]["status"] == "skipped" and seg["qc"]["flagged"] is False
        assert "wer" not in seg["qc"]
    assert outputs["summary"]["qc_skipped"] == 2 and outputs["summary"]["flagged"] == 0


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_dub_job_plan_and_qc_can_be_turned_off(tmp_path, monkeypatch):
    stt = HearingSTT()
    store, video, backend = _setup(tmp_path, monkeypatch, stt, _dub_chat())
    outputs = _run(store, backend, video, plan=False, qc=False)
    for seg in outputs["segments"]:
        assert "plan" not in seg and "qc" not in seg
    assert outputs["segments"][1]["translated_text"] == LONG_LINE.strip()
    assert stt.clip_calls == []
    assert outputs["summary"]["fits"] == 0 and outputs["summary"]["flagged"] == 0


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_long_line_borrows_the_gap_before_being_sped_up(tmp_path, monkeypatch):
    # "hola ahi" is 8 chars = 0.2 s with the fake voice: it never needs a gap. A
    # 1.7 s clip in a 1.5 s slot takes 0.2 s of the 0.5 s gap and is not sped up.
    class SlowTTS(FakeTTS):
        def synthesize(self, text, **kw):
            import numpy as np
            return ve.wav_bytes_mono16(np.zeros(int(16000 * 1.7), dtype="float32") + 0.1, 16000)

    stt = HearingSTT({0: "hola ahi", 1: "kenobi corto"})
    store, video, backend = _setup(tmp_path, monkeypatch, stt, _dub_chat())
    monkeypatch.setattr(ve, "default_tts_engines", lambda **kw: [SlowTTS()])
    outputs = _run(store, backend, video)
    fit0 = outputs["segments"][0]["fit"]
    assert fit0["borrowed_gap_s"] == pytest.approx(0.2, abs=0.01)
    assert fit0["applied_factor"] == pytest.approx(1.0, abs=0.01)
    assert fit0["target_duration_s"] == pytest.approx(1.7, abs=0.01)
    unplanned = _run(store, backend, video, plan=False)["segments"][0]["fit"]
    assert unplanned["applied_factor"] > 1.1 and unplanned["borrowed_gap_s"] == 0.0


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_resynthesize_refreshes_plan_and_qc(tmp_path, monkeypatch):
    stt = HearingSTT({0: "hola ahi", 1: "algo distinto"})
    store, video, backend = _setup(tmp_path, monkeypatch, stt, _dub_chat())
    outputs = _run(store, backend, video)
    work_dir = tmp_path / "data" / outputs["work_dir"]
    assert db.load_manifest(work_dir)["segments"][1]["qc"]["flagged"] is True

    stt.heard[1] = "kenobi arreglado"
    result = db.resynthesize_segment(store, backend, work_dir, 1, new_text="kenobi arreglado", remix=False)
    seg = result["segment"]
    assert seg["qc"]["wer"] == 0.0 and seg["qc"]["flagged"] is False
    assert seg["plan"]["est_s"] == pytest.approx(len("kenobi arreglado") / 14.5, abs=0.001)
    assert "shortened_from" not in seg["plan"]  # the text is the person's now
    assert result["summary"]["flagged"] == 0
    manifest = db.load_manifest(work_dir)
    assert manifest["segments"][1]["qc"]["flagged"] is False and manifest["summary"]["flagged"] == 0


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_resynthesize_without_transcriber_marks_qc_skipped(tmp_path, monkeypatch):
    stt = HearingSTT({0: "hola ahi", 1: "kenobi corto"})
    store, video, backend = _setup(tmp_path, monkeypatch, stt, _dub_chat())
    outputs = _run(store, backend, video)
    work_dir = tmp_path / "data" / outputs["work_dir"]
    monkeypatch.setattr(ve, "default_stt_engines", lambda: [])
    result = db.resynthesize_segment(store, backend, work_dir, 0, new_text="saludos", remix=False)
    assert result["segment"]["qc"] == {"status": "skipped", "reason": "no_transcriber", "flagged": False}
    assert result["summary"]["qc_skipped"] == 1


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_resynthesize_old_manifest_without_plan_fields(tmp_path, monkeypatch):
    stt = HearingSTT({0: "hola ahi", 1: "kenobi corto"})
    store, video, backend = _setup(tmp_path, monkeypatch, stt, _dub_chat())
    outputs = _run(store, backend, video)
    work_dir = tmp_path / "data" / outputs["work_dir"]
    manifest = db.load_manifest(work_dir)
    for key in ("options", "summary"):
        manifest.pop(key)
    for seg in manifest["segments"]:
        seg.pop("plan"), seg.pop("qc")
    db._write_manifest(work_dir / "manifest.json", manifest)

    seg = db.resynthesize_segment(store, backend, work_dir, 0, new_text="hola ahi", remix=False)["segment"]
    assert "plan" not in seg  # a job made before the plan existed stays unplanned
    assert seg["qc"]["status"] == "ok"  # but its edited line is still checked


def test_agent_result_is_compact():
    outputs = {"title": "t", "summary": {"fits": 1}, "work_dir": "x", "segments": [{
        "index": 0, "start_s": 0, "end_s": 1, "source_text": "a", "translated_text": "b", "fitted_path": "/p",
        "fit": {"applied_factor": 1.2}, "plan": {"est_s": 1, "slot_s": 1, "class": "fits", "cps": 14.5, "gap_s": 0},
        "qc": {"status": "ok", "metric": "wer", "wer": 0.5, "hyp": "z", "threshold": 0.35, "flagged": True}}]}
    result = db.agent_result(outputs)
    row = result["segments"][0]
    assert row["plan"] == {"est_s": 1, "slot_s": 1, "class": "fits"}
    assert row["qc"] == {"status": "ok", "wer": 0.5, "hyp": "z", "flagged": True}
    assert row["speedup"] == 1.2 and "fitted_path" not in row
    assert result["summary"] == {"fits": 1} and "work_dir" not in result


def test_agent_voice_job_exposes_plan_and_qc_for_a_finished_dub(client):
    c, app, _allowed = client
    store = app.state.store
    job = store.create_job("dub", "cpu", {"video_path": "x", "target_language": "es", "voice": {}})
    store.update_job(job["id"], state="done", progress=1.0, outputs={
        "title": "t", "final_video": "voice_studio/dub/k/dubbed.mp4", "work_dir": "voice_studio/dub/k",
        "summary": {"fits": 0, "tight": 0, "impossible": 1, "shortened": 0, "flagged": 1, "qc_skipped": 0},
        "segments": [{"index": 0, "start_s": 0, "end_s": 1, "source_text": "a", "translated_text": "b",
                      "plan": {"est_s": 3, "slot_s": 1, "class": "impossible", "shorten": "no_llm"},
                      "qc": {"status": "ok", "metric": "wer", "wer": 1.0, "hyp": "q", "flagged": True}}]})
    body = c.get("/api/agent/voice_job", params={"job_id": job["id"]}).json()
    assert body["state"] == "done"
    seg = body["result"]["segments"][0]
    assert seg["plan"]["class"] == "impossible" and seg["plan"]["shorten"] == "no_llm"
    assert seg["qc"]["flagged"] is True and seg["qc"]["hyp"] == "q"
    assert body["result"]["summary"]["flagged"] == 1
    assert "work_dir" not in body["result"]


# -- translation that never happened (seen live: a reasoning model returned nothing and the source was voiced) -----

def test_translate_retries_an_empty_reply_and_marks_untranslated():
    from prosperos_hoard.dubbing import translate_segments
    calls = []

    def chat(messages):
        calls.append(messages)
        return ""  # a model that spends its budget thinking

    report = []
    out = translate_segments(chat, [{"text": "Hola, esto es una prueba."}], "English", "es", report=report)
    assert out == ["Hola, esto es una prueba."]  # the source line is kept...
    assert report == ["untranslated"]  # ...but the job knows it
    assert len(calls) == 2 and "That was not a translation" in calls[1][-1]["content"]


def test_translate_retries_an_echo_and_accepts_the_second_answer():
    from prosperos_hoard.dubbing import translate_segments
    answers = iter(["Hola, esto es una prueba.", "Hello, this is a test."])
    report = []
    out = translate_segments(lambda m: next(answers), [{"text": "Hola, esto es una prueba."}], "en", "es", report=report)
    assert out == ["Hello, this is a test."] and report == ["retried"]


def test_translate_same_language_echo_is_not_untranslated():
    from prosperos_hoard.dubbing import translate_segments
    report = []
    out = translate_segments(lambda m: "Gracias.", [{"text": "Gracias."}], "es", "es", report=report)
    assert out == ["Gracias."] and report == ["ok"]


def test_summary_counts_untranslated_lines():
    from prosperos_hoard.dub_plan import summarize
    s = summarize([{"translation": "untranslated"}, {"translation": "ok"}])
    assert s["untranslated"] == 1


def test_short_clip_is_padded_not_drawled(monkeypatch, tmp_path):
    from prosperos_hoard import dubbing
    seen = {}

    def fake_run(cmd, timeout=None):
        seen["af"] = cmd[cmd.index("-af") + 1]
        out = tmp_path / "out.wav"
        out.write_bytes(b"RIFF")
        return type("P", (), {"returncode": 0, "stderr": b""})()

    monkeypatch.setattr(dubbing.audio_mod, "probe_duration_s", lambda p: 1.5)
    monkeypatch.setattr(dubbing.procutil, "run", fake_run)
    monkeypatch.setattr(dubbing, "_ffmpeg", lambda: "ffmpeg")
    info = dubbing.fit_audio_to_duration(tmp_path / "in.wav", 2.74, tmp_path / "out.wav")
    assert info["applied_factor"] == dubbing.MIN_SLOWDOWN  # eased a little, the rest is silence
    assert info["requested_factor"] < 0.6 and info["clamped"] is True
    assert "apad" in seen["af"] and "atempo=0.92" in seen["af"]
    # a long clip is still sped up as before
    monkeypatch.setattr(dubbing.audio_mod, "probe_duration_s", lambda p: 4.0)
    assert dubbing.fit_audio_to_duration(tmp_path / "in.wav", 2.0, tmp_path / "out.wav")["applied_factor"] == 2.0


def test_qc_skips_a_line_the_transcriber_filters_by_design(monkeypatch, tmp_path):
    from prosperos_hoard import dubbing
    monkeypatch.setattr(dubbing, "_trim_silence", lambda src, dest: src)
    heard_nothing = type("E", (), {"transcribe": lambda self, p, language=None, word_timestamps=False: {"text": ""}})()
    seg = {"translated_text": "Thanks for watching.", "fitted_path": str(tmp_path / "c.wav")}
    dubbing.run_qc(heard_nothing, seg, "en", None)
    assert seg["qc"]["status"] == "skipped" and seg["qc"]["reason"] == "filtered_phrase" and not seg["qc"]["flagged"]
    # an ordinary line heard as nothing is still flagged
    seg = {"translated_text": "The studio checks each line.", "fitted_path": str(tmp_path / "c.wav")}
    dubbing.run_qc(heard_nothing, seg, "en", None)
    assert seg["qc"]["flagged"] is True


def test_qc_transcribes_the_trimmed_clip_and_falls_back(monkeypatch, tmp_path):
    from prosperos_hoard import dubbing
    used = []
    engine = type("E", (), {"transcribe": lambda self, p, language=None, word_timestamps=False: used.append(p) or {"text": "hello there"}})()
    trimmed = tmp_path / "c.qc.wav"
    monkeypatch.setattr(dubbing, "_trim_silence", lambda src, dest: trimmed)
    dubbing.run_qc(engine, {"translated_text": "hello there", "fitted_path": str(tmp_path / "c.wav")}, "en", None)
    assert used == [trimmed]
    # ffmpeg failing leaves the untrimmed clip
    monkeypatch.undo()
    monkeypatch.setattr(dubbing.procutil, "run", lambda cmd, timeout=None: type("P", (), {"returncode": 1, "stderr": b""})())
    monkeypatch.setattr(dubbing, "_ffmpeg", lambda: "ffmpeg")
    src = tmp_path / "c.wav"
    assert dubbing._trim_silence(src, tmp_path / "x.wav") == src
