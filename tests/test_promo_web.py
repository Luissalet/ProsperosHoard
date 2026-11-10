"""Promo from a web page: brand scan, script, checks and fixes, sound, the REST routes and one short real render.

The browser is simulated (a fake page collector) everywhere except the last test, which drives the real headless
browser and ffmpeg on a tiny local page and is skipped when either is missing."""
from __future__ import annotations

import io
import subprocess
import time
import wave
from pathlib import Path

import numpy as np
import pytest
from PIL import Image, ImageDraw

from prosperos_hoard import promo_brand as brand
from prosperos_hoard import promo_compose as compose
from prosperos_hoard import promo_sound as sound
from prosperos_hoard import promo_web as web
from prosperos_hoard.backend import ffmpeg_path


# ------------------------------------------------------------------ a simulated page

def _png(colors: list[tuple[int, int, int]]) -> bytes:
    img = Image.new("RGB", (600, 400), (250, 250, 250))
    draw = ImageDraw.Draw(img)
    for n, c in enumerate(colors):
        draw.rectangle([n * 150, 0, n * 150 + 140, 200], fill=c)
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def _logo() -> bytes:
    img = Image.new("RGBA", (300, 100), (0, 0, 0, 0))
    ImageDraw.Draw(img).rectangle([10, 10, 290, 90], fill=(20, 90, 200, 255))
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


def fake_page(url: str) -> dict:
    return {"final_url": url, "title": "Acme Rockets - launch things", "site_name": "Acme", "lang": "en",
            "h1": "Rockets for every backyard", "description": "Acme builds small rockets that anyone can launch in an afternoon.",
            "theme_color": "#145ac8", "screenshots": [_png([(20, 90, 200), (240, 120, 20)])],
            "element_colors": [{"role": "button", "bg": "rgb(20, 90, 200)", "color": "rgb(255, 255, 255)"}],
            "css_vars": {"--brand-primary": "#145ac8", "--accent": "#f07814"},
            "texts": [{"tag": "h2", "text": "Built in a day"}, {"tag": "h2", "text": "Safe for the whole family"},
                      {"tag": "h2", "text": "Free shipping worldwide"}, {"tag": "p", "text": "We test every rocket twice before it leaves the workshop."}],
            "logo_png": _logo(), "logo_from": "page", "fonts": {}}


# ------------------------------------------------------------------ brand

def test_urls_are_normalised_and_checked():
    assert brand.normalise_url("example.com") == "https://example.com"
    with pytest.raises(brand.PromoBrandError) as e:
        brand.normalise_url("ftp://example.com")
    assert e.value.code == "bad_url"
    with pytest.raises(brand.PromoBrandError) as e:
        brand.normalise_url("  ")
    assert e.value.code == "url_required"


def test_colours_parse_and_contrast():
    assert brand.parse_color("rgb(10, 20, 30)")[:3] == (10, 20, 30)
    assert brand.parse_color("#fff")[:3] == (255, 255, 255)
    assert brand.parse_color("transparent") is None or brand.parse_color("transparent")[3] == 0
    assert brand.contrast((0, 0, 0), (255, 255, 255)) == pytest.approx(21, abs=0.1)


def test_link_tails_and_noise_are_cleaned():
    assert not brand.clean_text("Fast and simple. Learn more >", 100).lower().endswith("learn more >")
    kept = brand.pick_phrases([{"tag": "h2", "text": "Built in a day"}, {"tag": "h2", "text": "Accept all cookies"},
                               {"tag": "h2", "text": "Built in a day"}], "Head", "Value")
    assert "Built in a day" in kept and not any("cookies" in k.lower() for k in kept) and len(kept) == len(set(kept))


def test_open_fonts_are_kept_and_closed_ones_replaced():
    assert brand.licence_allows("Inter", "https://fonts.gstatic.com/x.woff2")[0]
    assert not brand.licence_allows("Proprietary Sans", "https://site.example/p.woff2")[0]
    assert brand.norm_family("SourceSansPro-Bold") == brand.norm_family("Source Sans Pro")
    sub = brand.substitute_for("Proprietary Sans", "'Proprietary Sans', sans-serif")
    assert sub and isinstance(sub, dict)


def test_scan_builds_and_keeps_a_kit(data_dir):
    kit = brand.scan(data_dir, "acme.test", collector=fake_page)
    assert kit["host"] == "acme.test" and kit["site_name"] == "Acme" and kit["headline"].startswith("Rockets")
    assert {"primary", "dark", "light"} <= set(kit["palette"]) and len(kit["colors"]) >= 3
    folder = brand.kit_folder(data_dir, kit["id"])
    assert (folder / "kit.json").exists() and (folder / "logo.png").exists() and (folder / "brand-board.png").exists()
    assert brand.load_kit(data_dir, kit["id"])["id"] == kit["id"]
    assert [k["id"] for k in brand.list_kits(data_dir)] == [kit["id"]]


# ------------------------------------------------------------------ script and page

@pytest.fixture
def kit(data_dir):
    return brand.scan(data_dir, "acme.test", collector=fake_page)


def test_requests_are_validated():
    assert compose.check_request(None, None, None, None) == (30.0, ["16:9", "9:16"], "keynote", 60)
    for args, code in [((3, None, None, None), "bad_length"), ((30, ["4:3"], None, None), "bad_format"),
                       ((30, None, "loud", None), "bad_vibe"), ((30, None, None, 59), "bad_fps"), (("x", None, None, None), "bad_length")]:
        with pytest.raises(compose.PromoError) as e:
            compose.check_request(*args)
        assert e.value.code == code


@pytest.mark.parametrize("vibe", list(compose.VIBES))
def test_script_fills_the_length_in_every_vibe(kit, vibe):
    script = compose.plan_script(kit, 20, vibe, "auto", None)
    shots = script["shots"]
    assert shots[0]["kind"] == "hook" and shots[-1]["kind"] == "cta" and any(s["kind"] == "logo" for s in shots)
    assert abs(shots[-1]["start"] + shots[-1]["dur"] - 20) < 0.05
    assert all(b["start"] >= a["start"] + a["dur"] - 1e-6 for a, b in zip(shots, shots[1:]))
    assert script["written_by"] == "template" and script["bpm"] > 0


def test_a_model_may_write_the_texts_but_a_failure_falls_back(kit):
    def broken(*a, **k):
        raise RuntimeError("no model")
    assert compose.plan_script(kit, 12, "launch", "auto", broken)["written_by"] == "template"


def test_page_is_built_with_the_timeline_hooks(kit, tmp_path):
    script = compose.plan_script(kit, 10, "keynote", "auto", None)
    html = compose.build_html(kit, script, 1080, 1920, 30, tmp_path / "p").read_text(encoding="utf-8")
    assert "window.seek" in html and "window.measure" in html and "1080" in html
    assert kit["headline"].split()[0] in html


# ------------------------------------------------------------------ checks and fixes

def _item(**over):
    base = {"id": "a", "shot": "s1", "role": "text", "text": "Hello", "box": {"x0": 200, "y0": 300, "x1": 800, "y1": 420}, "sw": 100, "cw": 100,
            "fs": 90, "color": "rgb(255, 255, 255)", "bg": "rgb(0, 0, 0)"}
    return {**base, **over}


def test_dom_checks_find_the_four_text_problems():
    ok = [(1.0, {"W": 1920, "H": 1080, "items": [_item()]})]
    assert web.dom_issues(ok) == []
    bad = [(1.0, {"W": 1920, "H": 1080, "items": [
        _item(id="out", box={"x0": -20, "y0": 300, "x1": 800, "y1": 420}),
        _item(id="cut", sw=900, cw=500),
        _item(id="small", fs=10),
        _item(id="grey", color="rgb(120, 120, 120)", bg="rgb(130, 130, 130)"),
        _item(id="o1", box={"x0": 200, "y0": 600, "x1": 800, "y1": 700}), _item(id="o2", box={"x0": 300, "y0": 620, "x1": 900, "y1": 720})]})]
    kinds = {i["kind"] for i in web.dom_issues(bad)}
    assert {"text_out", "text_cut", "text_small", "contrast", "overlap"} <= kinds


def test_off_brand_colours_are_found_and_brand_blends_are_not():
    palette = {"primary": "#145ac8", "dark": "#101820", "light": "#f5f5f5"}
    allowed = web.allowed_colors(palette)
    script = {"shots": [{"key": "s1", "start": 0.0, "dur": 2.0}]}
    on = Image.new("RGB", (320, 180), (20, 90, 200))
    off = Image.new("RGB", (320, 180), (230, 40, 40))
    assert web.palette_issues({0.5: on}, script, allowed) == []
    found = web.palette_issues({0.5: off}, script, allowed)
    assert found and found[0]["kind"] == "palette"


def test_frozen_shots_and_jumps_are_reported():
    script = {"shots": [{"key": "s1", "kind": "hook", "start": 0.0, "dur": 3.0}], "settle_s": 0.2}
    flat = {t / 4: Image.new("RGB", (160, 90), (40, 40, 40)) for t in range(12)}
    issues, measured = web.motion_issues(flat, script)
    assert not issues or issues[0]["kind"] == "frozen"
    assert web.jump_issues({"s2": 80.0})[0]["kind"] == "jump" and web.jump_issues({"s2": 5.0}) == []


def test_fixes_change_the_script_and_say_so():
    script = {"shots": [{"key": "s1", "kind": "point", "fit": 1.0, "text": "x " * 40, "bg_role": "dark", "drift": 1.0}], "wipe_s": 0.5, "deco": True}
    done = web.apply_fixes(script, [{"kind": "text_cut", "shot": "s1"}, {"kind": "palette", "shot": "s1"}, {"kind": "jump", "shot": "s1"},
                                    {"kind": "contrast", "shot": "s1"}])
    assert script["shots"][0]["fit"] < 1.0 and script["deco"] is False and script["wipe_s"] > 0.5 and script["shots"][0]["bg_role"] == "light"
    assert len(done) == 4
    assert web.apply_fixes(script, []) == []


def test_contact_sheet_lays_frames_in_a_grid():
    items = [(n / 4, Image.new("RGB", (192, 108), (n * 10, 20, 20))) for n in range(9)]
    sheet = web.make_sheet(items, thumb_w=96, columns=3, title="test")
    assert sheet.width >= 3 * 96 and sheet.height > 3 * 54


# ------------------------------------------------------------------ sound

def test_sound_events_follow_the_shots_and_the_tempo():
    script = compose.plan_script({"host": "a.test", "site_name": "A", "headline": "Head line here", "value_prop": "A value", "phrases": ["One", "Two", "Three"],
                                  "palette": {"primary": "#145ac8", "dark": "#101820", "light": "#f5f5f5", "accent": "#f07814", "secondary": "#145ac8"}},
                                 16, "launch", "en", None)
    events = sound.events_for(script["shots"], script["bpm"], 16)
    kinds = {e["kind"] for e in events}
    assert {"beat", "whoosh", "hit"} <= kinds
    assert all(0 <= e["t"] <= 16 for e in events)
    audio = sound.synthesise(events, 16)
    assert audio.shape[-1] >= 16 * sound.SR and np.max(np.abs(audio)) <= 1.0 and np.max(np.abs(audio)) > 0.05


def test_wav_is_written_and_music_is_mixed_under_the_effects(tmp_path):
    audio = sound.synthesise([{"t": 0.5, "kind": "hit", "level": 1.0}], 2)
    path = sound.write_wav(tmp_path / "a.wav", audio)
    with wave.open(str(path)) as w:
        assert w.getframerate() == sound.SR and w.getnchannels() == 2
    mixed = sound.mix_with_music(audio, np.zeros_like(audio) + 0.1)
    assert mixed.shape == audio.shape


@pytest.mark.skipif(not ffmpeg_path(), reason="ffmpeg missing")
def test_the_master_lands_near_the_loudness_target(tmp_path):
    events = sound.events_for([{"key": "s1", "kind": "hook", "start": 0.0, "dur": 4.0}, {"key": "s2", "kind": "logo", "start": 4.0, "dur": 4.0}], 120, 8)
    raw, out = tmp_path / "raw.wav", tmp_path / "master.wav"
    sound.write_wav(raw, sound.synthesise(events, 8))
    result = sound.master_wav(ffmpeg_path(), raw, out)
    assert abs(result["lufs"] - sound.TARGET_LUFS) < 1.0 and result["true_peak_db"] <= -1.0


# ------------------------------------------------------------------ REST

@pytest.fixture
def promo_client(client, monkeypatch):
    c, app, _ = client
    monkeypatch.setattr(brand, "collect_page", fake_page)
    return c, app


def _wait(c, job_id, timeout=120.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = c.get(f"/api/jobs/{job_id}").json()
        if job["state"] not in ("queued", "waiting_gpu", "running"):
            return job
        time.sleep(0.2)
    raise AssertionError("job did not finish")


def test_options_and_brand_scan_routes(promo_client):
    c, app = promo_client
    options = c.get("/api/promo/options").json()
    assert set(options["vibes"]) == {"keynote", "launch", "minimal"} and set(options["formats"]) == {"16:9", "9:16"}
    project = c.post("/api/projects", json={"name": "Promo", "brief": "x"}).json()
    scanned = c.post("/api/brand/scan", json={"url": "acme.test", "project": project["id"]}).json()
    assert scanned["host"] == "acme.test" and scanned["assets"]
    assert [k["id"] for k in c.get("/api/promo/kits").json()["items"]] == [scanned["id"]]
    assert c.get(f"/api/promo/kits/{scanned['id']}").json()["headline"].startswith("Rockets")
    assert c.post("/api/brand/scan", json={"url": "ftp://x"}).status_code in (400, 422)


def test_promo_requests_are_refused_before_a_job_exists(promo_client):
    c, app = promo_client
    project = c.post("/api/projects", json={"name": "Promo", "brief": "x"}).json()
    base = f"/api/projects/{project['id']}/promo"
    for body in ({"url": "acme.test", "length_s": 2}, {"url": "acme.test", "formats": ["4:3"]}, {"url": "acme.test", "vibe": "loud"}, {}):
        assert c.post(base, json=body).status_code in (400, 422)
    assert c.post("/api/projects/nope/promo", json={"url": "acme.test"}).status_code == 404


def test_contact_sheet_route_needs_a_video(promo_client):
    c, app = promo_client
    project = c.post("/api/projects", json={"name": "Promo", "brief": "x"}).json()
    scanned = c.post("/api/brand/scan", json={"url": "acme.test", "project": project["id"]}).json()
    refused = c.post("/api/contact-sheet", json={"asset_id": scanned["assets"]["logo"]})
    assert refused.status_code == 400 and "not_video" in refused.text


def test_agent_routes_exist_and_are_draft_safe(promo_client):
    c, app = promo_client
    project = c.post("/api/projects", json={"name": "Promo", "brief": "x"}).json()
    res = c.post("/api/agent/brand_scan", json={"url": "acme.test", "project": project["id"]})
    assert res.status_code == 200
    from prosperos_hoard import agent_undo
    for name in ("promo_from_url", "brand_scan", "contact_sheet"):
        assert name in agent_undo.DRAFT_SAFE


# ------------------------------------------------------------------ one short real render

def _real_browser() -> bool:
    try:
        with brand.open_browser():
            return True
    except Exception:
        return False


@pytest.mark.skipif(not ffmpeg_path(), reason="ffmpeg missing")
def test_a_short_promo_is_rendered_for_real(data_dir, store, project, tmp_path):
    if not _real_browser():
        pytest.skip("no headless browser")
    kit = brand.scan(data_dir, "acme.test", collector=fake_page)
    job = {"id": "t1", "params": {"project_id": project["id"], "kit_id": kit["id"], "length_s": 6, "formats": ["16:9"], "vibe": "minimal", "fps": 24,
                                  "language": "en"}}
    result = web.render_job(store, job, lambda *a, **k: None)
    video = result["videos"]["16:9"]
    asset = store.get_asset(video)
    path = Path(data_dir) / asset["file_path"]
    probe = subprocess.run([ffmpeg_path(), "-hide_banner", "-i", str(path)], capture_output=True, text=True)
    text = probe.stderr
    assert "1920x1080" in text and "yuv420p" in text and "24 fps" in text and "Audio: aac" in text
    info = result["files"]["16:9"]
    assert info["ok"] and info["frames"] == 6 * 24 and result["contact_sheets"]["16:9"]
    sheet = store.get_asset(result["contact_sheets"]["16:9"])
    assert sheet["kind"] == "image"
    again = web.contact_sheet_for_asset(store, video, fps=2)
    assert again["asset_id"] and again["width"] > 0
