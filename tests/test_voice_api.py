"""HTTP-level tests for /api/voice/* (routing, validation, file plumbing).

Heavy engines are monkeypatched with fakes here too: the point of these
tests is the API surface (routes, bodies, error mapping, file responses),
not re-proving the pipelines already covered by test_voice_lab.py,
test_voice_pipelines.py and test_dubbing.py.
"""

from __future__ import annotations

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
