import pytest
from PIL import Image

from prosperos_hoard import design, engine
from prosperos_hoard.jobs import JobQueue


def _no_progress(*_a, **_k):
    return None


# ------------------------------------------------------------- mentions

def test_expand_mentions_pulls_prompt_negative_and_reference(store, project):
    ref = engine.render_design(store, project["id"], "thumbnail", {"title": "ref"})
    store.create_character(project["id"], "Aria", prompt="an idol with silver hair", negative="extra fingers",
                           canonical_asset_id=ref["id"])
    result = engine.expand_mentions(store, project["id"], "@Aria singing under neon lights")
    assert result["expanded_prompt"] == "an idol with silver hair singing under neon lights"
    assert result["negative_extra"] == "extra fingers"
    assert result["reference_asset_id"] == ref["id"]
    assert result["matched_characters"] == ["Aria"]


def test_mentions_with_spaces_longest_match_and_aliases(store, project):
    store.create_character(project["id"], "Iris Volt", prompt="IRIS")
    store.create_character(project["id"], "Iris", prompt="SHORT")
    store.create_character(project["id"], "Mika Frost", prompt="MIKA")
    r = engine.expand_mentions(store, project["id"], "@Iris Volt and @iris, then @MikaFrost, @Mika_Frost and @Mika.")
    assert r["expanded_prompt"] == "IRIS and SHORT, then MIKA, MIKA and MIKA."
    assert r["matched_characters"] == ["Iris Volt", "Iris", "Mika Frost"]


def test_mentions_ignore_emails_partial_words_and_report_unknowns(store, project):
    store.create_character(project["id"], "Iris", prompt="IRIS", negative="blurry")
    r = engine.expand_mentions(store, project["id"], "mail fan@Iris.com, @Irisa, @Nobody and @Iris @Iris")
    assert r["expanded_prompt"] == "mail fan@Iris.com, @Irisa, @Nobody and IRIS IRIS"
    assert r["unknown_mentions"] == ["Irisa", "Nobody"]
    assert r["negative_extra"] == "blurry"  # not duplicated for repeated mentions


def test_mentions_ambiguous_first_name_is_not_guessed(store, project):
    store.create_character(project["id"], "Lena Echo", prompt="ECHO")
    store.create_character(project["id"], "Lena Rune", prompt="RUNE")
    r = engine.expand_mentions(store, project["id"], "@Lena smiles")
    assert r["matched_characters"] == [] and r["unknown_mentions"] == ["Lena"]


def test_mentions_regex_characters_in_names_are_literal(store, project):
    store.create_character(project["id"], "A.B (x)", prompt="DOTTY")
    r = engine.expand_mentions(store, project["id"], "@A.B (x) here, @AxB (x) not")
    assert r["expanded_prompt"].startswith("DOTTY here")
    assert "@AxB" in r["expanded_prompt"]


def test_duplicate_character_names_rejected(store, project):
    store.create_character(project["id"], "Iris", prompt="x")
    with pytest.raises(ValueError):
        store.create_character(project["id"], "iris", prompt="y")


def test_compose_prompt_style_by_name_and_negative_dedupe(store, project):
    store.create_character(project["id"], "Aria", prompt="blue hair", negative="watermark, text")
    composed = engine.compose_prompt(store, project["id"], "@Aria portrait", "text", "Neon night city")
    assert composed["positive_prompt"].startswith("neon-lit night city")
    assert "blue hair portrait" in composed["positive_prompt"]
    assert composed["negative_prompt"].lower().split(", ").count("text") == 1
    assert composed["style_defaults"]["checkpoint"] == "sd_xl_base_1.0.safetensors"
    with pytest.raises(engine.EngineError):
        engine.compose_prompt(store, project["id"], "x", None, "No such style")


# --------------------------------------------------------------- lineage

def _run_job(store, backend, type_, params, project_id):
    queue = JobQueue(store)
    handlers = {"generate_image": engine.generate_image, "edit_image": engine.edit_image, "animate": engine.animate_image}
    queue.register(type_, lambda job, p: handlers[type_](store, backend, job, p))
    queue.start()
    try:
        job = queue.enqueue(type_, "gpu", params, project_id=project_id)
        return queue.wait_for(job["id"], 30)
    finally:
        queue.stop()


def _pixels(store, asset_id):
    with Image.open(store.data_dir / store.get_asset(asset_id)["file_path"]) as im:
        return design.pixel_hash(im.convert("RGB"))


def test_lineage_reuse_reproduces_byte_identical_and_vary_changes_seed(store, backend_with_comfy, project):
    params = {"prompt": "a red idol portrait", "positive_prompt": "a red idol portrait", "negative_prompt": "",
              "width": 512, "height": 512, "seed": 777, "count": 1, "template": "sdxl_txt2img"}
    done = _run_job(store, backend_with_comfy, "generate_image", params, project["id"])
    assert done["state"] == "done", done
    original = done["outputs"]["asset_ids"][0]
    lineage = engine.get_lineage(store, original)
    recipe = lineage["recipe"]
    assert recipe["params"]["seed"] == 777
    assert recipe["checkpoint"] == "sd_xl_base_1.0.safetensors"
    assert len(recipe["template_hash"]) == 16
    assert lineage["reproduce"]["args"]["operation"] == "reuse"

    again = _run_job(store, backend_with_comfy, "edit_image", {"asset_id": original, "operation": "reuse"}, project["id"])
    assert again["state"] == "done", again
    copy_id = again["outputs"]["asset_ids"][0]
    assert _pixels(store, copy_id) == _pixels(store, original)
    assert store.get_asset(copy_id)["recipe"]["derived_from"] == original

    varied = _run_job(store, backend_with_comfy, "edit_image", {"asset_id": original, "operation": "vary", "seed": 9}, project["id"])
    vid = varied["outputs"]["asset_ids"][0]
    assert store.get_asset(vid)["recipe"]["params"]["seed"] == 9
    assert _pixels(store, vid) != _pixels(store, original)


def test_img2img_records_reference_and_strength(store, backend_with_comfy, project):
    base = _run_job(store, backend_with_comfy, "generate_image",
                    {"positive_prompt": "idol", "width": 256, "height": 256, "seed": 1, "template": "sdxl_txt2img"}, project["id"])
    src = base["outputs"]["asset_ids"][0]
    edited = _run_job(store, backend_with_comfy, "edit_image",
                      {"asset_id": src, "operation": "img2img", "prompt": "idol in the rain", "strength": 0.3}, project["id"])
    assert edited["state"] == "done", edited
    recipe = store.get_asset(edited["outputs"]["asset_ids"][0])["recipe"]
    assert recipe["input_asset_ids"] == [src]
    assert recipe["params"]["denoise"] == 0.3
    assert isinstance(recipe["params"]["seed"], int)


def test_img2img_of_a_flux_image_does_not_inherit_flux_sampling(store, backend_with_comfy, project):
    base = _run_job(store, backend_with_comfy, "generate_image",
                    {"prompt": "idol on a rooftop", "positive_prompt": "idol on a rooftop", "width": 512, "height": 512,
                     "seed": 1, "template": "flux_schnell_txt2img"}, project["id"])
    assert base["state"] == "done", base
    src = base["outputs"]["asset_ids"][0]
    assert store.get_asset(src)["recipe"]["params"]["cfg"] == 1
    edited = _run_job(store, backend_with_comfy, "edit_image", {"asset_id": src, "operation": "img2img"}, project["id"])
    assert edited["state"] == "done", edited
    params = store.get_asset(edited["outputs"]["asset_ids"][0])["recipe"]["params"]
    assert params["cfg"] == 6.5 and params["steps"] == 30 and params["sampler"] == "dpmpp_2m"
    assert "flux" not in str(params["checkpoint"]).lower()
    assert params["positive_prompt"] == "idol on a rooftop"


def test_img2img_of_an_instruction_edit_uses_the_users_words(store, backend_with_comfy, project):
    base = _run_job(store, backend_with_comfy, "generate_image",
                    {"positive_prompt": "idol", "width": 256, "height": 256, "seed": 1, "template": "sdxl_txt2img"}, project["id"])
    ref = base["outputs"]["asset_ids"][0]
    edit = _run_job(store, backend_with_comfy, "generate_image",
                    {"prompt": "in the rain", "positive_prompt": "Keep the character in <image1> exactly the same, in the rain",
                     "seed": 2, "template": "qwen21_edit", "reference_asset_ids": [ref]}, project["id"])
    assert edit["state"] == "done", edit
    again = _run_job(store, backend_with_comfy, "edit_image",
                     {"asset_id": edit["outputs"]["asset_ids"][0], "operation": "img2img"}, project["id"])
    assert again["state"] == "done", again
    params = store.get_asset(again["outputs"]["asset_ids"][0])["recipe"]["params"]
    assert params["positive_prompt"] == "in the rain" and params["cfg"] == 6.5


def test_animate_produces_mp4_video_asset(store, backend_with_comfy, project):
    import shutil

    if not engine.ffmpeg_path():
        pytest.skip("ffmpeg missing")
    base = _run_job(store, backend_with_comfy, "generate_image",
                    {"positive_prompt": "idol", "width": 320, "height": 320, "seed": 3, "template": "sdxl_txt2img"}, project["id"])
    done = _run_job(store, backend_with_comfy, "animate", {"asset_id": base["outputs"]["asset_ids"][0], "frames": 8, "fps": 8}, project["id"])
    assert done["state"] == "done", done
    video = store.get_asset(done["outputs"]["asset_ids"][0])
    assert video["kind"] == "video" and video["mime"] == "video/mp4"
    assert video["file_path"].endswith(".mp4")
    assert (store.data_dir / video["file_path"]).is_file()
    assert video["duration_s"] and 0.5 <= video["duration_s"] <= 2.0
    assert shutil.which("ffmpeg") is None or video["thumb_path"]


# ------------------------------------------------------------ photocards

def test_photocard_set_produces_fronts_backs_and_sheet(store, project):
    chars = []
    for name in ("Iris", "Mika"):
        c = store.create_character(project["id"], name, prompt=f"idol {name}", bio="hello")
        ref = engine.render_design(store, project["id"], "thumbnail", {"title": name, "accent": "#ff4d8d"})
        chars.append(store.update_character(c["id"], canonical_asset_id=ref["id"]))
    group = store.create_group(project["id"], "Neon Static", member_ids=[c["id"] for c in chars])
    result = engine.photocard_set(store, project["id"], group["id"])
    assert len(result["front_ids"]) == 2 and len(result["back_ids"]) == 2
    back = store.get_asset(result["back_ids"][1])
    assert back["recipe"]["fields"]["serial"] == "No. 002/002"
    assert back["recipe"]["fields"]["monogram"] == "NS"
    sheet = store.get_asset(result["contact_sheet_id"])
    assert sheet["width"] and sheet["thumb_path"]


def test_photocard_set_without_any_images_raises_actionable_error(store, project):
    c = store.create_character(project["id"], "NoImage", prompt="idol")
    group = store.create_group(project["id"], "Test Group", member_ids=[c["id"]])
    with pytest.raises(engine.EngineError) as exc:
        engine.photocard_set(store, project["id"], group["id"])
    assert exc.value.code == "no_reference_images"


def test_group_members_must_belong_to_project(store, project):
    other = store.create_project("Other")
    c = store.create_character(other["id"], "Stranger", prompt="x")
    with pytest.raises(ValueError):
        store.create_group(project["id"], "G", member_ids=[c["id"]])


def test_render_design_creates_asset_with_recipe_and_print_bleed(store, project):
    asset = engine.render_design(store, project["id"], "thumbnail", {"title": "Hi", "accent": "#ff4d8d"})
    assert asset["recipe"]["template"] == "thumbnail"
    assert (asset["width"], asset["height"]) == (1280, 720)
    printed = engine.render_design(store, project["id"], "thumbnail", {"title": "Hi"}, print_mode=True)
    bleed = design.bleed_px()
    assert bleed == 35
    assert (printed["width"], printed["height"]) == (1280 + 2 * bleed, 720 + 2 * bleed)
    with Image.open(store.data_dir / printed["file_path"]) as im:
        assert round(im.info["dpi"][0]) == 300


def test_design_rejects_non_image_assets_in_image_fields(store, project, tmp_path):
    lyr = engine.create_lyrics(store, project["id"], "[00:01.00]x")
    with pytest.raises(engine.EngineError) as exc:
        engine.render_design(store, project["id"], "thumbnail", {"title": "x", "image": lyr["id"]})
    assert exc.value.code == "not_an_image"


def test_show_assets_uses_contact_sheet_above_four_and_stays_small(store, project):
    ids = [engine.render_design(store, project["id"], "lyric_card", {"quote": f"line {i}"})["id"] for i in range(6)]
    four = engine.show_assets(store, ids[:4], 768)
    assert len(four) == 4 and all(len(x["bytes"]) <= engine.SHOW_MAX_BYTES for x in four)
    sheet = engine.show_assets(store, ids, 768)
    assert len(sheet) == 1 and sheet[0]["kind"] == "contact_sheet" and sheet[0]["order"] == ids
    assert len(sheet[0]["bytes"]) <= engine.SHOW_MAX_BYTES


def test_inpaint_and_hires_run_on_the_backend(store, backend_with_comfy, project):
    from PIL import Image

    base = _run_job(store, backend_with_comfy, "generate_image",
                    {"positive_prompt": "idol portrait", "width": 512, "height": 512, "seed": 2, "template": "sdxl_txt2img"}, project["id"])
    src = base["outputs"]["asset_ids"][0]
    mask_path = store.data_dir / "inbox" / "mask.png"
    mask_path.parent.mkdir(parents=True, exist_ok=True)
    Image.new("RGB", (512, 512), (0, 0, 0)).save(mask_path)
    mask = engine.import_asset(store, project["id"], mask_path)
    inp = _run_job(store, backend_with_comfy, "edit_image",
                   {"asset_id": src, "operation": "inpaint", "mask_asset_id": mask["id"], "prompt": "gold earrings"}, project["id"])
    assert inp["state"] == "done", inp
    recipe = store.get_asset(inp["outputs"]["asset_ids"][0])["recipe"]
    assert recipe["input_asset_ids"] == [src, mask["id"]] and recipe["params"]["denoise"] == 1.0
    up = _run_job(store, backend_with_comfy, "edit_image", {"asset_id": src, "operation": "hires"}, project["id"])
    assert up["state"] == "done", up
    big = store.get_asset(up["outputs"]["asset_ids"][0])
    assert (big["width"], big["height"]) == (768, 768)
    assert big["recipe"]["derived_from"] == src and big["recipe"]["params"]["seed"] == 2
    design_asset = engine.render_design(store, project["id"], "thumbnail", {"title": "x"})
    refused = _run_job(store, backend_with_comfy, "edit_image", {"asset_id": design_asset["id"], "operation": "hires"}, project["id"])
    assert refused["state"] == "failed" and "img2img" in refused["message"]


# ------------------------------------------------------------- finishing

class _Roots:
    def __init__(self, *roots):
        self.roots = list(roots)

    def import_roots(self, lexical=False):
        return self.roots


def test_import_path_outside_the_roots_is_refused_before_any_stat(store, tmp_path, monkeypatch):
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    from pathlib import Path

    touched = []
    real_resolve = Path.resolve

    def spy(self, *a, **k):
        touched.append(str(self))
        return real_resolve(self, *a, **k)

    monkeypatch.setattr(Path, "resolve", spy)
    with pytest.raises(engine.EngineError) as exc:
        engine.resolve_import_path(_Roots(allowed), store, str(tmp_path / "elsewhere" / "x.png"))
    assert "outside" in str(exc.value) and touched == []
    with pytest.raises(engine.EngineError) as exc:
        engine.resolve_import_path(_Roots(allowed), store, "\\\\fileserver\\share\\x.png")
    assert "network" in str(exc.value) and touched == []
    with pytest.raises(engine.EngineError) as exc:
        engine.resolve_import_path(_Roots(allowed), store, "\\\\?\\C:\\x.png")
    assert "network" in str(exc.value)


def test_import_bakes_exif_orientation_and_cleans_up_on_failure(store, project, monkeypatch):
    src = store.data_dir / "inbox" / "phone.jpg"
    src.parent.mkdir(parents=True, exist_ok=True)
    img = Image.new("RGB", (40, 20), (200, 10, 10))
    exif = img.getexif()
    exif[0x0112] = 6  # "rotate 90 degrees clockwise to display"
    img.save(src, format="JPEG", exif=exif.tobytes())
    asset = engine.import_asset(store, project["id"], src)
    assert (asset["width"], asset["height"]) == (20, 40)
    with Image.open(store.data_dir / asset["file_path"]) as stored:
        assert stored.size == (20, 40)
        assert stored.getexif().get(0x0112, 1) == 1

    before = sorted(p.name for p in store.assets_dir.iterdir())
    thumbs_before = sorted(p.name for p in store.thumbs_dir.iterdir())

    def broken(**_kw):
        raise RuntimeError("database is locked")

    monkeypatch.setattr(store, "create_asset", broken)
    with pytest.raises(RuntimeError):
        engine.import_asset(store, project["id"], src)
    assert sorted(p.name for p in store.assets_dir.iterdir()) == before
    assert sorted(p.name for p in store.thumbs_dir.iterdir()) == thumbs_before


def _tone_wav(path, seconds, sr=22050):
    import wave

    import numpy as np

    t = np.arange(int(seconds * sr)) / sr
    data = (0.3 * np.sin(2 * np.pi * 220 * t) * 32767).astype("<i2").tobytes()
    path.parent.mkdir(parents=True, exist_ok=True)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(sr)
        w.writeframes(data)
    return path


def test_auto_cut_refuses_a_song_shorter_than_one_clip(store, project):
    if not engine.ffmpeg_path():
        pytest.skip("ffmpeg missing")
    song = engine.import_asset(store, project["id"], _tone_wav(store.data_dir / "inbox" / "blip.wav", 0.3))
    with pytest.raises(engine.EngineError, match="at least"):
        engine.auto_cut(store, project["id"], song["id"], None, None, None, None)


def test_analysis_cap_does_not_shorten_the_songs_duration(store, project, monkeypatch):
    if not engine.ffmpeg_path():
        pytest.skip("ffmpeg missing")
    song = engine.import_asset(store, project["id"], _tone_wav(store.data_dir / "inbox" / "long.wav", 4.0))
    monkeypatch.setattr(engine, "ANALYSIS_MAX_S", 2.0)
    result = engine.analyze_audio(store, song["id"], force=True)
    assert result["duration_s"] == pytest.approx(2.0, abs=0.1)
    assert store.get_asset(song["id"])["duration_s"] == pytest.approx(4.0, abs=0.1)


def test_character_with_a_studio_voice_speaks_through_the_voice_library(store, project, monkeypatch):
    import numpy as np

    from prosperos_hoard import voice_engines as ve

    class FakeTTS(ve.TTSEngine):
        id = "fake-basic"
        capabilities = ve.EngineCapabilities(cloning=False)
        calls = []

        def is_installed(self):
            return True

        def synthesize(self, text, voice_ref=None, speed=None, pitch=None, style=None, sample_path=None, language=None):
            FakeTTS.calls.append((text, speed))
            return ve.wav_bytes_mono16(np.zeros(8000, dtype=np.float32), 8000)

    monkeypatch.setattr(engine, "_studio_tts_engines", lambda *_a: [FakeTTS()])
    voice = store.create_studio_voice("Narrator", "fake-basic", language="en")
    char = store.create_character(project["id"], "Nova", prompt="x",
                                  voice={"backend": "studio", "voice_id": voice["id"], "speed": 1.2})
    asset = engine.voice_line(store, None, project["id"], "hello there", character_id=char["id"])
    assert asset["recipe"]["provider"] == "studio:fake-basic"
    assert FakeTTS.calls == [("hello there", 1.2)]
    # a library voice id given as an override also goes through the studio
    other = engine.voice_line(store, None, project["id"], "again", voice_override=voice["id"])
    assert other["recipe"]["provider"] == "studio:fake-basic"


def test_update_timeline_finishing_persists_and_validates(store, project):
    img = engine.render_design(store, project["id"], "thumbnail", {"title": "cover"})
    tl = store.create_timeline(project["id"], "Test cut", tracks=[
        {"type": "visual", "clips": [{"asset_id": img["id"], "kind": "image", "start_s": 0.0, "duration_s": 1.0,
                                       "trim_start_s": 0.0, "ken_burns": {"zoom_start": 1.0, "zoom_end": 1.0, "pan": "none"},
                                       "transition_in": {"type": "cut", "duration_s": 0.0}}]},
    ])
    assert tl["finishing"] == {}  # new timelines render exactly as before this feature

    updated = engine.update_timeline(store, tl["id"], {
        "finishing": {"color_grade": "sodium_night", "grain": 0.3, "vignette": True, "letterbox": True,
                      "glitch_on_downbeats": True, "lyric_style": "horror"},
    })
    assert updated["finishing"]["color_grade"] == "sodium_night"
    assert updated["finishing"]["lyric_style"] == "horror"
    # persisted, not just returned in-memory
    assert store.get_timeline(tl["id"])["finishing"] == updated["finishing"]

    with pytest.raises(engine.EngineError) as exc:
        engine.update_timeline(store, tl["id"], {"finishing": {"color_grade": "not-a-real-preset"}})
    assert exc.value.code == "bad_finishing"

    # clearing it back to {} is a valid patch too
    cleared = engine.update_timeline(store, tl["id"], {"finishing": {}})
    assert cleared["finishing"] == {}
