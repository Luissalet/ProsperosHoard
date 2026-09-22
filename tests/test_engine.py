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
