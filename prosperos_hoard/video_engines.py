"""Video choices grounded in the exact workflow's live nodes and model files."""
import json
from . import comfy_driver, engine

CHOICES = (
    ("wan14b", "Wan 2.2 14B", "wan22_i2v_14b", "image"),
    ("wan", "Wan 2.2 5B", "wan22_ti2v", "image"),
    ("ltx23", "LTX 2.3 distilled", "ltx23_i2v", "image"),
    ("svd", "SVD XT", "svd_img2vid", "image"),
    ("animate", "Wan Animate 2", "wan_animate2", "motion"),
    ("ltx23_control", "LTX 2.3 · Structure control", "ltx23_control", "motion"),
    ("flf", "Wan 2.2 · First / last frame", "wan22_flf2v", "end"),
    ("s2v", "Wan 2.2 S2V", "wan22_s2v", "audio"),
    ("infinitetalk", "InfiniteTalk", "wan21_infinitetalk", "audio"),
)
MODEL_FIELDS = {"ckpt_name", "unet_name", "clip_name", "vae_name", "lora_name", "audio_encoder_name", "text_encoder"}


def catalog(backend, object_info=None):
    live, problem = True, None
    try:
        info = object_info if object_info is not None else engine._object_info(backend, autostart=False)
    except Exception as exc:
        live, problem = False, str(exc)
        try:
            info = json.loads(engine.object_info_cache_path(backend.data_dir).read_text(encoding="utf-8"))
        except Exception:
            info = {}
    entries = []
    for key, label, template, mode in CHOICES:
        workflow, spec = comfy_driver.load_template(template, backend.data_dir)
        missing = []
        models = []
        for node in workflow.values():
            cls = node["class_type"]
            if cls not in info:
                missing.append(cls)
                continue
            for field, value in node.get("inputs", {}).items():
                if not isinstance(value, str) or (field not in MODEL_FIELDS and not (cls == "ModelPatchLoader" and field == "name")):
                    continue
                choices = comfy_driver.combo_choices(info, cls, field)
                # Motion's fp8 conversion is selected by the renderer itself.
                if template == "wan_animate2" and field == "unet_name":
                    value = engine.animate2_unet(info) or value
                models.append(value)
                if value not in choices:
                    missing.append(value)
        entries.append(dict(id=key, label=label, template=template, mode=mode,
                            installed=bool(info) and not missing, available=live and bool(info) and not missing,
                            missing=sorted(set(missing)), models=sorted(set(models)),
                            vram_mb=comfy_driver.estimate_vram_mb(spec, backend.vram_estimates_mb())))
    automatic = next((x["id"] for x in entries if x["id"] in ("wan14b", "wan") and x["available"]), None)
    return dict(live=live, problem=problem, auto_resolves_to=automatic, engines=entries)
