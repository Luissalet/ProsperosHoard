"""Three-step dub translation (literal, reflect, adapt), the smart fit strategy
and the video retime that goes with it."""

from __future__ import annotations

import json
import shutil
from pathlib import Path
from typing import Any

import pytest

from prosperos_hoard import audio as audio_mod
from prosperos_hoard import dub_plan as dp
from prosperos_hoard import dubbing as db
from prosperos_hoard import procutil
from prosperos_hoard import video_retime as vr
from prosperos_hoard import voice_engines as ve
from prosperos_hoard.backend import ffmpeg_path
from prosperos_hoard.hoard_link.errors import Unavailable
from prosperos_hoard.jobs import Progress
from prosperos_hoard.store import Store

from test_dubbing import FakeBackend, FakeSTT, FakeTTS, _tiny_video  # noqa: E402

HAS_FFMPEG = shutil.which("ffmpeg") is not None or bool(ffmpeg_path())


# ---------------------------------------------------------------- translation --

def _step_of(messages: list[dict[str, Any]]) -> str:
    system = messages[0]["content"]
    if system.startswith("You review"):
        return "reflect"
    if system.startswith("You write the final"):
        return "adapt"
    return "literal"


def routed(literal="hola amigo mio", reflect='{"issues": ["too formal", "too long"]}', adapted="hola colega"):
    """A fake chat_fn that answers each step with its own canned reply (a callable or a string)."""
    calls: list[tuple[str, list[dict[str, Any]]]] = []

    def chat(messages):
        step = _step_of(messages)
        calls.append((step, messages))
        reply = {"literal": literal, "reflect": reflect, "adapt": adapted}[step]
        return reply(messages) if callable(reply) else reply

    chat.calls = calls
    return chat


def _steps(chat):
    return [c[0] for c in chat.calls]


def test_three_steps_happy_path_records_literal_issues_adapted():
    chat = routed()
    report, steps = [], []
    out = db.translate_segments(chat, [{"text": "hello my friend", "char_budget": 12}], "Spanish", "en",
                                report=report, mode="adapt", steps=steps)
    assert out == ["hola colega"] and report == ["ok"]
    assert steps == [{"literal": "hola amigo mio", "issues": ["too formal", "too long"], "adapted": "hola colega"}]
    assert _steps(chat) == ["literal", "reflect", "adapt"]


def test_budget_glossary_and_issues_reach_the_right_prompts():
    chat = routed()
    db.translate_segments(chat, [{"text": "the Force", "char_budget": 17}], "Spanish", "en",
                          glossary={"Force": "la Fuerza"}, mode="adapt", steps=[])
    by_step = {step: msgs for step, msgs in chat.calls}
    for step in ("literal", "reflect", "adapt"):
        assert "Force -> la Fuerza" in by_step[step][0]["content"]
    assert "17 characters" in by_step["reflect"][0]["content"]
    assert "at most 17 characters" in by_step["adapt"][0]["content"]
    assert "hola amigo mio" in by_step["reflect"][1]["content"] and "the Force" in by_step["reflect"][1]["content"]
    assert "- too formal" in by_step["adapt"][1]["content"]
    # the literal step is plain: the source line is the whole user message
    assert by_step["literal"][1]["content"] == "the Force"


def test_garbage_json_in_reflect_means_no_issues_and_adapt_is_skipped_when_it_fits():
    for junk in ["I think it is fine!", "{broken json", "", "null", '{"issues": 5}', "[]"]:
        chat = routed(reflect=junk)
        steps = []
        out = db.translate_segments(chat, [{"text": "hello", "char_budget": 40}], "Spanish", "en", mode="adapt", steps=steps)
        assert out == ["hola amigo mio"], junk
        assert steps[0]["issues"] == [] and steps[0]["adapted"] == "hola amigo mio"
        assert _steps(chat) == ["literal", "reflect"], junk


def test_literal_line_over_budget_is_adapted_even_without_issues():
    chat = routed(reflect='{"issues": []}', adapted="hola")
    steps = []
    out = db.translate_segments(chat, [{"text": "hello", "char_budget": 6}], "Spanish", "en", mode="adapt", steps=steps)
    assert out == ["hola"] and steps[0]["issues"] == [] and steps[0]["adapted"] == "hola"
    assert _steps(chat) == ["literal", "reflect", "adapt"]


def test_reflect_failure_falls_back_to_literal():
    def boom(messages):
        raise RuntimeError("model fell over")

    chat = routed(reflect=boom)
    steps = []
    out = db.translate_segments(chat, [{"text": "hello"}], "Spanish", "en", mode="adapt", steps=steps)
    assert out == ["hola amigo mio"]
    assert steps[0]["issues"] == [] and steps[0]["adapted"] == "hola amigo mio" and steps[0]["fallback"] == "reflect"
    assert _steps(chat) == ["literal", "reflect"]


def test_unavailable_model_after_the_literal_step_keeps_the_literal_line():
    def gone(messages):
        raise Unavailable("llm", ["gone"])

    out = db.translate_segments(routed(adapted=gone), [{"text": "hello"}], "Spanish", "en", mode="adapt", steps=[])
    assert out == ["hola amigo mio"]


@pytest.mark.parametrize("bad", ["", "   ", "hello", '"hello"'])
def test_adapt_failure_or_echo_keeps_literal(bad):
    chat = routed(adapted=bad)
    steps = []
    out = db.translate_segments(chat, [{"text": "hello"}], "Spanish", "en", mode="adapt", steps=steps)
    assert out == ["hola amigo mio"] and steps[0]["adapted"] == "hola amigo mio" and steps[0]["fallback"] == "adapt"


def test_adapt_that_raises_keeps_literal():
    def boom(messages):
        raise RuntimeError("x")

    steps = []
    out = db.translate_segments(routed(adapted=boom), [{"text": "hello"}], "Spanish", "en", mode="adapt", steps=steps)
    assert out == ["hola amigo mio"] and steps[0]["fallback"] == "adapt"


def test_adapt_that_is_longer_and_over_budget_is_not_taken():
    chat = routed(literal="hola", adapted="hola mis queridos amigos de siempre")
    steps = []
    out = db.translate_segments(chat, [{"text": "hello", "char_budget": 10}], "Spanish", "en", mode="adapt", steps=steps)
    assert out == ["hola"] and steps[0]["fallback"] == "adapt"


def test_adapt_wrapped_in_quotes_is_cleaned():
    out = db.translate_segments(routed(adapted='"hola colega"'), [{"text": "hello"}], "Spanish", "en", mode="adapt", steps=[])
    assert out == ["hola colega"]


def test_call_count_is_bounded_per_line():
    chat = routed()
    db.translate_segments(chat, [{"text": f"line {i}"} for i in range(5)], "Spanish", "en", mode="adapt", steps=[])
    assert len(chat.calls) == 5 * 3
    # the literal retry adds one call and nothing else grows
    answers = iter(["", "hola otra vez"])
    chat2 = routed(literal=lambda m: next(answers))
    steps = []
    report = []
    out = db.translate_segments(chat2, [{"text": "hello"}], "Spanish", "en", mode="adapt", steps=steps, report=report)
    assert report == ["retried"] and len(chat2.calls) == 4  # literal, literal retry, reflect, adapt
    assert out == ["hola colega"] and steps[0]["literal"] == "hola otra vez"


def test_untranslated_line_skips_reflect_and_adapt():
    chat = routed(literal="")
    report, steps = [], []
    out = db.translate_segments(chat, [{"text": "hello"}], "Spanish", "en", report=report, mode="adapt", steps=steps)
    assert out == ["hello"] and report == ["untranslated"]
    assert steps == [{"literal": "", "issues": [], "adapted": ""}]
    assert _steps(chat) == ["literal", "literal"]  # the literal call and its retry, nothing more


def test_unavailable_in_the_literal_step_propagates_in_adapt_mode():
    def gone(messages):
        raise Unavailable("llm", ["no local LLM reachable"])

    with pytest.raises(Unavailable):
        db.translate_segments(gone, [{"text": "hi"}], "Spanish", mode="adapt", steps=[])


def test_direct_mode_is_one_call_per_line_and_records_no_steps():
    calls = []

    def chat(messages):
        calls.append(messages)
        return "hola"

    steps = []
    assert db.translate_segments(chat, [{"text": "hi"}, {"text": "bye"}], "Spanish", mode="direct", steps=steps) == ["hola", "hola"]
    assert len(calls) == 2 and steps == [None, None]
    assert "dubbing" in calls[0][0]["content"]  # the original single-call prompt


def test_lines_keep_their_order_and_own_budgets():
    seen = []

    def chat(messages):
        step = _step_of(messages)
        if step == "literal":
            return "T:" + messages[1]["content"]
        if step == "reflect":
            seen.append(messages[0]["content"])
            return '{"issues": ["x"]}'
        return "A:" + messages[1]["content"].split("\n")[0].split(": ", 1)[1]

    out = db.translate_segments(chat, [{"text": "a", "char_budget": 10}, {"text": "b", "char_budget": 20}], "Spanish", "en",
                                mode="adapt", steps=[])
    assert out == ["A:a", "A:b"]
    assert "10 characters" in seen[0] and "20 characters" in seen[1]


def test_parse_issues_is_tolerant():
    assert db.parse_issues('{"issues": ["a", "b"]}') == ["a", "b"]
    assert db.parse_issues('```json\n{"issues": ["a"]}\n```') == ["a"]
    assert db.parse_issues('Sure! {"issues": ["tone"]} Hope it helps') == ["tone"]
    assert db.parse_issues('["x", "y"]') == ["x", "y"]
    assert db.parse_issues('{"issues": "just one"}') == ["just one"]
    assert db.parse_issues('{"issues": ["a", "", "  "]}') == ["a"]
    assert len(db.parse_issues(json.dumps({"issues": [f"i{n}" for n in range(20)]}))) == db.MAX_ISSUES
    assert db.parse_issues(None) == [] and db.parse_issues("nope") == []


def test_translation_mode_resolution():
    chat = lambda m: ""
    assert db.resolve_translation_mode(None, chat) == "adapt"
    assert db.resolve_translation_mode("direct", chat) == "direct"
    assert db.resolve_translation_mode("ADAPT", chat) == "adapt"
    assert db.resolve_translation_mode("weird", chat) == "adapt"
    assert db.resolve_translation_mode("adapt", None) == "direct"  # no model: nothing to reflect with


# ------------------------------------------------------------------ fit math --

def test_fit_strategy_fits_changes_nothing():
    st = dp.fit_strategy(1.0, 1.5, 0.5)
    assert st["kind"] == "fits" and st["borrow_gap_s"] == 0 and st["audio_factor"] == 1.0 and st["video_factor"] == 1.0
    assert st["trim_s"] == 0 and st["window_s"] == 1.5


def test_fit_strategy_uses_the_gap_first():
    st = dp.fit_strategy(1.8, 1.5, 0.5)
    assert st["kind"] == "gap" and st["borrow_gap_s"] == pytest.approx(0.3)
    assert st["audio_factor"] == 1.0 and st["video_factor"] == 1.0 and st["window_s"] == pytest.approx(1.8)
    # only as much as the clip needs, and no more than the gap offers
    assert dp.fit_strategy(5.0, 1.5, 0.5)["borrow_gap_s"] == 0.5


def test_fit_strategy_soft_range_speeds_only_the_audio():
    st = dp.fit_strategy(2.2, 1.5, 0.5)  # window 2.0, needs 1.1
    assert st["kind"] == "audio" and st["audio_factor"] == pytest.approx(1.1) and st["video_factor"] == 1.0
    edge = dp.fit_strategy(2.4, 1.5, 0.5)  # exactly soft_audio
    assert edge["kind"] == "audio" and edge["audio_factor"] == pytest.approx(1.2)
    assert edge["video_factor"] == 1.0 and edge["trim_s"] == 0


def test_fit_strategy_just_beyond_soft_does_not_cut_the_video_for_a_sliver():
    st = dp.fit_strategy(2.5, 1.5, 0.5)  # need 1.25: the video share would be ~2%
    assert st["kind"] == "audio" and st["video_factor"] == 1.0 and st["audio_factor"] == pytest.approx(1.25)


def test_fit_strategy_splits_the_remaining_need_by_square_root():
    st = dp.fit_strategy(3.0, 1.5, 0.5)  # window 2.0, need 1.5
    assert st["kind"] == "audio+video"
    assert st["audio_factor"] == pytest.approx(1.2 * (1.5 / 1.2) ** 0.5, abs=1e-3)
    assert st["audio_factor"] * st["video_factor"] == pytest.approx(1.5, abs=2e-3)
    assert 1.05 < st["video_factor"] < 1.2 and st["trim_s"] == 0
    # the clip, sped up and given the slowed window, ends where the window ends
    assert 3.0 / st["audio_factor"] == pytest.approx(st["window_s"] * st["video_factor"], abs=0.01)


def test_fit_strategy_respects_the_caps_and_trims_the_rest():
    st = dp.fit_strategy(10.0, 1.5, 0.0)  # need 6.67
    assert st["audio_factor"] == 1.5 and st["video_factor"] == 2.0
    assert st["kind"] == "trimmed" and st["trim_s"] == pytest.approx(10.0 / 1.5 - 1.5 * 2.0, abs=0.01)
    # a stricter cap is honoured
    tight = dp.fit_strategy(4.0, 1.5, 0.0, audio_max=1.3, video_max=1.4)
    assert tight["audio_factor"] <= 1.3 and tight["video_factor"] <= 1.4 and tight["trim_s"] > 0


def test_fit_strategy_moderate_need_at_the_cap_does_not_trim():
    st = dp.fit_strategy(4.5, 1.5, 0.0)  # need 3.0: audio 1.5 (capped), video 2.0 -> exactly 3.0
    assert st["audio_factor"] == pytest.approx(1.5) and st["video_factor"] == pytest.approx(2.0) and st["trim_s"] == 0


def test_fit_strategy_is_continuous_across_the_soft_limit():
    below = dp.fit_strategy(2.399, 1.5, 0.5)["audio_factor"]
    above = dp.fit_strategy(2.6, 1.5, 0.5)
    assert below == pytest.approx(1.2, abs=0.01) and above["audio_factor"] >= below


def test_summarize_retime_counts_stretched_lines():
    segs = [{"fit": {"strategy": {"video_factor": 1.5, "window_s": 2.0}}},
            {"fit": {"strategy": {"video_factor": 1.0, "window_s": 1.0}}},
            {"fit": {}}, {}]
    assert dp.summarize_retime(segs) == {"retimed": 1, "added_s": 1.0}


# ---------------------------------------------------------------- retime math --

def test_build_chunks_covers_the_source_and_tracks_the_new_timeline():
    chunks = vr.build_chunks([{"start_s": 3, "end_s": 4, "factor": 1.5}, {"start_s": 1, "end_s": 2, "factor": 2.0}], 6.0)
    assert [(c["src_start"], c["src_end"], c["factor"]) for c in chunks] == [
        (0, 1, 1.0), (1, 2, 2.0), (2, 3, 1.0), (3, 4, 1.5), (4, 6, 1.0)]
    assert [(c["out_start"], c["out_end"]) for c in chunks] == [(0, 1), (1, 3), (3, 4), (4, 5.5), (5.5, 7.5)]
    assert vr.new_duration(chunks) == 7.5 and vr.added_seconds(chunks) == pytest.approx(1.5)
    assert vr.needs_retime(chunks)


def test_build_chunks_ignores_tiny_and_overlapping_ranges():
    chunks = vr.build_chunks([
        {"start_s": 1, "end_s": 2, "factor": 1.02},      # below the minimum factor
        {"start_s": 2, "end_s": 2.01, "factor": 2.0},    # too short
        {"start_s": 3, "end_s": 5, "factor": 2.0},
        {"start_s": 4, "end_s": 6, "factor": 1.5},       # overlaps: starts where the previous ended
        {"start_s": 9, "end_s": 12, "factor": 2.0},      # past the end of the video: clamped away
    ], 7.0)
    assert [(c["src_start"], c["src_end"], c["factor"]) for c in chunks] == [(0, 3, 1.0), (3, 5, 2.0), (5, 6, 1.5), (6, 7, 1.0)]
    for a, b in zip(chunks, chunks[1:]):
        assert a["src_end"] == b["src_start"] and a["out_end"] == pytest.approx(b["out_start"])


def test_build_chunks_without_retimes_is_one_untouched_chunk():
    chunks = vr.build_chunks([], 5.0)
    assert len(chunks) == 1 and not vr.needs_retime(chunks) and vr.new_duration(chunks) == 5.0


def test_build_chunks_range_touching_the_end_leaves_no_zero_chunk():
    chunks = vr.build_chunks([{"start_s": 4, "end_s": 5, "factor": 2.0}], 5.0)
    assert [(c["src_start"], c["src_end"]) for c in chunks] == [(0, 4), (4, 5)] and vr.new_duration(chunks) == 6.0


def test_map_time_shifts_everything_after_a_stretch():
    chunks = vr.build_chunks([{"start_s": 1, "end_s": 2, "factor": 2.0}, {"start_s": 3, "end_s": 4, "factor": 1.5}], 6.0)
    assert vr.map_time(chunks, 0.5) == 0.5
    assert vr.map_time(chunks, 1.0) == 1.0
    assert vr.map_time(chunks, 1.5) == 2.0          # halfway through a 2x stretch
    assert vr.map_time(chunks, 2.0) == 3.0
    assert vr.map_time(chunks, 3.0) == 4.0          # first shift of 1.0 s
    assert vr.map_time(chunks, 4.0) == 5.5          # plus 0.5 s
    assert vr.map_time(chunks, 6.0) == 7.5
    assert vr.map_time(chunks, 7.0) == 8.5          # past the end: normal speed
    assert vr.map_time([], 3.0) == 3.0


def test_batches_split_the_chunk_list():
    chunks = vr.build_chunks([{"start_s": i * 2.0, "end_s": i * 2.0 + 1.0, "factor": 1.5} for i in range(30)], 70.0)
    assert len(chunks) == 60
    groups = vr.batches(chunks, 40)
    assert [len(g) for g in groups] == [40, 20]
    assert [c for g in groups for c in g] == chunks
    assert vr.batches(chunks, 0)[0] == chunks[:1]  # a silly size is clamped to 1


def test_filter_graphs_are_relative_to_the_batch_and_slow_with_setpts_and_atempo():
    chunks = vr.build_chunks([{"start_s": 1, "end_s": 2, "factor": 2.0}], 3.0)
    vf = vr.video_filter(chunks, fps=25.0)
    assert "trim=start=0.0000:end=1.0000" in vf and "setpts=PTS*2.000000" in vf and "fps=25.000000" in vf
    assert "concat=n=3:v=1:a=0" in vf and "tpad=stop_mode=clone" in vf and "trim=end=4.0000" in vf
    af = vr.audio_filter(chunks)
    assert "atempo=0.500000" in af and "concat=n=3:v=0:a=1" in af and "atrim=end=4.0000" in af
    # a later batch reads from its own first chunk
    later = vr.video_filter(chunks[1:], None)
    assert "trim=start=0.0000:end=1.0000" in later and "trim=start=1.0000:end=2.0000" in later and "fps=" not in later


def test_chunks_key_changes_with_the_chunks():
    a = vr.build_chunks([{"start_s": 1, "end_s": 2, "factor": 2.0}], 5.0)
    b = vr.build_chunks([{"start_s": 1, "end_s": 2, "factor": 1.5}], 5.0)
    assert vr.chunks_key(a) == vr.chunks_key(list(a)) and vr.chunks_key(a) != vr.chunks_key(b)


# ------------------------------------------------------------------ real ffmpeg --

def _synthetic_clip(path: Path, seconds: float = 6.0) -> None:
    cmd = [ffmpeg_path(), "-y", "-loglevel", "error", "-f", "lavfi", "-i", f"testsrc=duration={seconds}:size=160x90:rate=25",
           "-f", "lavfi", "-i", f"sine=frequency=440:duration={seconds}", "-shortest", "-c:v", "libx264", "-pix_fmt", "yuv420p",
           "-c:a", "aac", str(path)]
    proc = procutil.run(cmd, timeout=120)
    assert proc.returncode == 0, proc.stderr


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
@pytest.mark.parametrize("batch_size", [40, 2])
def test_retime_video_and_audio_have_the_expected_duration(tmp_path, batch_size):
    src = tmp_path / "in.mp4"
    _synthetic_clip(src)
    chunks = vr.build_chunks([{"start_s": 1, "end_s": 2, "factor": 2.0}, {"start_s": 3, "end_s": 4, "factor": 1.5}], 6.0)
    expected = vr.new_duration(chunks)
    assert expected == pytest.approx(7.5)
    out_v, out_a = tmp_path / "out.mp4", tmp_path / "out.wav"
    vr.retime_video(ffmpeg_path(), src, chunks, out_v, fps=25.0, batch_size=batch_size)
    vr.retime_audio(ffmpeg_path(), src, chunks, out_a, batch_size=batch_size)
    assert audio_mod.probe_duration_s(out_v) == pytest.approx(expected, abs=0.25)
    assert audio_mod.probe_duration_s(out_a) == pytest.approx(expected, abs=0.1)
    # the batches left nothing behind
    assert sorted(p.name for p in tmp_path.iterdir()) == ["in.mp4", "out.mp4", "out.wav"]


# ------------------------------------------------------------------- job level --

class LongTTS(FakeTTS):
    """Every line takes 3 s to say, far more than the 1.5 s slots of the fake transcript."""

    def synthesize(self, text, **kw):
        import numpy as np
        return ve.wav_bytes_mono16(np.sin(np.linspace(0, 400, 16000 * 3)).astype("float32") * 0.2, 16000)


def _job(tmp_path, monkeypatch, tts, **params):
    store = Store(tmp_path / "data")
    video = tmp_path / "clip.mp4"
    _tiny_video(video)  # 4 s
    monkeypatch.setattr(ve, "default_stt_engines", lambda: [FakeSTT()])
    monkeypatch.setattr(ve, "default_tts_engines", lambda **kw: [tts])
    chat = routed(literal="hola", reflect='{"issues": []}')
    job = store.create_job("dub", "cpu", {"video_path": str(video), "target_language": "Spanish",
                                          "voice": {"engine_id": "fake-tts"}, "qc": False, **params})
    return store, FakeBackend(chat), job, chat


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_job_in_adapt_mode_stores_translation_steps(tmp_path, monkeypatch):
    store, backend, job, chat = _job(tmp_path, monkeypatch, FakeTTS())
    out = db.dub_job(store, backend, job, Progress(store, job["id"]))
    seg = out["segments"][0]
    assert seg["translation_steps"] == {"literal": "hola", "issues": [], "adapted": "hola"}
    assert seg["translated_text"] == "hola"
    manifest = db.load_manifest(tmp_path / "data" / out["work_dir"])
    assert manifest["options"]["translation_mode"] == "adapt" and manifest["options"]["fit_mode"] == "audio"
    assert all(s["fit"].get("strategy") is None for s in out["segments"])
    # the reflect prompt carried the line's character budget from the plan
    reflect = [m for step, m in chat.calls if step == "reflect"][0]
    assert "characters" in reflect[0]["content"]


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_job_in_direct_mode_makes_one_call_per_line(tmp_path, monkeypatch):
    store, backend, job, chat = _job(tmp_path, monkeypatch, FakeTTS(), translation_mode="direct")
    out = db.dub_job(store, backend, job, Progress(store, job["id"]))
    assert len(chat.calls) == 2 and all("translation_steps" not in s for s in out["segments"])
    assert db.load_manifest(tmp_path / "data" / out["work_dir"])["options"]["translation_mode"] == "direct"


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_smart_fit_slows_the_picture_and_shifts_the_timeline(tmp_path, monkeypatch):
    store, backend, job, _chat = _job(tmp_path, monkeypatch, LongTTS(), fit_mode="smart")
    out = db.dub_job(store, backend, job, Progress(store, job["id"]))
    first, second = out["segments"]
    s0, s1 = first["fit"]["strategy"], second["fit"]["strategy"]
    assert s0["kind"] == "audio+video" and s0["borrow_gap_s"] == pytest.approx(0.5)
    assert s1["borrow_gap_s"] == 0.0 and s1["video_factor"] > 1.0   # the last line has no gap to borrow
    assert first["retime"]["factor"] == s0["video_factor"] and first["retime"]["end_s"] == pytest.approx(2.0)
    added = 2.0 * (s0["video_factor"] - 1) + 1.5 * (s1["video_factor"] - 1)
    assert out["summary"]["retimed"] == 2 and out["summary"]["added_s"] == pytest.approx(added, abs=0.01)
    assert out["retimed"] is True
    # everything after the first stretch moved later
    assert first["timeline"]["start_s"] == 0.0
    assert second["timeline"]["start_s"] == pytest.approx(2.0 + 2.0 * (s0["video_factor"] - 1), abs=0.01)
    srt = (tmp_path / "data" / out["subtitles"]).read_text(encoding="utf-8")
    assert f"00:00:{second['timeline']['start_s']:06.3f}".replace(".", ",") in srt  # the cue follows the shifted picture
    final = tmp_path / "data" / out["final_video"]
    assert audio_mod.probe_duration_s(final) == pytest.approx(4.0 + added, abs=0.35)
    # the retimed media is reused by a remix that changes nothing in the timeline
    work = tmp_path / "data" / out["work_dir"]
    stamp = (work / "retimed.key").read_text(encoding="utf-8")
    mtime = (work / "retimed.mp4").stat().st_mtime_ns
    result = db.resynthesize_segment(store, backend, work, 0, new_text="hola de nuevo")
    assert (work / "retimed.key").read_text(encoding="utf-8") == stamp
    assert (work / "retimed.mp4").stat().st_mtime_ns == mtime
    assert result["summary"]["retimed"] == 2
    assert "translation_steps" not in result["segment"]


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_audio_fit_mode_leaves_the_picture_alone(tmp_path, monkeypatch):
    store, backend, job, _chat = _job(tmp_path, monkeypatch, LongTTS())
    out = db.dub_job(store, backend, job, Progress(store, job["id"]))
    assert "retimed" not in out and "retimed" not in out["summary"]
    assert all("retime" not in s and "timeline" not in s for s in out["segments"])
    assert audio_mod.probe_duration_s(tmp_path / "data" / out["final_video"]) == pytest.approx(4.0, abs=0.3)


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_smart_fit_with_clips_that_fit_changes_no_video(tmp_path, monkeypatch):
    store, backend, job, _chat = _job(tmp_path, monkeypatch, FakeTTS(), fit_mode="smart")
    out = db.dub_job(store, backend, job, Progress(store, job["id"]))
    assert out["summary"]["retimed"] == 0 and out["summary"]["added_s"] == 0 and "retimed" not in out
    assert not (tmp_path / "data" / out["work_dir"] / "retimed.mp4").exists()
    assert out["segments"][0]["fit"]["strategy"]["kind"] == "fits"


def test_agent_result_shows_issues_and_fit_strategy():
    outputs = {"title": "t", "summary": {}, "segments": [{
        "index": 0, "start_s": 0, "end_s": 1, "source_text": "a", "translated_text": "b",
        "translation_steps": {"literal": "x", "issues": ["too long"], "adapted": "b"},
        "fit": {"applied_factor": 1.3, "strategy": {"kind": "audio+video", "audio_factor": 1.3, "video_factor": 1.2,
                                                    "trim_s": 0.0, "window_s": 2.0, "borrow_gap_s": 0.5}}}]}
    row = db.agent_result(outputs)["segments"][0]
    assert row["issues"] == ["too long"]
    assert row["fit"] == {"kind": "audio+video", "audio_factor": 1.3, "video_factor": 1.2, "trim_s": 0.0}


# ------------------------------------------------------------------ api / mcp --

def test_dub_params_only_carry_modes_that_differ_from_the_defaults(client):
    c, app, allowed = client
    clip = allowed / "clip.mp4"
    clip.write_bytes(b"not really a video")
    body = {"source_path": str(clip), "target_language": "es", "voice": {"engine_id": "fake-tts"}}

    def params_of(extra):
        resp = c.post("/api/voice/dub", json={**body, **extra})
        assert resp.status_code == 200, resp.text
        return app.state.store.get_job(resp.json()["job"]["id"])["params"]

    default = params_of({})
    assert "translation_mode" not in default and "fit_mode" not in default
    explicit_default = params_of({"translation_mode": "adapt", "fit_mode": "audio"})
    assert "translation_mode" not in explicit_default and "fit_mode" not in explicit_default
    changed = params_of({"translation_mode": "direct", "fit_mode": "smart"})
    assert changed["translation_mode"] == "direct" and changed["fit_mode"] == "smart"
    assert db._content_key({"params": default}) != db._content_key({"params": changed})  # a different work directory
    assert c.post("/api/voice/dub", json={**body, "fit_mode": "turbo"}).status_code == 422
    assert c.post("/api/voice/dub", json={**body, "translation_mode": "magic"}).status_code == 422


def test_mcp_voice_dub_exposes_the_new_modes():
    import importlib

    mcp_server = importlib.import_module("prosperos_hoard.mcp_server")
    tool = mcp_server.mcp._tool_manager.get_tool("voice_dub")
    props = tool.parameters["properties"]
    assert props["translation_mode"]["enum"] == ["direct", "adapt"] and props["translation_mode"]["default"] == "adapt"
    assert props["fit_mode"]["enum"] == ["audio", "smart"] and props["fit_mode"]["default"] == "audio"
    assert len(tool.description.splitlines()[0]) <= 110
