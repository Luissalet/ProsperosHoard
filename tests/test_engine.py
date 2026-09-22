from prosperos_hoard import comfy_driver, design, engine
from prosperos_hoard.devtools.fake_comfy import render_fake_image


def test_expand_mentions_pulls_prompt_negative_and_reference(store, project):
    char = store.create_character(
        project["id"], "Aria", prompt="an idol with silver hair", negative="extra fingers",
        canonical_asset_id="a_ref123",
    )
    result = engine.expand_mentions(store, project["id"], "@Aria singing under neon lights")
    assert "an idol with silver hair" in result["expanded_prompt"]
    assert "singing under neon lights" in result["expanded_prompt"]
    assert result["negative_extra"] == "extra fingers"
    assert result["reference_asset_id"] == "a_ref123"
    assert result["matched_characters"] == ["Aria"]


def test_expand_mentions_unknown_name_passes_through(store, project):
    result = engine.expand_mentions(store, project["id"], "hello @Nobody here")
    assert "@Nobody" in result["expanded_prompt"]
    assert result["matched_characters"] == []


def test_compose_prompt_applies_style_defaults(store, project):
    presets = store.list_style_presets()
    preset = next(p for p in presets if p["name"] == "Neon night city")
    composed = engine.compose_prompt(store, project["id"], "a lone figure", None, preset["id"])
    assert composed["positive_prompt"].startswith("neon-lit night city")
    assert composed["style_defaults"]["sampler"] == "dpmpp_2m"


def test_lineage_reproduces_byte_identical_fake_image(store, backend_with_comfy, project):
    job = {"id": "job1", "project_id": project["id"], "params": {
        "positive_prompt": "a red circle", "negative_prompt": "", "width": 512, "height": 512,
        "seed": 777, "steps": 20, "cfg": 6.0, "sampler": "euler_a", "scheduler": "normal", "count": 1,
        "template": "sdxl_txt2img", "checkpoint": "sd_xl_base_1.0.safetensors",
    }}
    result = engine.generate_image(store, backend_with_comfy, job, lambda *a, **k: None)
    asset = result["assets"][0]
    lineage = engine.get_lineage(store, asset["id"])
    recipe = lineage["recipe"]

    # Reproduce independently via the pure renderer using the recorded recipe.
    expected = render_fake_image(recipe["params"]["seed"], recipe["params"]["positive_prompt"],
                                  recipe["params"]["width"], recipe["params"]["height"])
    actual_path = store.data_dir / asset["file_path"]
    from PIL import Image

    with Image.open(actual_path) as actual:
        assert design.pixel_hash(actual.convert("RGB")) == design.pixel_hash(expected.convert("RGB"))


def test_photocard_set_produces_two_assets_per_member_plus_sheet(store, project):
    chars = []
    for name in ("Iris", "Mika"):
        c = store.create_character(project["id"], name, prompt=f"idol {name}")
        # Give each member a canonical reference image (rendered, not generated,
        # to keep this test independent of a ComfyUI backend).
        ref = engine.render_design(store, project["id"], "thumbnail", {"title": name, "accent": "#ff4d8d"})
        c = store.update_character(c["id"], canonical_asset_id=ref["id"])
        chars.append(c)
    group = store.create_group(project["id"], "Test Group", member_ids=[c["id"] for c in chars])
    result = engine.photocard_set(store, project["id"], group["id"])
    assert len(result["assets"]) == 2 * len(chars)
    assert result["contact_sheet"]["kind"] == "image"


def test_photocard_set_without_any_images_raises_actionable_error(store, project):
    c = store.create_character(project["id"], "NoImage", prompt="idol")
    group = store.create_group(project["id"], "Test Group", member_ids=[c["id"]])
    try:
        engine.photocard_set(store, project["id"], group["id"])
        assert False, "expected EngineError"
    except engine.EngineError as exc:
        assert exc.code == "no_reference_images"


def test_render_design_creates_asset_with_recipe(store, project):
    asset = engine.render_design(store, project["id"], "thumbnail", {"title": "Hi", "accent": "#ff4d8d"})
    assert asset["kind"] == "image"
    assert asset["recipe"]["template"] == "thumbnail"
    assert (store.data_dir / asset["file_path"]).is_file()
