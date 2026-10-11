"""Word retakes in context with AuK: text matching, the instruction, setup discovery, choosing the take that says the
new words, splicing only them (bit-exact outside the cut) and the API / MCP faces. AuK itself never runs here."""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np
import pytest

from prosperos_hoard import auk_retake as ar
from prosperos_hoard import voice_engines as ve
from prosperos_hoard import voice_retake as vr

SR = 16000


def _tones(freqs, sr=SR, word_s=0.4, gap_s=0.2):
    parts, words, t = [], [], 0.0
    for i, f in enumerate(freqs):
        parts += [np.zeros(int(gap_s * sr), dtype=np.float32),
                  (0.3 * np.sin(2 * np.pi * f * np.arange(int(word_s * sr)) / sr)).astype(np.float32)]
        t += gap_s
        words.append({"start_s": round(t, 3), "end_s": round(t + word_s, 3), "word": f"w{i}"})
        t += word_s
    parts.append(np.zeros(int(gap_s * sr), dtype=np.float32))
    return np.concatenate(parts), words


LINE_WORDS = ["Hay", "diecisiete", "grados", "con", "sol."]


def _line():
    x, words = _tones((200, 300, 400, 500, 600))
    for w, text in zip(words, LINE_WORDS):
        w["word"] = text
    return x, words


def test_tokens_spell_numbers_and_drop_accents():
    assert ar.tokens("Hay 18 grados, ¡con Sól!") == ["hay", "dieciocho", "grados", "con", "sol"]
    assert ar.tokens("21 35 100 101 1999") == ["veintiuno", "treinta", "y", "cinco", "cien", "ciento", "uno",
                                               "mil", "novecientos", "noventa", "y", "nueve"]
    assert ar.tokens("It is 42", "en") == ["it", "is", "forty", "two"]


def test_find_span_exact_neighbours_and_none():
    heard = [{"word": w} for w in ["Hay", "18", "grados", "con", "sol."]]
    assert ar.find_span(heard, "dieciocho")[:2] == (1, 1)
    with_neighbours = ar.find_span(heard, "dieciocho", before="Hay", after="grados")
    assert with_neighbours[:2] == (1, 1) and with_neighbours[2] == pytest.approx(1.1)
    assert ar.find_span(heard, "veinte grados") is None
    assert ar.find_span([], "x") is None
    assert ar.find_span([{"word": "Dieciocho"}, {"word": "GRADOS"}], "18 grados")[:2] == (0, 1)


def test_find_span_never_takes_another_name_or_number():
    """Review of r1: fuzzy matching accepted «María» for «Marta» and «veintiuno» for «veinticinco»."""
    assert ar.find_span([{"word": "María"}], "Marta", "es") is None
    assert ar.find_span([{"word": "Marta,"}], "marta", "es")[:2] == (0, 0)
    assert ar.find_span([{"word": "veintiuno"}], "veinticinco", "es") is None
    assert ar.find_span([{"word": "21"}], "veinticinco", "es") is None
    assert ar.find_span([{"word": "25"}], "veinticinco", "es")[:2] == (0, 0)
    assert ar.find_span([{"word": "diciocho"}], "dieciocho") is None


def test_plan_quotes_the_phrase_with_its_neighbours_and_scales_the_length():
    _, words = _line()
    p = ar.plan(words, 1, 1, "dieciocho", 3.0)
    assert p["instruction"] == "Replace 'Hay diecisiete grados' with 'Hay dieciocho grados'."
    assert p["expected_line"] == "Hay dieciocho grados con sol."
    assert p["gen_seconds"] == pytest.approx(3.0 * 24 / 25, abs=0.01)                   # spoken letters, not characters
    assert p["expected_new_s"] == pytest.approx(2.0 / 25 * 9, abs=0.01)                  # the line's own pace
    assert ar.plan(words, 0, 0, "x" * 400, 3.0)["gen_seconds"] == pytest.approx(5.4)      # capped at 1.8x
    assert "’" in ar.plan(words, 4, 4, "l'aube", 3.0)["instruction"]                 # no stray quote
    words[1]["word"] = "17"                                                               # as the transcriber writes it
    p = ar.plan(words, 1, 1, "dieciocho", 3.0)
    assert p["instruction"] == "Replace 'Hay diecisiete grados' with 'Hay dieciocho grados'."
    assert p["gen_seconds"] == pytest.approx(3.0 * 24 / 25, abs=0.01)


def test_spoken_and_piece_fits():
    assert ar.spoken("Hay 17 grados, 35 sol") == "Hay diecisiete grados, treinta y cinco sol"
    assert ar.spoken("at 42", "en") == "at forty two"
    assert ar.piece_fits(0.7, 0.72) and not ar.piece_fits(2.6, 0.94) and not ar.piece_fits(0.1, 0.72)
    assert ar.piece_fits(1.1, 0.3)                                                        # short words get 1.2 s of room


def _install(home: Path, flash=True) -> None:
    (home / ".venv" / "Scripts").mkdir(parents=True)
    (home / ".venv" / "Scripts" / "python.exe").write_bytes(b"")
    (home / "AuK" / "src" / "auk").mkdir(parents=True)
    ck = home / "AuK-Flash"
    ck.mkdir()
    (ck / ("auk_flash.safetensors" if flash else "auk_base.safetensors")).write_bytes(b"")
    (ck / "config.yaml").write_text("x")
    (home / "Qwen2.5-Omni-3B").mkdir()
    (home / "Qwen2.5-Omni-3B" / "config.json").write_text("{}")


def test_discover_reports_what_is_missing_and_finds_a_complete_layout(tmp_path):
    setup, status = ar.discover(tmp_path, env={})
    assert setup is None and not status["installed"] and "Python environment" in status["reason"]
    _install(tmp_path / "engines" / "auk")
    setup, status = ar.discover(tmp_path, env={})
    assert status["installed"] and setup.flash and setup.nfe == 4 and status["variant"] == "AuK-Flash"
    base = tmp_path / "base"
    _install(base, flash=False)
    setup, status = ar.discover(tmp_path, env={"PROSPERO_AUK_HOME": str(base),
                                               "PROSPERO_AUK_CKPT": str(base / "AuK-Flash" / "auk_base.safetensors")})
    assert status["installed"] and setup.nfe == 32 and status["variant"] == "AuK"


def _fake_runner(takes):
    """A runner that writes one candidate per seed: takes[seed] = (frequencies, transcript words)."""
    calls = []

    def run(setup, job, work_dir, gpu=None):
        calls.append({"job": job, "gpu": gpu})
        cands = []
        for seed in job["seeds"]:
            x, _ = _tones(takes[seed][0], sr=24000)
            f = work_dir / f"auk-seed{seed}.wav"
            f.write_bytes(ve.wav_bytes_mono16(x, 24000))
            cands.append({"seed": seed, "file": str(f), "gen_s": 0.1, "sample_rate": 24000})
        return {"ok": True, "load_s": 1.0, "peak_vram_gib": 8.9, "candidates": cands}
    run.calls = calls
    return run


def _heard(takes):
    def transcribe(path: Path):
        seed = int(path.stem.replace("auk-seed", ""))
        _, words = _tones(takes[seed][0], sr=24000)
        for w, text in zip(words, takes[seed][1]):
            w["word"] = text
        return words
    return transcribe


SETUP = ar.AukSetup(python=Path("py"), repo=Path("repo"), ckpt=Path("auk_flash.safetensors"), qwen=Path("q"))


def test_retake_picks_the_take_that_says_the_new_words_and_keeps_the_rest_bit_for_bit(tmp_path):
    x, words = _line()
    original = ve.wav_bytes_mono16(x, SR)
    takes = {1: ((200, 900, 400, 500, 600), ["Hay", "diecisiocho", "yaugaros", "con", "sol."]),
             2: ((200, 950, 400, 500, 600), ["Hay", "18", "grados", "con", "sol."]),
             3: ((200, 990, 400, 500, 600), ["A", "ver", "8", "grados", "sol."])}
    runner = _fake_runner(takes)
    out = ar.retake_with_auk(original, words, 1, 1, "dieciocho", setup=SETUP, transcribe=_heard(takes),
                             work_dir=tmp_path / "w", runner=runner, gpu=2)
    job = runner.calls[0]["job"]
    assert job["instruction"] == "Replace 'Hay diecisiete grados' with 'Hay dieciocho grados'." and job["seeds"] == [1, 2, 3]
    assert runner.calls[0]["gpu"] == 2 and Path(job["audio"]).is_file()
    auk = out["report"]["auk"]
    assert auk["chosen_seed"] == 2 and auk["variant"] == "AuK-Flash" and auk["nfe"] == 4
    assert [c["matched"] for c in auk["candidates"]] == [False, True, False]  # «diecisiocho» is not «dieciocho»
    new, _ = vr.decode_wav(out["wav"])
    orig, _ = vr.decode_wav(original)
    cut0 = int(round(out["report"]["cut_start_s"] * SR)) - int(vr.CROSSFADE_S * SR)
    assert np.array_equal(new[:cut0], orig[:cut0])                          # before the cut: untouched
    tail = len(orig) - int(round(out["report"]["cut_end_s"] * SR)) - int(vr.CROSSFADE_S * SR)
    assert np.array_equal(new[-tail:], orig[-tail:])                        # after the cut: untouched
    assert out["report"]["replaced"] == "diecisiete" and out["words"][1]["word"] == "dieciocho"
    # the piece really comes from take 2: its 950 Hz tone, not the TTS path
    mid = new[int(0.95 * SR):int(1.05 * SR), 0]
    spectrum = np.abs(np.fft.rfft(mid))
    assert abs(np.argmax(spectrum) * SR / len(mid) - 950) < 15


def test_retake_without_a_take_that_says_the_words_fails_with_what_was_heard(tmp_path):
    x, words = _line()
    takes = {1: ((200, 900, 400, 500, 600), ["Hay", "veinte", "grados", "con", "sol."])}
    with pytest.raises(vr.RetakeError) as err:
        ar.retake_with_auk(ve.wav_bytes_mono16(x, SR), words, 1, 1, "dieciocho", setup=SETUP, transcribe=_heard(takes),
                           work_dir=tmp_path / "w", runner=_fake_runner(takes), seeds=(1,))
    assert err.value.code == "auk_no_match" and "veinte" in err.value.message


def test_a_take_whose_new_word_swallows_the_rest_of_the_line_is_not_used(tmp_path):
    """Live case: the transcriber heard «Hi, 18°C.» with «18°C.» timed over the rest of the line; splicing that
    stretch would repeat «grados con sol»."""
    x, words = _line()
    merged_x, _ = _tones((200, 950), sr=24000, word_s=2.6)
    takes = {1: ((200, 950, 400, 500, 600), ["Hay", "18", "grados", "con", "sol."])}

    def runner(setup, job, work_dir, gpu=None):
        f1 = work_dir / "auk-seed1.wav"
        f2 = work_dir / "auk-seed2.wav"
        f1.write_bytes(ve.wav_bytes_mono16(merged_x, 24000))
        f2.write_bytes(ve.wav_bytes_mono16(_tones(takes[1][0], sr=24000)[0], 24000))
        return {"ok": True, "candidates": [{"seed": 1, "file": str(f1)}, {"seed": 2, "file": str(f2)}]}

    def transcribe(path):
        if path.stem == "auk-seed1":
            return [{"start_s": 0.2, "end_s": 0.4, "word": "Hi,"}, {"start_s": 0.6, "end_s": 3.2, "word": "18°C."}]
        return _heard({2: takes[1]})(path)

    out = ar.retake_with_auk(ve.wav_bytes_mono16(x, SR), words, 1, 1, "dieciocho", setup=SETUP, transcribe=transcribe,
                             work_dir=tmp_path / "w", runner=runner, seeds=(1, 2))
    seed1 = out["report"]["auk"]["candidates"][0]
    assert seed1["matched"] and seed1["fits"] is False and out["report"]["auk"]["chosen_seed"] == 2
    with pytest.raises(vr.RetakeError) as err:
        ar.retake_with_auk(ve.wav_bytes_mono16(x, SR), words, 1, 1, "dieciocho", setup=SETUP,
                           transcribe=lambda p: transcribe(tmp_path / "w2" / "auk-seed1.wav"),
                           work_dir=tmp_path / "w2", runner=runner, seeds=(1,))
    assert err.value.code == "auk_no_match" and "expected ~" in err.value.message


def test_bad_range_fails_before_running_auk(tmp_path):
    x, words = _line()
    runner = _fake_runner({})
    with pytest.raises(vr.RetakeError) as err:
        ar.retake_with_auk(ve.wav_bytes_mono16(x, SR), words, 3, 9, "x", setup=SETUP, transcribe=lambda p: [],
                           work_dir=tmp_path / "w", runner=runner)
    assert err.value.code == "bad_word_range" and runner.calls == []


def test_run_worker_reads_the_result_and_reports_failures(tmp_path, monkeypatch):
    seen = {}

    def fake_run(cmd, cwd, env, stdout, stderr, timeout, creationflags, check):
        seen.update(cmd=cmd, env=env)
        job = json.loads(Path(cmd[-1]).read_text(encoding="utf-8"))
        Path(job["result"]).write_text(json.dumps({"ok": job["nfe"] == 4, "error": "boom", "candidates": []}))
    monkeypatch.setattr(ar.subprocess, "run", fake_run)
    r = ar.run_worker(SETUP, {"seeds": [1]}, tmp_path / "w", gpu=3)
    assert r["ok"] and seen["env"]["CUDA_VISIBLE_DEVICES"] == "3" and seen["env"]["CUDA_DEVICE_ORDER"] == "PCI_BUS_ID"
    assert seen["cmd"][2] == "utf8" and seen["cmd"][3].endswith("auk_worker.py")
    with pytest.raises(vr.RetakeError) as err:
        ar.run_worker(SETUP, {"seeds": [1], "nfe": 32}, tmp_path / "w2")
    assert err.value.code == "auk_failed" and "boom" in err.value.message


# ------------------------------------------------------------------- API and MCP

class FakeSTT(ve.STTEngine):
    id = "fake-stt"
    label = "Fake"
    capabilities = ve.EngineCapabilities()

    def is_installed(self):
        return True

    def transcribe(self, path, language=None, word_timestamps=True):
        name = Path(path).stem
        if name.startswith("auk-seed"):
            words = [{"start_s": w["start_s"], "end_s": w["end_s"], "word": t}
                     for w, t in zip(_tones((200, 950, 400, 500, 600), sr=24000)[1], ["Hay", "18", "grados", "con", "sol."])]
        else:
            words = _line()[1]
        return {"language": "es", "text": " ".join(w["word"] for w in words),
                "segments": [{"start_s": 0, "end_s": 3.2, "text": "", "words": words}]}


class FakeTTS(ve.TTSEngine):
    id = "fake-tts"
    label = "Fake"
    capabilities = ve.EngineCapabilities(languages=["es"], cloning=False)

    def is_installed(self):
        return True

    def synthesize(self, text, voice_ref=None, speed=None, pitch=None, style=None, sample_path=None, language=None):
        return ve.wav_bytes_mono16(_line()[0], SR)


@pytest.fixture
def fakes(monkeypatch):
    monkeypatch.setattr("prosperos_hoard.api.ve.default_tts_engines", lambda **kw: [FakeTTS()])
    monkeypatch.setattr("prosperos_hoard.api.ve.default_stt_engines", lambda: [FakeSTT()])
    monkeypatch.setenv("PROSPERO_GPU_LEASE", "0")


def _recording(c, app):
    """A recording imported as a plain audio asset: no voice in its recipe."""
    project = c.post("/api/projects", json={"name": "AuK"}).json()
    asset = c.post("/api/voice/speak", json={"text": "x", "voice": {"engine_id": "fake-tts"}, "project": project["id"]}).json()
    store = app.state.store
    a = store.get_asset(asset["id"])
    (store.data_dir / a["file_path"]).write_bytes(ve.wav_bytes_mono16(_line()[0], SR))
    store.conn.execute("UPDATE assets SET recipe_json=NULL WHERE id=?", (asset["id"],))
    store.conn.commit()
    return asset


def test_api_status_and_not_installed(client, fakes, monkeypatch):
    c, app, _ = client
    monkeypatch.delenv("PROSPERO_AUK_HOME", raising=False)
    status = c.get("/api/voice/auk").json()
    assert status["engine"] == "auk" and status["installed"] is False and status["reason"].startswith("missing")
    asset = _recording(c, app)
    r = c.post("/api/voice/retake_words", json={"asset_id": asset["id"], "first_word": 1, "last_word": 1, "text": "dieciocho",
                                                "context_engine": "auk"})
    assert r.status_code >= 400 and "auk_not_installed" in r.text
    r = c.post("/api/voice/retake_words", json={"asset_id": asset["id"], "first_word": 1, "last_word": 1, "text": "dieciocho"})
    assert r.status_code >= 400 and "voice_required" in r.text and "context_engine" in r.text
    r = c.post("/api/voice/retake_words", json={"asset_id": asset["id"], "first_word": 1, "last_word": 1, "text": "x",
                                                "context_engine": "magic"})
    assert r.status_code >= 400 and "bad_context_engine" in r.text


def test_api_context_retake_on_a_recording_needs_no_voice(client, fakes, monkeypatch, tmp_path):
    c, app, _ = client
    home = tmp_path / "auk"
    _install(home)
    monkeypatch.setenv("PROSPERO_AUK_HOME", str(home))
    takes = {s: ((200, 950, 400, 500, 600), ["Hay", "18", "grados", "con", "sol."]) for s in (1, 2)}
    runner = _fake_runner(takes)
    monkeypatch.setattr(ar, "run_worker", runner)
    asset = _recording(c, app)
    r = c.post("/api/agent/voice_retake_words", json={"asset_id": asset["id"], "first_word": 1, "last_word": 1,
                                                      "text": "dieciocho", "context_engine": "auk", "seeds": 2})
    assert r.status_code == 200, r.text
    body = r.json()
    result = body.get("result", body)
    assert result["engine_id"] == "auk" and result["retake"]["auk"]["chosen_seed"] in (1, 2)
    assert runner.calls[0]["job"]["seeds"] == [1, 2]
    saved = app.state.store.get_asset(result["asset"]["id"])
    assert saved["recipe"]["engine"] == "auk" and saved["recipe"]["retake"]["auk"]["instruction"].startswith("Replace")
    assert not any((app.state.store.data_dir / "tmp" / "auk").glob("*"))      # takes cleaned up


def test_mcp_tool_sends_context_engine(monkeypatch):
    from prosperos_hoard import mcp_server
    sent = {}
    monkeypatch.setattr(mcp_server, "_call", lambda method, path, json=None, **kw: sent.update(path=path, body=json) or {})
    fn = getattr(mcp_server.voice_retake_words, "fn", mcp_server.voice_retake_words)
    fn("a1", 1, 1, "dieciocho", in_context=True, seeds=4)
    assert sent["path"] == "/api/agent/voice_retake_words"
    assert sent["body"]["context_engine"] == "auk" and sent["body"]["seeds"] == 4 and sent["body"]["voice"] is None
