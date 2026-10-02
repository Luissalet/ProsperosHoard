"""Stems: a song split into vocals, drums, bass and other (Demucs in
ComfyUI's Python, faked here by a script that writes the raw stems), the
lip sync reading the clean vocals and the kick effects reading the drums."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import numpy as np
import pytest

from prosperos_hoard import stems
from test_lipsync import _song
from test_new_templates import _last_prompt, _node, _run_job, _still

FAKE = r'''#!{py}
import json, sys
import numpy as np
script, lib, src, n, out, device, model = sys.argv[1:8]
mix = np.fromfile(src, dtype=np.float32).reshape(-1, 2)
for i, name in enumerate(["drums", "bass", "other", "vocals"]):
    (mix * (0.25 + 0.1 * i)).astype(np.float32).tofile(f"{{out}}/{{name}}.f32")
print("loading model...")
print(json.dumps({{"stems": ["drums", "bass", "other", "vocals"], "device": device}}))
'''


@pytest.fixture
def fake_python(tmp_path, store):
    path = tmp_path / "python"
    path.write_text(FAKE.format(py=sys.executable), encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    lib = stems.lib_dir(store.data_dir)
    lib.mkdir(parents=True, exist_ok=True)
    (lib / stems.MARKER).write_text("{}", encoding="utf-8")
    if os.name == "nt":
        pytest.skip("the fake interpreter is a shebang script")
    return path


def test_a_song_splits_into_four_stems_once(store, project, fake_python):
    song = _song(store, project, 3.0)
    out = stems.separate(store, song, fake_python, lambda *_: None, device="cpu")
    assert set(out["stems"]) == set(stems.STEMS) | {"instrumental"} and out["device"] == "cpu" and not out["reused"]
    vocals = store.get_asset(out["stems"]["vocals"])
    assert vocals["kind"] == "audio" and vocals["recipe"]["derived_from"] == song and "stem" in vocals["tags"]
    assert (store.data_dir / vocals["file_path"]).stat().st_size > 1000
    assert stems.existing(store, song) == out["stems"]
    again = stems.separate(store, song, fake_python, lambda *_: None)
    assert again["reused"] and again["stems"] == out["stems"]


def test_no_python_or_no_audio(store, project):
    song = _song(store, project, 2.0)
    with pytest.raises(stems.StemsError) as err:
        stems.separate(store, song, None, lambda *_: None)
    assert err.value.code == "no_python"
    still = store.create_asset(project_id=project["id"], kind="image", file_path="x.png", source="generated")["id"]
    with pytest.raises(stems.StemsError):
        stems.separate(store, still, Path(sys.executable), lambda *_: None)


def test_the_lips_follow_the_vocals_stem(store, backend_with_comfy, fake_comfy, project, fake_python):
    server, _ = fake_comfy
    server.motion_models = True
    still = _still(store, backend_with_comfy, project, 832, 480)
    song = _song(store, project)
    vocals = stems.separate(store, song, fake_python, lambda *_: None, device="cpu")["stems"]["vocals"]
    done = _run_job(store, backend_with_comfy, "generate_image", {
        "prompt": "she sings", "positive_prompt": "she sings", "negative_prompt": "", "seed": 11, "count": 1,
        "template": "wan22_s2v", "reference_asset_id": still, "audio_asset_id": song, "audio_start_s": 1.0,
        "audio_seconds": 3.0}, project["id"])
    assert done["state"] == "done", done
    wf = _last_prompt(fake_comfy)
    assert wf["vocals_in"]["inputs"]["audio"].startswith(f"prospero_{vocals}_1000_3000")
    assert _node(wf, "AudioEncoderEncode")["audio"] == ["vocals_in", 0]
    assert _node(wf, "CreateVideo")["audio"] == ["58", 0]  # the clip keeps the mix
    assert vocals in store.get_asset(done["outputs"]["asset_ids"][0])["recipe"]["input_asset_ids"]
    done = _run_job(store, backend_with_comfy, "generate_image", {
        "prompt": "she sings", "positive_prompt": "she sings", "negative_prompt": "", "seed": 12, "count": 1,
        "template": "wan22_s2v", "reference_asset_id": still, "audio_asset_id": song, "audio_start_s": 1.0,
        "audio_seconds": 3.0, "use_vocals": False}, project["id"])
    assert "vocals_in" not in _last_prompt(fake_comfy)


def test_stems_over_http(client, tmp_path):
    c, app, _ = client
    store = app.state.store
    pid = c.post("/api/projects", json={"name": "Stems"}).json()["id"]
    song = _song(store, {"id": pid}, 2.0)
    assert c.get(f"/api/assets/{song}/stems").json() == {"stems": {}}
    r = c.post("/api/agent/studio_stems", json={"asset_id": song, "device": "gpu0"})
    assert r.status_code == 400
    r = c.post(f"/api/assets/{song}/stems", json={"asset_id": song, "device": "cpu"})
    assert r.status_code == 200 and r.json()["job"]["type"] == "stems"


def test_effects_can_follow_one_stem():
    from prosperos_hoard import audio, video
    sr = audio.SAMPLE_RATE
    x = np.zeros(sr * 4, dtype=np.float32)
    for t in (0.5, 1.25, 2.0, 3.1):  # bright clicks: every attack, not only bass
        i = int(t * sr)
        x[i:i + 400] = np.sin(2 * np.pi * 3000 * np.arange(400) / sr) * np.exp(-np.arange(400) / 80)
    hits = audio.beat_hits(x, "onsets")
    assert len(hits) == 4 and abs(hits[0][0] - 0.5) < 0.05
    assert video.validate_finishing({"beat_fx": {"source": "vocals", "zoom": 0.4}})["beat_fx"]["source"] == "vocals"
    with pytest.raises(video.RenderError):
        video.validate_finishing({"beat_fx": {"source": "piano"}})


def test_the_instrumental_is_mixed_from_the_stems(store, project, fake_python):
    song = _song(store, project, 2.0)
    out = stems.separate(store, song, fake_python, lambda *_: None, device="cpu")
    inst = store.get_asset(out["stems"]["instrumental"])
    assert inst["recipe"]["stem"] == "instrumental" and set(inst["recipe"]["input_asset_ids"]) == {
        out["stems"]["drums"], out["stems"]["bass"], out["stems"]["other"]}
    assert stems.existing(store, song)["instrumental"] == inst["id"]
