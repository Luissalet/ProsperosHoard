"""The server side of the accelerated (WebGL) preview: the LUT route, the colour matrix of a media file and the output-size
scaling of pixel-sized effects. The browser side is checked by scripts/preview_check.py (it needs a browser)."""

import json
import ast
import re


from conftest import ROOT, _ff, make_services, needs_ffmpeg
from prosperos_hoard.editing import media as media_store
from prosperos_hoard.editing import projects as store
from prosperos_hoard.editing.main import create_app
from prosperos_hoard.editing.render import filters as fx

CUBE = 'TITLE "t"\nLUT_3D_SIZE 2\n' + "\n".join(f"{r} {g} {b}" for b in (0, 1) for g in (0, 1) for r in (0, 1)) + "\n"


def test_lut_route_serves_only_cube_text(client, tmp_path):
    lut = tmp_path / "grade.cube"
    lut.write_text(CUBE, encoding="utf-8")
    ok = client.get("/api/luts", params={"path": str(lut)})
    assert ok.status_code == 200 and ok.text.startswith('TITLE "t"') and ok.headers["content-type"].startswith("text/plain")
    other = tmp_path / "secrets.txt"
    other.write_text("not a LUT", encoding="utf-8")
    assert client.get("/api/luts", params={"path": str(other)}).status_code in (400, 403)
    assert client.get("/api/luts", params={"path": str(tmp_path / "missing.cube")}).status_code == 404


def test_lut_route_respects_file_roots(tmp_path):
    from fastapi.testclient import TestClient

    allowed = tmp_path / "allowed"
    allowed.mkdir()
    (allowed / "in.cube").write_text(CUBE, encoding="utf-8")
    (tmp_path / "out.cube").write_text(CUBE, encoding="utf-8")
    svc = make_services(tmp_path, file_roots=(allowed,))
    with TestClient(create_app(svc.config, svc), base_url="http://127.0.0.1") as c:
        assert c.get("/api/luts", params={"path": str(allowed / "in.cube")}).status_code == 200
        assert c.get("/api/luts", params={"path": str(tmp_path / "out.cube")}).status_code in (400, 403)


def test_blur_and_pixelate_scale_with_the_output():
    """Sizes are in canvas pixels: the same blur at half the output width is half the sigma, so a small preview looks like the export."""
    from prosperos_hoard.editing.timeline import Filter

    chain_full = fx.video_chain([Filter(type="blur", params={"radius": 8}), Filter(type="pixelate", params={"size": 16})], {"lut_name": lambda f: "", "factor": 1.0})
    chain_half = fx.video_chain([Filter(type="blur", params={"radius": 8}), Filter(type="pixelate", params={"size": 16})], {"lut_name": lambda f: "", "factor": 0.5})
    assert chain_full[0] == "gblur=sigma=8.00" and chain_half[0] == "gblur=sigma=4.00"
    assert chain_full[1] == "scale=iw/16:ih/16:flags=neighbor" and chain_half[1] == "scale=iw/8:ih/8:flags=neighbor"
    # a context without a factor (older callers) behaves as before
    assert fx.video_chain([Filter(type="blur", params={"radius": 3})], {"lut_name": lambda f: ""}) == ["gblur=sigma=3.00"]


@needs_ffmpeg
def test_media_reports_its_colour_matrix_and_old_media_are_filled_in(services, tmp_path, media_dir):
    tagged = tmp_path / "tagged.mp4"
    _ff("-f", "lavfi", "-i", "testsrc=s=320x180:r=10:d=1", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-colorspace", "bt709",
        "-color_primaries", "bt709", "-color_trc", "bt709", str(tagged))
    new = media_store.import_path(services, str(tagged))
    assert new["color_space"] == "bt709"
    assert media_store.get(services, new["id"])["color_space"] == "bt709"
    pic = media_store.import_path(services, str(media_dir / "pic.png"))
    assert isinstance(pic["color_space"], str)

    # an entry imported before the field existed: the project view probes it once and keeps the answer
    row = services.db.one("SELECT probe FROM media WHERE id = ?", (new["id"],))
    probe = json.loads(row["probe"])
    probe.pop("color_space")
    services.db.execute("UPDATE media SET probe = ? WHERE id = ?", (json.dumps(probe), new["id"]))
    assert media_store.get(services, new["id"])["color_space"] is None
    project = store.create(services, "p", preset="hd720", media=[new["id"]])
    assert store.view(services, project["id"])["media"][new["id"]]["color_space"] == "bt709"
    assert media_store.get(services, new["id"])["color_space"] == "bt709"


@needs_ffmpeg
def test_exact_frame_blocks_follow_the_output_size(services, media_dir):
    """The pixelate block is 16 canvas pixels: a 640 px wide frame of a 1280 px canvas shows 8 px blocks, like the export scaled down."""
    from PIL import Image

    mid = media_store.import_path(services, str(media_dir / "talk.mp4"))["id"]
    pid = store.create(services, "px", preset="hd720", media=[mid])["id"]
    clip = store.doc(services, pid).tracks[0].clips[0].id
    store.edit(services, pid, [{"op": "filter_add", "clips": [clip], "type": "pixelate", "params": {"size": 16}}])
    from prosperos_hoard.editing.render import runner

    def block_width(path) -> int:
        row = Image.open(path).convert("L").crop((0, 100, Image.open(path).width, 101))
        values = list(row.getdata())
        runs, run = [], 1
        for a, b in zip(values, values[1:]):
            if abs(a - b) < 3:
                run += 1
            else:
                runs.append(run)
                run = 1
        return min(r for r in runs if r > 1) if any(r > 1 for r in runs) else 0

    wide = block_width(runner.render_frame(services, pid, 1500, width=1280))
    half = block_width(runner.render_frame(services, pid, 1500, width=640))
    assert wide >= 12 and 0 < half <= wide * 0.75, (wide, half)


@needs_ffmpeg
def test_exact_frame_draws_fades_and_transitions(services, media_dir):
    """A single frame taken part-way through a crossfade or a fade-in shows that moment (it used to show the outgoing clip /
    the full-strength picture), so it agrees with the export and with the editor's accelerated preview."""
    from PIL import Image

    from prosperos_hoard.editing.render import runner

    mid = media_store.import_path(services, str(media_dir / "scenes.mp4"))["id"]
    pid = store.create(services, "fx", preset="hd720")["id"]
    first = store.edit(services, pid, [{"op": "add_media", "media": mid, "src_in": 0, "src_out": 2000}])["results"][0]["clip"]
    second = store.edit(services, pid, [{"op": "add_media", "media": mid, "src_in": 2000, "src_out": 4000}])["results"][0]["clip"]
    store.edit(services, pid, [{"op": "transition", "clip": second, "type": "crossfade", "dur": 1000}])
    centre = lambda path: Image.open(path).convert("RGB").getpixel((Image.open(path).width // 2, Image.open(path).height // 2))  # noqa: E731

    r, g, b = centre(runner.render_frame(services, pid, 1500, width=320))
    assert r > 90 and 40 < g < 90 and b < 40, (r, g, b)  # half red (254) and half green (128)
    r, g, b = centre(runner.render_frame(services, pid, 1200, width=320))
    assert r > g + 40, (r, g, b)  # a third of the way in: mostly red still

    store.edit(services, pid, [{"op": "set", "clip": first, "props": {"fade_in": 1000}}])
    r, g, b = centre(runner.render_frame(services, pid, 500, width=320))
    assert 90 < r < 170 and g < 40, (r, g, b)  # half way up the fade-in
    store.edit(services, pid, [{"op": "set", "clip": second, "props": {"fade_out": 1000}}])
    r, g, b = centre(runner.render_frame(services, pid, 2500, width=320))
    assert 40 < g < 90 and r < 40, (r, g, b)  # half way down the fade-out


def test_preview_strings_exist_in_both_languages():
    text = (ROOT / "editing_frontend" / "src" / "i18n.js").read_text(encoding="utf-8")
    for key in ("gl_preview", "gl_preview_help", "gl_preview_unavailable", "gl_preview_settings", "gl_preview_status_on", "gl_preview_status_off",
                "gl_preview_status_missing"):
        assert len(re.findall(rf"\b{key}:", text)) == 2, key


def test_the_preview_check_script_stays_out_of_pytest():
    script = ROOT / "scripts" / "editing_preview_check.py"
    assert script.exists() and not script.name.startswith("test_")
    ast.parse(script.read_text(encoding="utf-8"))  # it is valid Python (parsed here: the folder name may hold quotes)
