"""HTTP-level tests for /api/voice/* (routing, validation, file plumbing).

Heavy engines are monkeypatched with fakes here too: the point of these
tests is the API surface (routes, bodies, error mapping, file responses),
not re-proving the pipelines already covered by test_voice_lab.py,
test_voice_pipelines.py and test_dubbing.py.
"""

from __future__ import annotations

import json
import wave
from pathlib import Path

import numpy as np
import pytest

from prosperos_hoard import voice_engines as ve


class FakeTTS(ve.TTSEngine):
    id = "fake-tts"
    label = "Fake"
    capabilities = ve.EngineCapabilities(languages=["en"], cloning=False)

    def is_installed(self):
        return True

    def synthesize(self, text, voice_ref=None, speed=None, pitch=None, style=None, sample_path=None, language=None):
        n = max(1, len(text)) * 100
        samples = (np.sin(np.linspace(0, 10, n)) * 0.2).astype("float32")
        return ve.wav_bytes_mono16(samples, 16000)


class FakeSTT(ve.STTEngine):
    id = "fake-stt"
    label = "Fake"
    capabilities = ve.EngineCapabilities()

    def is_installed(self):
        return True

    def transcribe(self, path, language=None, word_timestamps=True):
        return {"language": "en", "text": "hello world",
                "segments": [{"start_s": 0.0, "end_s": 1.0, "text": "hello world", "words": []}]}


@pytest.fixture(autouse=True)
def fake_engines(monkeypatch):
    monkeypatch.setattr("prosperos_hoard.api.ve.default_tts_engines", lambda **kw: [FakeTTS()])
    monkeypatch.setattr("prosperos_hoard.api.ve.default_stt_engines", lambda: [FakeSTT()])


def _wav_tone(n_samples: int, sr: int = 16000) -> np.ndarray:
    t = np.linspace(0, n_samples / sr, n_samples, endpoint=False)
    return (0.2 * np.sin(2 * np.pi * 220 * t)).astype(np.float32)


def _wav_bytes(seconds=1.0, sr=16000) -> bytes:
    return ve.wav_bytes_mono16(_wav_tone(int(sr * seconds), sr), sr)


def test_voice_engines_route(client):
    c, app, _allowed = client
    resp = c.get("/api/voice/engines")
    assert resp.status_code == 200
    body = resp.json()
    assert {"tts", "stt"} == set(body)
    ids = {e["id"] for e in body["tts"]}
    assert "fake-tts" in ids


def test_voice_speak_returns_wav_bytes(client):
    c, app, _allowed = client
    resp = c.post("/api/voice/speak", json={"text": "hello", "voice": {"engine_id": "fake-tts"}})
    assert resp.status_code == 200
    assert resp.headers["content-type"] == "audio/wav"
    assert resp.content[:4] == b"RIFF"


def test_voice_speak_with_project_saves_asset(client):
    c, app, _allowed = client
    project = c.post("/api/projects", json={"name": "Speak"}).json()
    resp = c.post("/api/voice/speak", json={"text": "hello", "voice": {"engine_id": "fake-tts"},
                                            "project": project["id"]})
    assert resp.status_code == 200
    asset = resp.json()
    assert asset["kind"] == "audio"
    assets = c.get(f"/api/projects/{project['id']}/assets").json()["items"]
    assert any(a["id"] == asset["id"] for a in assets)


def test_voice_speak_empty_text_rejected(client):
    c, app, _allowed = client
    resp = c.post("/api/voice/speak", json={"text": "  ", "voice": {"engine_id": "fake-tts"}})
    assert resp.status_code == 400
    assert resp.json()["error"] == "empty_text"


def test_agent_voice_speak_reports_bytes_without_project(client):
    c, app, _allowed = client
    resp = c.post("/api/agent/voice_speak", json={"text": "hi", "voice": {"engine_id": "fake-tts"}})
    assert resp.status_code == 200
    body = resp.json()
    assert body["engine_id"] == "fake-tts"
    assert body["bytes"] > 0


def test_voice_voices_crud(client, tmp_path):
    c, app, allowed = client
    sample = allowed / "sample.wav"
    with wave.open(str(sample), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(16000)
        w.writeframes((_wav_tone(16000) * 32767).astype("<i2").tobytes())

    resp = c.post("/api/agent/voice_create", json={"name": "Narrator", "engine_id": "fake-tts",
                                                    "source_path": str(sample)})
    assert resp.status_code == 200, resp.text
    voice = resp.json()
    assert voice["name"] == "Narrator"
    voice_id = voice["id"]

    listed = c.get("/api/voice/voices").json()["items"]
    assert any(v["id"] == voice_id for v in listed)

    got = c.get(f"/api/voice/voices/{voice_id}")
    assert got.status_code == 200 and got.json()["engine_id"] == "fake-tts"

    updated = c.patch(f"/api/voice/voices/{voice_id}", json={"notes": "test note"})
    assert updated.json()["notes"] == "test note"

    preset = c.post(f"/api/voice/voices/{voice_id}/presets", json={"name": "calm", "speed": 0.9})
    assert any(p["name"] == "calm" for p in preset.json()["presets"])

    sample_resp = c.get(f"/api/voice/voices/{voice_id}/sample")
    assert sample_resp.status_code == 200

    deleted = c.delete(f"/api/voice/voices/{voice_id}")
    assert deleted.json()["ok"] is True
    assert c.get(f"/api/voice/voices/{voice_id}").status_code == 404


def test_voice_voices_upload(client):
    c, app, _allowed = client
    files = {"file": ("sample.wav", _wav_bytes(), "audio/wav")}
    resp = c.post("/api/voice/voices/upload", params={"name": "Uploaded", "engine_id": "fake-tts"}, files=files)
    assert resp.status_code == 200, resp.text
    assert resp.json()["has_sample"] is True


def test_voice_transcribe_upload(client):
    c, app, _allowed = client
    files = {"file": ("clip.wav", _wav_bytes(), "audio/wav")}
    resp = c.post("/api/voice/transcribe/upload", files=files)
    assert resp.status_code == 200
    body = resp.json()
    assert body["text"] == "hello world"
    assert "srt" in body and "vtt" in body and "txt" in body


def test_voice_transcribe_upload_rejects_oversized_file(client, data_dir, monkeypatch):
    # unlike voice_voices_upload and voice_dictate, this endpoint had no
    # size cap at all: an unbounded client could write an arbitrarily large
    # file to disk before transcription even started.
    from prosperos_hoard import engine

    monkeypatch.setattr(engine, "MAX_MEDIA_BYTES", 1024)
    c, app, _allowed = client
    files = {"file": ("clip.wav", _wav_bytes(seconds=5.0), "audio/wav")}  # well over 1024 bytes
    resp = c.post("/api/voice/transcribe/upload", files=files)
    assert resp.status_code == 400
    assert resp.json()["error"] == "too_large"
    # the partial upload must not be left behind in tmp/uploads
    leftovers = list((data_dir / "tmp" / "uploads").glob("*"))
    assert leftovers == []


def test_voice_dictate(client):
    c, app, _allowed = client
    files = {"file": ("clip.wav", _wav_bytes(), "audio/wav")}
    resp = c.post("/api/voice/dictate", files=files)
    assert resp.status_code == 200
    assert resp.json()["text"] == "hello world"


def test_voice_transcribe_requires_source(client):
    c, app, _allowed = client
    resp = c.post("/api/voice/transcribe", json={})
    assert resp.status_code == 400
    assert resp.json()["error"] == "source_required"


def test_voice_audiobook_requires_text(client):
    c, app, _allowed = client
    resp = c.post("/api/voice/audiobook", json={"voice": {"engine_id": "fake-tts"}})
    assert resp.status_code == 400
    assert resp.json()["error"] == "text_required"


def test_voice_audiobook_end_to_end(client):
    c, app, _allowed = client
    resp = c.post("/api/voice/audiobook", json={"text": "Hello world. This is a test.",
                                                "voice": {"engine_id": "fake-tts"}, "wait_s": 30})
    assert resp.status_code == 200, resp.text
    job = resp.json()["job"]
    assert job["state"] == "done"
    job_id = job["id"]

    status = c.get(f"/api/voice/audiobook/{job_id}")
    assert status.json()["state"] == "done"

    download = c.get(f"/api/voice/audiobook/{job_id}/download", params={"file": "final"})
    assert download.status_code == 200 and len(download.content) > 0

    srt = c.get(f"/api/voice/audiobook/{job_id}/download", params={"file": "srt"})
    assert srt.status_code == 200


def test_voice_dub_requires_source(client):
    c, app, _allowed = client
    resp = c.post("/api/voice/dub", json={"target_language": "fr", "voice": {"engine_id": "fake-tts"}})
    assert resp.status_code == 400
    assert resp.json()["error"] == "source_required"


def test_voice_engine_install_unknown_engine(client):
    c, app, _allowed = client
    resp = c.post("/api/voice/engines/does-not-exist/install", json={"kind": "tts"})
    assert resp.status_code == 400
    assert resp.json()["error"] == "unknown_engine"


def test_voice_engine_install_bad_kind(client):
    c, app, _allowed = client
    resp = c.post("/api/voice/engines/fake-tts/install", json={"kind": "nonsense"})
    assert resp.status_code == 400
    assert resp.json()["error"] == "bad_kind"


def test_voice_job_not_found(client):
    c, app, _allowed = client
    resp = c.get("/api/agent/voice_job", params={"job_id": "job_does_not_exist"})
    assert resp.status_code == 404


# ------------------------------------------------------------ regressions

def test_upload_routes_run_blocking_work_off_the_event_loop(client, monkeypatch):
    import asyncio

    seen = []

    class LoopProbeSTT(FakeSTT):
        def transcribe(self, path, language=None, word_timestamps=True):
            try:
                asyncio.get_running_loop()
                seen.append("event-loop")
            except RuntimeError:
                seen.append("worker-thread")
            return super().transcribe(path, language, word_timestamps)

    monkeypatch.setattr("prosperos_hoard.api.ve.default_stt_engines", lambda: [LoopProbeSTT()])
    c, app, _allowed = client
    files = {"file": ("clip.wav", _wav_bytes(), "audio/wav")}
    assert c.post("/api/voice/transcribe/upload", files=files).status_code == 200
    assert c.post("/api/voice/dictate", files={"file": ("clip.wav", _wav_bytes(), "audio/wav")}).status_code == 200
    resp = c.post("/api/voice/voices/upload", params={"name": "Probe", "engine_id": "fake-tts"},
                  files={"file": ("s.wav", _wav_bytes(), "audio/wav")})
    assert resp.status_code == 200, resp.text
    assert seen == ["worker-thread"] * 3


def test_transcribe_language_auto_means_detect(client, monkeypatch):
    got = []

    class LangSTT(FakeSTT):
        def transcribe(self, path, language=None, word_timestamps=True):
            got.append(language)
            return super().transcribe(path, language, word_timestamps)

    monkeypatch.setattr("prosperos_hoard.api.ve.default_stt_engines", lambda: [LangSTT()])
    c, app, _allowed = client
    for lang in ("auto", "Spanish"):
        resp = c.post("/api/voice/dictate", params={"language": lang},
                      files={"file": ("clip.wav", _wav_bytes(), "audio/wav")})
        assert resp.status_code == 200
    assert got == [None, "es"]
    resp = c.post("/api/voice/dictate", params={"language": "Klingon"},
                  files={"file": ("clip.wav", _wav_bytes(), "audio/wav")})
    assert resp.status_code == 400 and resp.json()["error"] == "bad_language"


def test_voice_studio_downloads_never_leave_the_voice_studio_folder(client, data_dir):
    c, app, _allowed = client
    store = app.state.store
    (data_dir / "backend.json").write_text('{"faustus_token": "secret"}', encoding="utf-8")
    job = store.create_job("audiobook", "cpu", {"text": "x"})
    store.update_job(job["id"], state="done", outputs={"final_file": "backend.json", "srt_file": "../backend.json"})
    for which in ("final", "srt"):
        resp = c.get(f"/api/voice/audiobook/{job['id']}/download", params={"file": which})
        assert resp.status_code == 404
        assert b"secret" not in resp.content


def test_voice_delete_removes_the_sample_folder_and_lexicon_roundtrip(client, data_dir):
    c, app, _allowed = client
    resp = c.post("/api/voice/voices/upload", params={"name": "Private Person", "engine_id": "fake-tts"},
                  files={"file": ("s.wav", _wav_bytes(), "audio/wav")})
    voice_id = resp.json()["id"]
    row = c.get(f"/api/voice/voices/{voice_id}").json()
    folder = (data_dir / row["sample_path"]).parent
    assert folder.is_dir()

    patched = c.patch(f"/api/voice/voices/{voice_id}", json={"lexicon": {"Prospero": "PROS-per-oh"}})
    assert patched.status_code == 200
    listed = {v["id"]: v for v in c.get("/api/voice/voices").json()["items"]}
    assert listed[voice_id]["lexicon"] == {"Prospero": "PROS-per-oh"}
    assert listed[voice_id]["presets"] == []
    bad = c.post(f"/api/voice/voices/{voice_id}/presets", json={"name": "_lexicon", "speed": 2})
    assert bad.status_code == 400 and bad.json()["error"] == "bad_preset_name"

    deleted = c.delete(f"/api/voice/voices/{voice_id}").json()
    assert deleted["ok"] is True and deleted["files_removed"] is True
    assert not folder.exists()


def test_voice_speak_applies_request_lexicon(client, monkeypatch):
    spoken = []

    class Rec(FakeTTS):
        def synthesize(self, text, **kw):
            spoken.append(text)
            return super().synthesize(text, **kw)

    monkeypatch.setattr("prosperos_hoard.api.ve.default_tts_engines", lambda **kw: [Rec()])
    c, app, _allowed = client
    resp = c.post("/api/voice/speak", json={"text": "Hello Prospero",
                                            "voice": {"engine_id": "fake-tts", "lexicon": {"prospero": "PROS-per-oh"}}})
    assert resp.status_code == 200
    assert spoken == ["Hello PROS-per-oh"]


def test_voice_dub_validates_language_and_voice_before_queueing(client):
    c, app, allowed = client
    video = allowed / "clip.mp4"
    video.write_bytes(b"not really a video")
    resp = c.post("/api/voice/dub", json={"source_path": str(video), "target_language": "Klingon",
                                          "voice": {"engine_id": "fake-tts"}})
    assert resp.status_code == 400 and resp.json()["error"] == "bad_language"
    resp = c.post("/api/voice/dub", json={"source_path": str(video), "target_language": "Spanish",
                                          "voice": {"engine_id": "no-such-engine"}})
    assert resp.status_code == 400 and resp.json()["error"] == "unknown_engine"
    assert not app.state.store.conn.execute("SELECT 1 FROM jobs WHERE type='dub'").fetchone()


def test_voice_audiobook_validates_voice_before_queueing(client):
    c, app, _allowed = client
    resp = c.post("/api/voice/audiobook", json={"text": "Hello.", "voice": {"engine_id": "nope"}})
    assert resp.status_code == 400 and resp.json()["error"] == "unknown_engine"
    resp = c.post("/api/voice/audiobook", json={"text": "Hello.", "voice": {"engine_id": "fake-tts", "pitch": 40}})
    assert resp.status_code == 400 and resp.json()["error"] == "bad_pitch"


def test_agent_voice_job_shows_compact_audiobook_outputs(client):
    c, app, _allowed = client
    resp = c.post("/api/agent/voice_audiobook", json={"text": "# One\nHello world.\n\n# Two\nBye now.",
                                                      "voice": {"engine_id": "fake-tts"}, "wait_s": 30})
    job = resp.json()["job"]
    assert job["state"] == "done"
    outputs = job["outputs"]
    assert [ch["title"] for ch in outputs["chapters"]] == ["One", "Two"]
    assert outputs["sentence_count"] == 2
    assert "final_file" not in outputs and "work_dir" not in outputs
    again = c.get("/api/agent/voice_job", params={"job_id": job["id"]}).json()
    assert again["outputs"]["chapter_count"] == 2


def test_agent_voice_dub_segments_pages(client):
    c, app, _allowed = client
    store = app.state.store
    rows = [{"index": i, "start_s": float(i), "end_s": i + 0.5, "source_text": f"s{i}", "translated_text": f"t{i}",
             "fit": {}} for i in range(30)]
    job = store.create_job("dub", "cpu", {"target_language": "es"})
    store.update_job(job["id"], state="done", outputs={"segments": rows, "final_video": "voice_studio/dub/x/d.mp4"})
    page = c.get("/api/agent/voice_dub_segments", params={"job_id": job["id"], "offset": 20, "limit": 50}).json()
    assert page["job_id"] == job["id"] and page["total"] == 30
    assert [r["index"] for r in page["items"]] == list(range(20, 30)) and page["next_offset"] is None
    view = c.get("/api/agent/voice_job", params={"job_id": job["id"]}).json()
    assert view["outputs"]["segments"]["total"] == 30 and "final_video" not in json.dumps(view)
    other = store.create_job("audiobook", "cpu", {"text": "x"})
    assert c.get("/api/agent/voice_dub_segments", params={"job_id": other["id"]}).status_code == 400


def test_free_memory_also_unloads_voice_models(client, monkeypatch):
    called = []
    monkeypatch.setattr("prosperos_hoard.api.ve.unload_models", lambda: called.append(1) or {"unloaded": 0})
    c, app, _allowed = client
    c.post("/api/backend/comfy/free")
    assert called == [1]
