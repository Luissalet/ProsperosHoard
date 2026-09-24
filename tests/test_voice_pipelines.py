from __future__ import annotations

import shutil
import zipfile
from pathlib import Path
from typing import Any

import numpy as np
import pytest

from prosperos_hoard import voice_engines as ve
from prosperos_hoard import voice_pipelines as vp
from prosperos_hoard.jobs import Progress
from prosperos_hoard.store import Store

HAS_FFMPEG = shutil.which("ffmpeg") is not None


class FakeTTS(ve.TTSEngine):
    id = "fake"
    label = "Fake"
    capabilities = ve.EngineCapabilities(languages=["en"], cloning=False)

    def is_installed(self):
        return True

    def synthesize(self, text, voice_ref=None, speed=None, pitch=None, style=None, sample_path=None, language=None):
        n = max(1, len(text)) * 200
        samples = (np.sin(np.linspace(0, 20, n)) * 0.2).astype("float32")
        return ve.wav_bytes_mono16(samples, 16000)


# ------------------------------------------------------------------ chunking

def test_split_into_chapters_markdown_headings():
    text = "# Chapter One\nHello world.\n\n# Chapter Two\nSecond part."
    chapters = vp.split_into_chapters(text)
    assert [c["title"] for c in chapters] == ["Chapter One", "Chapter Two"]
    assert chapters[0]["text"] == "Hello world."
    assert chapters[1]["text"] == "Second part."


def test_split_into_chapters_chapter_word():
    text = "Chapter 1\nOnce upon a time.\n\nChapter 2\nThe end."
    chapters = vp.split_into_chapters(text)
    assert [c["title"] for c in chapters] == ["Chapter 1", "Chapter 2"]


def test_split_into_chapters_spanish_capitulo():
    text = "Capitulo I\nHabia una vez.\n\nCapitulo II\nFin."
    chapters = vp.split_into_chapters(text)
    assert len(chapters) == 2


def test_split_into_chapters_no_headings_is_one_chapter():
    text = "Just a plain paragraph with no structure at all."
    chapters = vp.split_into_chapters(text)
    assert len(chapters) == 1
    assert chapters[0]["text"] == text


def test_split_into_sentences_basic():
    sentences = vp.split_into_sentences("Hello world. This is a test! Does it work? Yes.")
    assert sentences == ["Hello world.", "This is a test!", "Does it work?", "Yes."]


def test_split_into_sentences_paragraphs():
    text = "First paragraph sentence.\n\nSecond paragraph here."
    assert vp.split_into_sentences(text) == ["First paragraph sentence.", "Second paragraph here."]


def test_extract_text_from_txt_and_md(tmp_path):
    txt = tmp_path / "a.txt"
    txt.write_text("plain text content", encoding="utf-8")
    assert vp.extract_text_from_file(txt) == "plain text content"

    md = tmp_path / "b.md"
    md.write_text("# Title\nBody text.", encoding="utf-8")
    assert "Body text." in vp.extract_text_from_file(md)


def test_extract_text_unsupported_extension(tmp_path):
    bad = tmp_path / "a.docx"
    bad.write_bytes(b"x")
    with pytest.raises(vp.AudiobookError):
        vp.extract_text_from_file(bad)


def _build_tiny_epub(path: Path) -> None:
    with zipfile.ZipFile(path, "w") as zf:
        zf.writestr("mimetype", "application/epub+zip")
        zf.writestr("META-INF/container.xml", (
            '<?xml version="1.0"?>'
            '<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">'
            '<rootfiles><rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>'
            '</rootfiles></container>'
        ))
        zf.writestr("OEBPS/content.opf", (
            '<?xml version="1.0"?>'
            '<package xmlns="http://www.idpf.org/2007/opf" version="2.0">'
            '<manifest><item id="ch1" href="ch1.xhtml" media-type="application/xhtml+xml"/></manifest>'
            '<spine><itemref idref="ch1"/></spine></package>'
        ))
        zf.writestr("OEBPS/ch1.xhtml", "<html><body><p>Hello from the book.</p><p>Second line.</p></body></html>")


def test_extract_text_from_epub(tmp_path):
    epub_path = tmp_path / "book.epub"
    _build_tiny_epub(epub_path)
    text = vp.extract_text_from_file(epub_path)
    assert "Hello from the book." in text
    assert "Second line." in text


# --------------------------------------------------------------- audio glue

def test_resample_identity_and_change():
    samples = np.zeros(100, dtype=np.float32)
    assert vp._resample(samples, 16000, 16000) is samples
    out = vp._resample(np.ones(1000, dtype=np.float32), 8000, 16000)
    assert len(out) == 2000


def test_wav_bytes_to_float_roundtrip():
    samples = (np.sin(np.linspace(0, 10, 4000)) * 0.5).astype(np.float32)
    data = ve.wav_bytes_mono16(samples, 16000)
    back, sr = vp._wav_bytes_to_float(data)
    assert sr == 16000
    assert len(back) == len(samples)
    assert np.allclose(back, samples, atol=1e-3)


def test_synthesize_sentences_calls_on_sentence(tmp_path):
    store = Store(tmp_path / "data")
    calls = []
    samples = vp.synthesize_sentences(store, [FakeTTS()], {"engine_id": "fake"}, ["One.", "Two."],
                                      on_sentence=lambda i, s, a, b: calls.append((i, s, round(a, 2), round(b, 2))))
    assert len(calls) == 2
    assert calls[0][0] == 0 and calls[1][0] == 1
    assert calls[1][2] >= calls[0][3]  # second sentence starts after the first ends (plus the pause)
    assert samples.dtype == np.float32


# ------------------------------------------------------------------- job(s)

def test_audiobook_job_with_fake_engine(tmp_path, monkeypatch):
    store = Store(tmp_path / "data")
    project = store.create_project("Book Project")
    monkeypatch.setattr(ve, "default_tts_engines", lambda **kw: [FakeTTS()])

    text = "# Chapter One\nHello world. This is a test!\n\n# Chapter Two\nSecond chapter here. The end."
    job = store.create_job("audiobook", "cpu", {
        "text": text, "title": "Test Book", "voice": {"engine_id": "fake"}, "format": "mp3",
        "project_id": project["id"],
    })
    outputs = vp.audiobook_job(store, None, job, Progress(store, job["id"]))

    assert outputs["title"] == "Test Book"
    assert len(outputs["chapters"]) == 2
    assert outputs["sentence_count"] == 4
    assert (tmp_path / "data" / outputs["final_file"]).is_file()
    assert (tmp_path / "data" / outputs["srt_file"]).is_file()
    assert (tmp_path / "data" / outputs["lrc_file"]).is_file()
    assert outputs["asset_ids"]
    asset = store.get_asset(outputs["asset_ids"][0])
    assert asset["kind"] == "audio"
    assert asset["recipe"]["operation"] == "audiobook"


def _probe_sample_rate(path: Path) -> int:
    from prosperos_hoard import procutil
    from prosperos_hoard.backend import ffprobe_path

    proc = procutil.run([ffprobe_path(), "-v", "error", "-select_streams", "a:0", "-show_entries",
                        "stream=sample_rate", "-of", "csv=p=0", str(path)], text=True, timeout=30)
    return int(proc.stdout.strip())


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
@pytest.mark.parametrize("fmt,chapters", [
    ("mp3", None),
    ("m4b", [{"title": "C1", "start_s": 0.0, "end_s": 1.0}]),
])
def test_encode_final_output_sample_rate_is_fixed(tmp_path, fmt, chapters):
    # ffmpeg's loudnorm filter emits at 192kHz internally; left unfixed the
    # mp3/aac encoders each pick their own nearest supported rate from that
    # (48000 for libmp3lame, 96000 for aac in practice) instead of a sane,
    # predictable, universally-compatible rate for narrated speech.
    samples = (0.2 * np.sin(np.linspace(0, 40, vp.COMMON_SR * 2))).astype("float32")
    work_wav = tmp_path / "work.wav"
    work_wav.write_bytes(ve.wav_bytes_mono16(samples, vp.COMMON_SR))
    out = tmp_path / f"final.{fmt}"
    vp.encode_final(work_wav, out, chapters=chapters)
    assert out.is_file()
    assert _probe_sample_rate(out) == 44100


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_audiobook_job_m4b_has_chapters(tmp_path, monkeypatch):
    store = Store(tmp_path / "data")
    monkeypatch.setattr(ve, "default_tts_engines", lambda **kw: [FakeTTS()])
    text = "Chapter 1\nOne two three.\n\nChapter 2\nFour five six."
    job = store.create_job("audiobook", "cpu", {"text": text, "voice": {"engine_id": "fake"}, "format": "m4b"})
    outputs = vp.audiobook_job(store, None, job, Progress(store, job["id"]))
    final = tmp_path / "data" / outputs["final_file"]
    assert final.suffix == ".m4b"
    assert final.is_file() and final.stat().st_size > 0


def test_audiobook_job_resumes_without_resynthesizing(tmp_path, monkeypatch):
    store = Store(tmp_path / "data")
    calls = {"n": 0}

    class CountingTTS(FakeTTS):
        def synthesize(self, text, **kw):
            calls["n"] += 1
            return super().synthesize(text, **kw)

    monkeypatch.setattr(ve, "default_tts_engines", lambda **kw: [CountingTTS()])
    params = {"text": "Chapter 1\nOne.\n\nChapter 2\nTwo.", "voice": {"engine_id": "fake"}, "format": "mp3"}

    job1 = store.create_job("audiobook", "cpu", params)
    vp.audiobook_job(store, None, job1, Progress(store, job1["id"]))
    first_calls = calls["n"]
    assert first_calls == 2

    # a fresh job with the exact same content resumes: no chapter is redone
    job2 = store.create_job("audiobook", "cpu", params)
    outputs2 = vp.audiobook_job(store, None, job2, Progress(store, job2["id"]))
    assert calls["n"] == first_calls
    assert len(outputs2["chapters"]) == 2


@pytest.mark.skipif(not HAS_FFMPEG, reason="ffmpeg not installed")
def test_concat_wavs_handles_apostrophe_in_path(tmp_path):
    # the real Windows install folder is named "Prospero's Hoard": the
    # audiobook work directory (and so its chapter wavs) can live under a
    # path containing an apostrophe, which the ffmpeg concat demuxer's own
    # quoting must not choke on.
    work_dir = tmp_path / "Prospero's Hoard" / "data"
    work_dir.mkdir(parents=True)
    a = work_dir / "ch000.wav"
    b = work_dir / "ch001.wav"
    vp._write_wav(a, np.ones(1600, dtype=np.float32) * 0.1, 16000)
    vp._write_wav(b, np.ones(1600, dtype=np.float32) * 0.1, 16000)
    out = work_dir / "final.wav"
    vp._concat_wavs([a, b], out)
    assert out.is_file() and out.stat().st_size > 0
    from prosperos_hoard import audio as audio_mod

    assert audio_mod.probe_duration_s(out) == pytest.approx(0.2, abs=0.02)


def test_audiobook_job_resume_keeps_full_transcript(tmp_path, monkeypatch):
    """A resumed run (a re-queued job with the exact same content, as after
    a crash or a cancel) must reuse the already-rendered chapters' audio
    *and* keep their transcript lines - not just skip re-synthesizing them
    while silently dropping their SRT/LRC entries and undercounting
    sentence_count."""
    store = Store(tmp_path / "data")
    monkeypatch.setattr(ve, "default_tts_engines", lambda **kw: [FakeTTS()])
    params = {"text": "Chapter 1\nOne.\n\nChapter 2\nTwo.", "voice": {"engine_id": "fake"}, "format": "mp3"}

    job1 = store.create_job("audiobook", "cpu", params)
    outputs1 = vp.audiobook_job(store, None, job1, Progress(store, job1["id"]))
    assert outputs1["sentence_count"] == 2

    # simulate a crash/cancel and a fresh job re-queued with identical
    # content: it reuses ch000.wav (already on disk) but must still count
    # and transcribe that chapter's sentence(s)
    job2 = store.create_job("audiobook", "cpu", params)
    outputs2 = vp.audiobook_job(store, None, job2, Progress(store, job2["id"]))
    assert outputs2["sentence_count"] == 2

    srt = (tmp_path / "data" / outputs2["srt_file"]).read_text(encoding="utf-8")
    assert "One." in srt
    assert "Two." in srt


def test_audiobook_job_rejects_oversized_text(tmp_path, monkeypatch):
    store = Store(tmp_path / "data")
    monkeypatch.setattr(vp, "MAX_TEXT_CHARS", 10)
    job = store.create_job("audiobook", "cpu", {"text": "x" * 20, "voice": {"engine_id": "fake"}})
    with pytest.raises(vp.AudiobookError):
        vp.audiobook_job(store, None, job, Progress(store, job["id"]))
