"""project_from_timeline: an edit from another app (FCP7 XML, EDL or a plan) becomes a project.

The two fixtures are what the video studio's exporter writes for the same cut (four picture clips, one with a trim, a still, a song with an
apostrophe in its name, two sung lines as markers); ``@MEDIA@`` is replaced by this machine's folder, quoted as the exporter does."""

import json
import shutil
from pathlib import Path
from urllib.parse import quote

import pytest

from conftest import ROOT, make_services, needs_ffmpeg
from prosperos_hoard.editing import agent_tools, timeline_import
from prosperos_hoard.editing import projects as store
from prosperos_hoard.editing.errors import LumiereError, NotFound, Refused

pytestmark = needs_ffmpeg
FIX = ROOT / "editing_tests" / "fixtures"


@pytest.fixture
def folder(tmp_path, media_dir):
    """The cut's media under the names the fixtures use (spaces and an apostrophe on purpose), in a folder with a space too."""
    d = tmp_path / "Mis medios"
    d.mkdir()
    shutil.copy(media_dir / "talk.mp4", d / "shot one.mp4")
    shutil.copy(media_dir / "vert.mp4", d / "shot two.mp4")
    shutil.copy(media_dir / "pic.png", d / "still.png")
    shutil.copy(media_dir / "clicks.wav", d / "Ana's song.wav")
    return d


def quoted_folder(folder: Path) -> str:
    path=folder.resolve().as_posix()
    if len(path)>1 and path[1]==':':
        path='/'+path  # file://localhost/D%3A/..., never a hostname localhostD%3A
    return quote(path,safe='/')


def write_fixture(folder: Path, name: str, target: str = "") -> Path:
    text = (FIX / name).read_text(encoding="utf-8").replace("@MEDIA@", quoted_folder(folder))
    out = folder / (target or name)
    out.write_text(text, encoding="utf-8")
    return out


def layout(svc, pid):
    p = store.doc(svc, pid)
    media = {m["id"]: Path(m["path"]).name for m in svc.db.query("SELECT id, path FROM media")}
    video = [(media[c.media], c.start, c.src_in, c.src_out) for c in p.main_track().clips]
    audio = [(media[c.media], c.start, c.src_in, c.src_out) for t in p.tracks if t.kind == "audio" for c in t.clips]
    return p, video, audio


def check_prospero_cut(svc, res, canvas=(1280, 720, 24)):
    p, video, audio = layout(svc, res["project_id"])
    assert video[0] == ("shot one.mp4", 0, 1000, 4000)
    assert video[1] == ("shot two.mp4", 3000, 0, 2500)
    assert video[2][:2] == ("still.png", 5500)
    assert video[3] == ("shot one.mp4", 7500, 6000, 7500)
    assert p.main_track().clips[2].duration == 2000
    assert audio == [("Ana's song.wav", 0, 0, 9000)]
    assert (p.canvas.width, p.canvas.height, p.canvas.fps) == canvas
    return p


def test_the_studios_xml_becomes_a_project_exactly(services, folder):
    xml = write_fixture(folder, "prospero_cut.xml")
    res = timeline_import.project_from_timeline(services, fcpxml_path=str(xml))
    assert res["ok"] and res["source"] == "xmeml" and res["clips"] == 5 and res["skipped"] == [] and res["name"] == "Mi corte"
    assert res["url"].endswith(f"/#/p/{res['project_id']}") and res["duration_ms"] == 9000
    p = check_prospero_cut(services, res)
    assert [(m.t, m.label) for m in p.markers] == [(500, "first line"), (4000, "second <line> & more")]   # XML escapes read back as text


def test_the_studios_edl_gives_the_same_project(services, folder):
    edl = write_fixture(folder, "prospero_cut.edl")
    res = timeline_import.project_from_timeline(services, edl_path=str(edl), title="Desde EDL")
    assert res["source"] == "edl" and res["name"] == "Desde EDL" and res["clips"] == 5
    p = check_prospero_cut(services, res, canvas=(1920, 1080, 24))      # an EDL has no canvas: the first picture's shape, at 24 fps (fps= changes it)
    assert [(m.t, m.label) for m in p.markers] == [(500, "first line"), (4000, "second <line> & more")]


def test_the_edl_files_are_found_by_name_in_media_dirs(services, folder, tmp_path):
    edl = write_fixture(folder, "prospero_cut.edl")
    elsewhere = tmp_path / "otro sitio"
    elsewhere.mkdir()
    moved = elsewhere / "cut.edl"
    shutil.copy(edl, moved)
    with pytest.raises(LumiereError) as lost:                              # not next to the media, and nothing says where they are
        timeline_import.project_from_timeline(services, edl_path=str(moved))
    assert lost.value.code == "media_missing"
    res = timeline_import.project_from_timeline(services, edl_path=str(moved), media_dirs=[str(folder)])
    assert res["clips"] == 5 and services.db.query("SELECT COUNT(*) c FROM projects")[0]["c"] == 1   # the failed try left nothing behind


def test_an_xml_whose_files_moved_still_finds_them(services, folder, tmp_path):
    xml = write_fixture(folder, "prospero_cut.xml")
    text = xml.read_text(encoding="utf-8").replace(quoted_folder(folder), "/nowhere/at/all")
    lost = tmp_path / "moved.xml"
    lost.write_text(text, encoding="utf-8")
    res = timeline_import.project_from_timeline(services, fcpxml_path=str(lost), media_dirs=[str(folder)])
    assert res["clips"] == 5 and res["skipped"] == []


def test_a_missing_file_is_skipped_and_listed_and_the_rest_arrives(services, folder):
    (folder / "shot two.mp4").unlink()
    res = timeline_import.project_from_timeline(services, fcpxml_path=str(write_fixture(folder, "prospero_cut.xml")))
    assert res["clips"] == 4 and [s["name"] for s in res["skipped"]] == ["shot two.mp4"] and "no file" in res["skipped"][0]["reason"]
    _, video, _ = layout(services, res["project_id"])
    assert [v[:2] for v in video] == [("shot one.mp4", 0), ("still.png", 5500), ("shot one.mp4", 7500)]     # its place stays empty


def test_an_unreadable_media_is_reported_without_losing_the_rest(services, folder, monkeypatch):
    denied = folder/'shot two.mp4'
    original = Path.is_file
    def readable(path):
        if path == denied:
            raise PermissionError('simulated inaccessible media')
        return original(path)
    monkeypatch.setattr(Path,'is_file',readable)
    result=timeline_import.project_from_timeline(services,fcpxml_path=str(write_fixture(folder,'prospero_cut.xml')))
    assert result['clips']==4
    assert len(result['skipped'])==1 and 'inaccessible' in result['skipped'][0]['reason']
    _, video, _ = layout(services,result['project_id'])
    assert [v[:2] for v in video] == [('shot one.mp4',0),('still.png',5500),('shot one.mp4',7500)]


def test_a_plan_with_tracks_and_markers(services, folder):
    plan = {"clips": [{"path": str(folder / "shot one.mp4"), "in_s": 2, "out_s": 5},
                      {"path": str(folder / "shot one.mp4"), "in_s": 8, "out_s": 9.5},
                      {"path": str(folder / "shot two.mp4"), "in_s": 0, "out_s": 2, "track": 1, "start_s": 1},
                      {"path": str(folder / "Ana's song.wav"), "in_s": 0, "out_s": 4.5, "track": 0}],
            "markers": [{"t": 3, "text": "chapter"}]}
    res = timeline_import.project_from_timeline(services, plan=plan, title="Plan")
    p = store.doc(services, res["project_id"])
    videos = [t for t in p.tracks if t.kind == "video"]
    assert len(videos) == 2
    assert [(c.start, c.src_in, c.src_out) for c in videos[0].clips] == [(0, 2000, 5000), (3000, 8000, 9500)]     # the second follows the first
    assert [(c.start, c.src_in, c.src_out) for c in videos[1].clips] == [(1000, 0, 2000)]
    assert [(m.t, m.label) for m in p.markers] == [(3000, "chapter")]
    assert (p.canvas.width, p.canvas.height, p.canvas.fps) == (1920, 1080, 30)                                  # defaults when the plan says nothing
    as_text = timeline_import.project_from_timeline(services, plan=json.dumps(plan), title="Plan 2", fps=25)
    assert services.db.query("SELECT COUNT(*) c FROM projects")[0]["c"] == 2 and "25" in as_text["canvas"]


def test_a_vertical_first_clip_makes_a_vertical_project(services, folder):
    res = timeline_import.project_from_timeline(services, plan={"clips": [{"path": str(folder / "shot two.mp4"), "in_s": 0, "out_s": 3}]})
    assert res["canvas"].startswith("1080x1920")


def test_clips_longer_than_their_file_are_trimmed_to_it(services, folder):
    res = timeline_import.project_from_timeline(services, plan={"clips": [{"path": str(folder / "shot two.mp4"), "in_s": 5, "out_s": 60}]})
    _, video, _ = layout(services, res["project_id"])
    assert video == [("shot two.mp4", 0, 5000, 6000)]


def test_bad_requests_say_what_is_wrong(services, folder):
    with pytest.raises(LumiereError, match="exactly one"):
        timeline_import.project_from_timeline(services)
    with pytest.raises(LumiereError, match="exactly one"):
        timeline_import.project_from_timeline(services, plan={"clips": []}, edl_path="x.edl")
    with pytest.raises(LumiereError, match="clips"):
        timeline_import.project_from_timeline(services, plan={"clips": []})
    with pytest.raises(LumiereError, match="out_s"):
        timeline_import.project_from_timeline(services, plan={"clips": [{"path": "a.mp4", "in_s": 3, "out_s": 1}]})
    with pytest.raises(NotFound):
        timeline_import.project_from_timeline(services, fcpxml_path=str(folder / "nope.xml"))
    bad = folder / "bad.xml"
    bad.write_text("<xmeml><oops", encoding="utf-8")
    with pytest.raises(LumiereError, match="could not be read"):
        timeline_import.project_from_timeline(services, fcpxml_path=str(bad))
    bad.write_text('<?xml version="1.0"?><!DOCTYPE x [<!ENTITY a "b">]><xmeml version="5"><sequence/></xmeml>', encoding="utf-8")
    with pytest.raises(LumiereError, match="entities"):
        timeline_import.project_from_timeline(services, fcpxml_path=str(bad))
    bad.write_text("<xmeml version='5'><project/></xmeml>", encoding="utf-8")
    with pytest.raises(LumiereError, match="sequence"):
        timeline_import.project_from_timeline(services, fcpxml_path=str(bad))
    assert services.db.query("SELECT COUNT(*) c FROM projects")[0]["c"] == 0


def test_files_outside_the_allowed_folders_are_refused(tmp_path, folder):
    svc = make_services(tmp_path / "svc", file_roots=[folder.resolve()])
    svc.start()
    try:
        plan = {"clips": [{"path": str(tmp_path / "elsewhere.mp4"), "in_s": 0, "out_s": 1}]}
        with pytest.raises(LumiereError) as err:
            timeline_import.project_from_timeline(svc, plan=plan)
        assert err.value.code == "media_missing" and "LUMIERE_FILE_ROOTS" in str(err.value)
        with pytest.raises(Refused):
            timeline_import.project_from_timeline(svc, fcpxml_path=str(tmp_path / "x.xml"))
        ok = timeline_import.project_from_timeline(svc, plan={"clips": [{"path": str(folder / "shot one.mp4"), "in_s": 0, "out_s": 1}]})
        assert ok["clips"] == 1
    finally:
        svc.stop()


def test_the_tool_and_the_http_route(client, folder):
    xml = write_fixture(folder, "prospero_cut.xml")
    token = client.svc.token
    r = client.post("/api/agent/call", json={"name": "project_from_timeline", "arguments": {"title": "Por HTTP", "fcpxml_path": str(xml)}, "reason": "Open the edit made elsewhere"},
                    headers={"Authorization": f"Bearer {token}"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] and body["project_id"] and body["name"] == "Por HTTP" and body["clips"] == 5
    assert client.get(f"/api/projects/{body['project_id']}").status_code == 200
    names = [t["name"] for t in client.get("/api/agent/tools").json()["tools"]]
    assert "project_from_timeline" in names
    tool = {t.name: t for t in agent_tools.TOOLS}["project_from_timeline"]
    assert tool.annotations["readOnlyHint"] is False
    two = client.post("/api/agent/call", json={"name": "project_from_timeline", "arguments": {"title": "x"}, "reason": "Open an edit with no file"}, headers={"Authorization": f"Bearer {token}"})
    assert two.status_code == 400 and "exactly one" in two.json()["error"]


def test_the_edl_reader_in_isolation():
    tl = timeline_import.parse_edl("TITLE: T\nFCM: NON-DROP FRAME\n\n001  AX  V  C  00:00:01:00 00:00:02:00 00:00:00:00 00:00:01:00\n"
                                   "* FROM CLIP NAME: a b.mp4\n002  AX  A  C  00:00:00:00 00:00:01:12 00:00:00:00 00:00:01:12\n"
                                   "* FROM CLIP NAME: s.wav\n* LYRIC 00:00:00:12 hola\n", fps=24)
    assert tl.title == "T" and [(i.ref, i.kind, i.start_ms, i.in_ms, i.out_ms) for i in tl.items] == [
        ("a b.mp4", "video", 0, 1000, 2000), ("s.wav", "audio", 0, 0, 1500)]
    assert tl.markers == [(500, "hola")]


def test_file_urls_read_back_to_paths():
    f = timeline_import.url_to_path
    assert f("file://localhost/C%3a/Users/Ana%20Gil/a%27b.mp4") == "C:/Users/Ana Gil/a'b.mp4"
    assert f("file:///tmp/x%20y.mp4") == "/tmp/x y.mp4" and f("/plain/path.mp4") == "/plain/path.mp4" and f("") == ""
