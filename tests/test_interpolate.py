"""Real frame/audio finishing, lineage, failed jobs and cancellation."""
import hashlib

import numpy as np
import pytest

from prosperos_hoard import interpolate
from prosperos_hoard.engine import EngineError
from prosperos_hoard.hoard_link.media.ffmpeg import FFmpeg
from prosperos_hoard.jobs import JobCancelled
from test_qa import _clip


def source_clip(store, pid, sound=False, offset=0):
    frames = np.zeros((48, 96, 192), dtype=np.uint8)
    texture = np.random.default_rng(7).integers(80, 250, (32, 32), dtype=np.uint8)
    for i in range(48):
        frames[i, 32:64, 16 + 2*i:48 + 2*i] = texture
    aid = _clip(store, pid, frames)
    if sound:
        f = FFmpeg()
        src = store.data_dir / store.get_asset(aid)["file_path"]
        sound_path = src.with_name("with-sound.mp4")
        f.run(["-i", str(src), "-itsoffset", str(offset), "-f", "lavfi", "-i", f"sine=frequency=440:duration={2-offset}",
               "-map", "0:v", "-map", "1:a", "-c:v", "copy", "-c:a", "aac", str(sound_path)])
        src.write_bytes(sound_path.read_bytes())
        sound_path.unlink()
    return aid


@pytest.mark.parametrize("fps,sound", [(48, True), (60, False)])
def test_real_interpolation_preserves_clip_and_generates_intermediate_motion(store, project, fps, sound):
    aid = source_clip(store, project["id"], sound)
    src = store.data_dir / store.get_asset(aid)["file_path"]
    original = store.get_asset(aid)
    digest = hashlib.sha256(src.read_bytes()).hexdigest()
    out = interpolate.run(store, {"params": {"asset_id": aid, "fps": fps}}, lambda *a: None)
    asset = store.get_asset(out["asset_id"])
    media = FFmpeg()
    path = store.data_dir / asset["file_path"]
    info = media.probe(path)
    video = next(s for s in info["streams"] if s["codec_type"] == "video")
    assert float(video["avg_frame_rate"].split('/')[0]) / float(video["avg_frame_rate"].split('/')[1]) == fps
    assert int(video["nb_frames"]) == 2 * fps
    assert (asset["width"], asset["height"]) == (192, 96)
    assert abs(float(video["duration"]) - 2) <= 1 / fps
    assert media.summarize(info)["has_audio"] is sound
    if sound:
        raw, _ = media.capture(["-i", str(path), "-vn", "-f", "s16le", "-ac", "1", "-ar", "16000", "pipe:1"], binary=True)
        wave = np.frombuffer(raw, dtype=np.int16)
        assert 1.98 <= len(wave) / 16000 <= 2.04
        assert np.sqrt(np.mean(wave.astype(float)**2)) > 1000
        spectrum = np.abs(np.fft.rfft(wave[:16000].astype(float)))
        assert 435 <= int(spectrum.argmax()) <= 445  # pitch and pacing preserved
    # At 48fps the half-frame contains motion between the source poses,
    # rather than just duplicating frames to change the container's fps.
    if fps == 48:
        raw, _ = media.capture(["-i", str(path), "-vf", "format=gray", "-f", "rawvideo", "pipe:1"], binary=True)
        frames = np.frombuffer(raw, dtype=np.uint8).reshape(-1, 96, 192)
        centers = [(frame.sum(axis=0) * np.arange(192)).sum() / frame.sum() for frame in frames[20:23]]
        assert centers[0] + 0.3 < centers[1] < centers[2] - 0.3, centers
        assert frames[-1].sum() > 10000  # the final source pose wasn't lost
    assert asset["recipe"]["derived_from"] == aid
    assert asset["recipe"]["method"] == "ffmpeg_minterpolate"
    assert store.get_asset(aid) == original
    assert hashlib.sha256(src.read_bytes()).hexdigest() == digest


@pytest.mark.parametrize('video_start', [0, 5])
def test_delayed_audio_keeps_its_offset(store, project, video_start):
    aid = source_clip(store, project['id'], sound=True, offset=0.5)
    media = FFmpeg()
    src = store.data_dir / store.get_asset(aid)['file_path']
    if video_start:
        shifted = src.with_name('shifted.mp4')
        media.run(['-i', str(src), '-c', 'copy', '-output_ts_offset', str(video_start), str(shifted)])
        src.write_bytes(shifted.read_bytes())
        shifted.unlink()
    def audio_start(aid):
        info = media.probe(store.data_dir / store.get_asset(aid)['file_path'])
        return float(next(s for s in info['streams'] if s['codec_type'] == 'audio')['start_time'])
    before = audio_start(aid)
    assert before - video_start > 0.45
    out = interpolate.run(store, {'params': {'asset_id': aid}}, lambda *a: None)
    assert abs(audio_start(out['asset_id']) - (before - video_start)) < 0.05


@pytest.mark.parametrize("fps", [0, -1, 121, float('nan'), float('inf')])
def test_invalid_rates_do_not_create_files(store, project, fps):
    aid = source_clip(store, project["id"])
    before = sorted(p.relative_to(store.data_dir).as_posix() for p in store.data_dir.rglob('*.mp4'))
    with pytest.raises(EngineError) as err:
        interpolate.run(store, {"params": {"asset_id": aid, "fps": fps}}, lambda *a: None)
    assert err.value.code == "bad_fps"
    assert sorted(p.relative_to(store.data_dir).as_posix() for p in store.data_dir.rglob('*.mp4')) == before


def test_cancelled_finish_leaves_no_take(store, project):
    aid = source_clip(store, project["id"])
    class Progress:
        def __call__(self, *a): pass
        def cancelled(self): return True
    before = list(store.data_dir.rglob('*.mp4'))
    with pytest.raises(JobCancelled):
        interpolate.run(store, {"params": {"asset_id": aid}}, Progress())
    assert list(store.data_dir.rglob('*.mp4')) == before


def test_failed_finish_removes_partial_output(store, project, monkeypatch):
    aid = source_clip(store, project["id"])
    before = list(store.data_dir.rglob('*.mp4'))
    def fail(self, args, **kwargs):
        from pathlib import Path
        Path(args[-1]).write_bytes(b'partial')
        raise RuntimeError('encoder failed')
    monkeypatch.setattr(FFmpeg, 'run', fail)
    with pytest.raises(RuntimeError, match='encoder failed'):
        interpolate.run(store, {"params": {"asset_id": aid}}, lambda *a: None)
    assert list(store.data_dir.rglob('*.mp4')) == before


def test_http_repeated_calls_and_recoverable_rate_error(client):
    c, app, _ = client
    store = app.state.store
    pid = store.create_project('Smooth')['id']
    aid = source_clip(store, pid)
    bad = c.post('/api/agent/studio_interpolate', json={'asset_id': aid, 'fps': 12, 'wait_s': 30})
    assert bad.status_code == 400
    assert 'above the source rate (24)' in bad.text
    assert store.conn.execute('SELECT COUNT(*) FROM jobs').fetchone()[0] == 0
    made = []
    for _ in range(2):
        r = c.post('/api/agent/studio_interpolate', json={'asset_id': aid, 'fps': 48, 'wait_s': 30})
        assert r.status_code == 200, r.text
        assert r.json()['job']['state'] == 'done', r.text
        made.extend(r.json()['job']['asset_ids'])
    assert len(made) == len(set(made)) == 2
    assert all(store.get_asset(a)['recipe']['derived_from'] == aid for a in made)
