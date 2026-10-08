"""Export of a cut for desktop editors (FCP7 XML for Premiere/Resolve, CMX
3600 EDL), production settings (autopilot) and the pre-run check."""

from __future__ import annotations

import io
import copy
import json
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

from prosperos_hoard import exporters
from prosperos_hoard import productions as prod
from test_productions import _asset, tiny_spec


def _timeline():
    return {"name": "Night Walk 9:16", "fps": 24, "width": 1080, "height": 1920, "audio_asset_id": "song",
            "tracks": [{"type": "visual", "clips": [
                {"asset_id": "still", "kind": "image", "start_s": 0, "duration_s": 2.0},
                {"asset_id": "clip", "kind": "video", "start_s": 2.0, "duration_s": 1.5, "trim_start_s": 1.0},
                {"asset_id": "gone", "kind": "image", "start_s": 3.5, "duration_s": 0.5}]},
                {"type": "lyrics", "clips": [{"text": "lamps & road", "start_s": 0.5, "end_s": 2}]}]}


def _lookup(tmp: Path):
    files = {"still": ("image", None), "clip": ("video", 5.0), "song": ("audio", 8.0)}
    def look(aid):
        if aid not in files:
            return None
        kind, dur = files[aid]
        return {"path": tmp / f"{aid} file.bin", "name": aid, "kind": kind, "duration_s": dur, "width": 1080, "height": 1920}
    return look


def test_xmeml_lays_the_clips_frame_exact_with_markers(tmp_path):
    xml = exporters.to_xmeml(_timeline(), _lookup(tmp_path))
    root = ET.fromstring(xml.split("\n", 2)[2])
    seq = root.find("sequence")
    assert seq.findtext("duration") == str(48 + 36 + 12)
    items = seq.findall("./media/video/track/clipitem")
    assert [(i.findtext("start"), i.findtext("end"), i.findtext("in"), i.findtext("out")) for i in items] == [
        ("0", "48", "0", "48"), ("48", "84", "24", "60")]  # the missing asset leaves its gap, not a broken clip
    assert items[0].find("file/pathurl").text.startswith("file://localhost/") and "%20file.bin" in items[0].find("file/pathurl").text
    audio = seq.find("./media/audio/track/clipitem")
    assert audio.findtext("end") == "96" and audio.find("file/pathurl") is not None
    assert [m.findtext("name") for m in seq.findall("marker")] == ["lamps & road"]


def test_edl_events_and_windows_paths(tmp_path):
    edl = exporters.to_edl(_timeline(), _lookup(tmp_path))
    lines = edl.splitlines()
    assert lines[0] == "TITLE: Night Walk 916" and lines[1] == "FCM: NON-DROP FRAME"
    assert "001  AX       V     C        00:00:00:00 00:00:02:00 00:00:00:00 00:00:02:00" in edl
    assert "002  AX       V     C        00:00:01:00 00:00:02:12 00:00:02:00 00:00:03:12" in edl
    assert "* FROM CLIP NAME: clip file.bin" in edl and "* LYRIC 00:00:00:12 lamps & road" in edl
    assert exporters.file_url(Path("C:/Users/x y/a.mp4")).startswith("file://localhost/")


def test_export_route_zip_and_production_settings(client):
    c, app, _ = client
    store = app.state.store
    pid = store.create_project("Export")["id"]
    a, b = _asset(store, pid), _asset(store, pid)
    tl = store.create_timeline(pid, "Cut", "16:9", 24, tracks=[{"type": "visual", "clips": [
        {"asset_id": a, "kind": "image", "start_s": 0, "duration_s": 1}, {"asset_id": b, "kind": "image", "start_s": 1, "duration_s": 1}]}])
    r = c.get(f"/api/timelines/{tl['id']}/export")
    assert r.status_code == 200 and r.headers["content-type"] == "application/zip"
    names = zipfile.ZipFile(io.BytesIO(r.content)).namelist()
    assert sorted(names) == ["Cut.edl", "Cut.xml", "README.txt"]
    assert c.get(f"/api/timelines/{tl['id']}/export?format=edl").text.startswith("TITLE: Cut")
    assert c.get(f"/api/timelines/{tl['id']}/export?format=pdf").status_code == 400
    r = c.post("/api/agent/studio_export_timeline", json={"timeline_id": tl["id"]})
    assert r.status_code == 200 and r.json()["clips"] == 2

    state = prod.create_production(store.data_dir, "Pilot", prod.normalise_spec(tiny_spec()), {"song_review": True}, project_id=pid)
    r = c.patch(f"/api/productions/{state['slug']}/settings", json={"autopilot": True})
    assert r.status_code == 200, r.text
    assert r.json()["autopilot"] is True
    saved = prod.load_state(store.data_dir, state["slug"])["settings"]
    assert saved["animatic_autocontinue"] is True and saved["song_review"] is False
    r = c.post(f"/api/agent/studio_production_settings?production={state['slug']}", json={"autopilot": False})
    assert r.json()["autopilot"] is False
    pre = c.get(f"/api/productions/{state['slug']}/preflight").json()
    assert isinstance(pre["ok"], bool) and isinstance(pre["items"], list)
    assert c.post("/api/agent/studio_export_timeline", json={"production": state["slug"]}).status_code == 400


def test_caption_sidecars_keep_timeline_times_words_and_source(tmp_path):
    from prosperos_hoard.hoard_link.media import subs
    timeline = _timeline()
    timeline['tracks'][1]['clips'] = [
        {'text': 'Fuera', 'start_s': 5, 'end_s': 6},
        {'text': 'Última & <letra>', 'start_s': 3.5, 'end_s': 5, 'karaoke': True,
         'words': [{'text': 'Última', 'start_s': 3.5, 'end_s': 4.2}, {'text': 'fuera', 'start_s': 4.2, 'end_s': 5}]},
        {'text': 'Primera', 'start_s': .5, 'end_s': 2}
    ]
    before = copy.deepcopy(timeline)
    data = exporters.package(timeline, _lookup(tmp_path), 'Con letra')
    with zipfile.ZipFile(io.BytesIO(data)) as z:
        srt = z.read('Con_letra.srt').decode('utf-8')
        vtt = z.read('Con_letra.vtt').decode('utf-8')
        cues = json.loads(z.read('Con_letra.captions.json'))
        assert '00:00:00,500 --> 00:00:02,000' in srt
        assert '00:00:03,500 --> 00:00:04,000' in srt
        assert 'Fuera' not in srt and 'Última & <letra>' in srt
        assert 'Última &amp; &lt;letra&gt;' in vtt
        parsed = subs.parse_srt(srt)
        assert [(c.start_s, c.end_s) for c in parsed] == [(.5, 2), (3.5, 4)]
        assert cues['time_base'] == 'timeline_seconds'
        assert cues['cues'][1]['karaoke'] is True
        assert cues['cues'][1]['words'] == [{'text': 'Última', 'start_s': 3.5, 'end_s': 4}]
        assert 'import the SRT or VTT separately' in z.read('README.txt').decode()
    assert timeline == before


def test_caption_downloads_and_agent_links_share_the_same_cut(client):
    c, app, _ = client
    store = app.state.store
    pid = store.create_project('Caption export')['id']
    asset = _asset(store, pid)
    timeline = store.create_timeline(pid, 'Subtítulos', '16:9', 24, tracks=[
        {'type': 'visual', 'clips': [{'asset_id': asset, 'kind': 'image', 'start_s': 0, 'duration_s': 2}]},
        {'type': 'lyrics', 'clips': [{'text': 'Hola', 'start_s': .25, 'end_s': 1.75}]}])
    before = store.get_timeline(timeline['id'])
    result = c.post('/api/agent/studio_export_timeline', json={'timeline_id': timeline['id']}).json()
    assert result['captions'] == 1
    srt = c.get(result['srt']); vtt = c.get(result['vtt'])
    assert srt.status_code == vtt.status_code == 200
    assert '00:00:00,250 --> 00:00:01,750' in srt.text
    assert vtt.text.startswith('WEBVTT') and '00:00:00.250 --> 00:00:01.750' in vtt.text
    bundle = c.get(result['download'])
    with zipfile.ZipFile(io.BytesIO(bundle.content)) as z:
        assert next(z.read(n).decode('utf-8') for n in z.namelist() if n.endswith('.srt')) == srt.text
        assert next(z.read(n).decode('utf-8') for n in z.namelist() if n.endswith('.vtt')) == vtt.text
    assert store.get_timeline(timeline['id']) == before
