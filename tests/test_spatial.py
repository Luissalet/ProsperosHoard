"""Native source retention, coordinate exchange and generated-recipe provenance."""
from copy import deepcopy
import io
import json

from PIL import Image
import pytest

from prosperos_hoard import spatial


def document():
    return {"format": spatial.FORMAT, "width": 1024, "height": 768, "caption": "Una comparación en castellano",
            "items": [{"id": "left", "kind": "object", "caption": "Un cuenco rojo", "box": [64, 256, 320, 320]},
                      {"id": "title", "kind": "text", "caption": "Título azul", "text": "¿Qué cambió? 25 €", "box": [128, 32, 768, 128]}]}


def test_source_edits_are_immutable_and_keep_other_objects_and_original_bytes(store, project):
    original = document()
    first = spatial.save(store, project["id"], original, "Comparación")
    path = store.data_dir / store.get_asset(first["id"])["file_path"]
    original_bytes = path.read_bytes()
    edited = deepcopy(original)
    edited["items"][0]["box"][0] = 96
    second = spatial.save(store, project["id"], edited, "Comparación", first["id"])
    assert first["id"] != second["id"] and second["parent_id"] == first["id"]
    assert spatial.load(store, first["id"])["document"] == original and path.read_bytes() == original_bytes
    assert second["document"]["items"][1] == original["items"][1]
    assert second["document"]["items"][0]["id"] == "left"
    other = store.create_project("Otro")
    with pytest.raises(ValueError, match="another project"):
        spatial.save(store, other["id"], edited, parent_id=first["id"])


def test_rectangular_canvas_exchange_has_exact_centered_yxyx_boxes_and_spanish_text():
    result = spatial.compile_prompt(document(), "moss")
    layout = json.loads(result["prompt"])
    assert layout["bbox_format"] == "yxyx" and layout["bbox_coord_space"] == "abs_centralized"
    assert layout["items"][0]["bbox"] == [-128, -448, 192, -128]
    assert layout["items"][1]["bbox"] == [-352, -384, -224, 384]
    assert layout["items"][1]["text"] == "¿Qué cambió? 25 €"
    assert result["reference_asset_ids"] == []
    with pytest.raises(ValueError, match="target"):
        spatial.compile_prompt(document(), "unknown")
    coloured = document()
    coloured["items"][1]["colour"] = "#174ea6"
    assert "#174ea6" in spatial.compile_prompt(coloured)["prompt"]
    assert "#174ea6" in spatial.compile_prompt(coloured, "moss")["layout"]["items"][1]["caption"]


@pytest.mark.parametrize("edit", [lambda d: d["items"][0].update(box=[float("nan"), 0, 20, 20]),
                                  lambda d: d["items"][0].update(box=[1000, 0, 40, 20]),
                                  lambda d: d["items"][1].update(id="left")])
def test_invalid_geometry_or_identity_does_not_create_a_source(store, project, edit):
    broken = document()
    edit(broken)
    with pytest.raises(ValueError):
        spatial.save(store, project["id"], broken)
    assert not store.list_assets(project["id"], kind="layout")["items"]


def test_reference_order_and_real_reference_pixels_survive_preview(store, project):
    source = store.path_for_asset_file("synthetic-reference", ".png")
    Image.new("RGB", (20, 20), "red").save(source)
    ref = store.create_asset(project_id=project["id"], kind="image", file_path=source.relative_to(store.data_dir).as_posix())
    doc = document()
    doc["items"][0].update(reference_asset_id=ref["id"], caption="")
    doc["items"][1]["reference_asset_id"] = ref["id"]
    caption = spatial.compile_prompt(doc)
    assert caption["reference_asset_ids"] == [ref["id"]] and caption["prompt"].count("<image1>") == 2
    moss = spatial.compile_prompt(doc, "moss")
    assert moss["reference_asset_ids"] == [] and "does not encode reference" in moss["notes"][0]
    with Image.open(io.BytesIO(spatial.preview(store, doc))) as image:
        assert image.size == (1024, 768)
        assert image.getpixel((200, 400))[:3] == (255, 0, 0)
    source.unlink()
    with pytest.raises(ValueError, match="retained image"):
        spatial.save(store, project["id"], doc)


def test_api_source_compile_preview_and_generated_recipe_keep_exact_source(client):
    c, app, _ = client
    project = app.state.store.create_project("Spatial native API")
    saved = c.post("/api/spatial", json={"action": "save", "project": project["id"], "document": document(), "name": "Dos objetos"})
    assert saved.status_code == 200, saved.text
    aid = saved.json()["id"]
    read = c.post("/api/agent/studio_spatial", json={"action": "get", "asset_id": aid})
    assert read.status_code == 200 and read.json()["document"] == document()
    preview = c.post("/api/spatial", json={"action": "preview", "asset_id": aid})
    assert preview.status_code == 200, preview.text
    assert app.state.store.get_asset(preview.json()["asset"]["id"])["recipe"]["derived_from"] == aid
    # Local fake Comfy verifies real submission, file retention and recipe wiring,
    # not image-model quality or adherence.
    render = c.post(f"/api/projects/{project['id']}/generate", json={"prompt": "Luz suave", "composition_asset_id": aid,
                    "engine": "sdxl", "seed": 42, "wait_s": 10})
    assert render.status_code == 200, render.text
    result = render.json()
    job = app.state.store.get_job(result["job"]["id"])
    assert job["state"] == "done", job
    assert job["params"]["composition_asset_id"] == aid
    assert job["params"]["width"] == 1024 and job["params"]["height"] == 768
    assert "¿Qué cambió? 25 €" in result["final_prompt"] and "left=64" in result["final_prompt"]
    asset = app.state.store.get_asset(job["outputs"]["asset_ids"][0])
    assert asset["recipe"]["composition_asset_id"] == aid
    assert spatial.load(app.state.store, aid)["document"] == document()


def test_source_listing_exposes_native_pagination_without_losing_older_versions(store, project):
    ids = {spatial.save(store, project["id"], document())["id"] for _ in range(61)}
    first = spatial.dispatch(store, "list", project=project["id"])
    assert first["has_more"] and first["next_offset"] == 60
    second = spatial.dispatch(store, "list", project=project["id"], offset=first["next_offset"])
    assert not second["has_more"]
    assert {row["id"] for row in first["items"] + second["items"]} == ids
