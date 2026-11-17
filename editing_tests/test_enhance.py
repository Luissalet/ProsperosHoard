"""GPU video enhancement of saved clips: the filter chain, derived media, clips pointed at the enhanced copy and the tools.

The tests force the CPU path (no Vulkan, no NVENC), so no test ever touches a GPU."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from conftest import make_services, needs_ffmpeg
from prosperos_hoard.editing import enhance
from prosperos_hoard.editing import media as media_store
from prosperos_hoard.editing import projects as store
from prosperos_hoard.editing.errors import LumiereError

CPU = {"gpu": 2, "vulkan": False, "nvenc": False, "nvenc_hevc": False,
       "filters": {"libplacebo": False, "nlmeans_vulkan": False, "hqdn3d": True, "framerate": True, "minterpolate": True, "unsharp": True}}
GPU = {**CPU, "vulkan": True, "nvenc": True, "nvenc_hevc": True, "filters": {**CPU["filters"], "libplacebo": True, "nlmeans_vulkan": True}}


@pytest.fixture(autouse=True)
def cpu_only(monkeypatch):
    """Every capability probe answers from here; a test that wants the GPU chain builds it with ``GPU`` without running it."""
    monkeypatch.setattr(enhance, "capabilities", lambda svc, gpu, refresh=False: dict(CPU, gpu=gpu))


def chain(raw, caps=GPU, width=1280, height=720, fps=30.0):
    return enhance.build_chain(enhance.normalize(raw, 2), width, height, fps, caps)


# ---------------------------------------------------------------- pure logic

def test_normalize_checks_values_and_defaults_to_the_spare_gpu(monkeypatch):
    assert enhance.effects_on(enhance.normalize({})) == [] and enhance.normalize({})["gpu"] == 2
    monkeypatch.setenv("LUMIERE_ENHANCE_GPU", "3")
    assert enhance.normalize({})["gpu"] == 3 and enhance.normalize({}, 4)["gpu"] == 4
    for bad in ({"denoise": "extreme"}, {"upscale": 4}, {"interpolate": "magic"}, {"nope": 1}, {"sharpen": "high"}):
        with pytest.raises(LumiereError):
            enhance.normalize(bad)
    fx = enhance.normalize({"denoise": "medium", "upscale": 2, "sharpen": 0.456, "interpolate": "blend", "hdr": True}, 2)
    assert fx["sharpen"] == 0.46 and enhance.effects_on(fx) == ["denoise", "upscale", "sharpen", "interpolate", "hdr"]
    assert enhance.effects_on(enhance.only(fx, "sharpen")) == ["sharpen"]


def test_gpu_chain_orders_noise_frames_then_upscale_and_uses_the_chosen_gpu():
    c = chain({"denoise": "medium", "upscale": 2, "sharpen": 0.5, "interpolate": "mci", "interpolate_fps": 60})
    assert c.vf.index("nlmeans_vulkan") < c.vf.index("minterpolate") < c.vf.index("libplacebo")
    assert (c.out_w, c.out_h, c.out_fps) == (2560, 1440, 60.0) and c.gpu_filters
    assert c.hw_args == ["-init_hw_device", "vulkan=vk:2", "-filter_hw_device", "vk"]
    assert enhance.build_chain(enhance.normalize({"upscale": 2}, 3), 1280, 720, 30, GPU).hw_args[1] == "vulkan=vk:3"


def test_hdr_chain_and_encoder():
    c = chain({"hdr": True, "upscale": 1.5})
    assert c.hdr and c.pix_fmt == "p010le" and "color_trc=smpte2084" in c.vf and (c.out_w, c.out_h) == (1920, 1080)
    args, name = enhance.encoder_args(GPU, 2, c.out_w, c.out_h, c.out_fps, hdr=True)
    assert name == "hevc_nvenc" and args[args.index("-gpu") + 1] == "2" and "main10" in args
    assert enhance.encoder_args(CPU, 2, 1280, 720, 30, hdr=True)[1] == "libx265"
    assert enhance.encoder_args(GPU, 2, 1280, 720, 30, hdr=False)[1] == "h264_nvenc"
    assert enhance.encoder_args(CPU, 2, 1280, 720, 30, hdr=False)[1] == "libx264"


def test_cpu_fallback_and_limits():
    c = chain({"denoise": "medium", "upscale": 2, "sharpen": 0.5, "hdr": True}, CPU)
    assert not c.gpu_filters and "hqdn3d" in c.vf and "scale=2560:1440" in c.vf and "unsharp" in c.vf and "hdr" in c.skipped
    assert chain({"upscale": 3}, GPU, width=2560, height=1440).out_w == 3840
    assert "upscale" in chain({"upscale": 2}, GPU, width=3840, height=2160).skipped
    assert "interpolate" in chain({"interpolate": "blend", "interpolate_fps": 30}, GPU).skipped


# ---------------------------------------------------------------- jobs and tools

@pytest.fixture
def talk(services, media_dir):
    needs = media_dir / "talk.mp4"
    return media_store.import_path(services, str(needs))["id"]


def probe(path: Path) -> dict:
    out = subprocess.run(["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=width,height,r_frame_rate,codec_name",
                          "-of", "json", str(path)], capture_output=True, text=True, check=True).stdout
    return json.loads(out)["streams"][0]


@needs_ffmpeg
def test_media_job_derives_a_bigger_copy_and_keeps_the_original(services, talk):
    from prosperos_hoard.editing import agent_tools

    start = media_store.get(services, talk)
    res = agent_tools.run_media_enhance(services, agent_tools.EnhanceMediaArgs(media=talk, upscale=1.5, denoise="light"))
    job = services.jobs.get(res["job"])
    assert job["state"] == "done", job
    out = json.loads(job["result"]) if isinstance(job["result"], str) else job["result"]
    assert out["applied"] == ["denoise", "upscale"] and out["encoder"] == "libx264"
    derived = media_store.get(services, out["media"])
    assert derived["name"].endswith("(mejorado)") and derived["origin"] == "derived" and (derived["width"], derived["height"]) == (1920, 1080)
    assert Path(start["path"]).is_file() and media_store.get(services, talk)["width"] == 1280


@needs_ffmpeg
def test_clip_job_points_the_clip_at_the_enhanced_span(services, talk):
    from prosperos_hoard.editing import agent_tools

    pid = store.create(services, "Charla", preset="hd720", media=[talk])["id"]
    clip = store.doc(services, pid).tracks[0].clips[0]
    res = agent_tools.run_clip_enhance(services, agent_tools.EnhanceClipArgs(project=pid, clip=clip.id, interpolate="blend", interpolate_fps=60))
    assert services.jobs.get(res["job"])["state"] == "done"
    after = store.doc(services, pid).find(clip.id)[1]
    assert after.media != talk and round(media_store.get(services, after.media)["fps"]) == 60
    assert after.src_out - after.src_in == clip.src_out - clip.src_in          # the clip keeps its length on the timeline


def test_a_call_with_no_effect_or_a_wrong_value_is_a_direct_error(services, talk):
    from prosperos_hoard.editing import agent_tools

    with pytest.raises(LumiereError):
        agent_tools.run_media_enhance(services, agent_tools.EnhanceMediaArgs(media=talk))
    with pytest.raises(LumiereError):
        agent_tools.run_media_enhance(services, agent_tools.EnhanceMediaArgs(media=talk, upscale=2.5))
    with pytest.raises(Exception):
        agent_tools.run_media_enhance(services, agent_tools.EnhanceMediaArgs(media="med_nope", upscale=2))


def test_info_and_the_tools_are_in_the_catalogue(services):
    from prosperos_hoard.editing import agent_tools

    info = enhance.info(services)
    assert info["gpu"] == 2 and info["capabilities"]["vulkan"] is False and info["effects"]["upscale"][-1] == 3.0
    names = {t.name for t in agent_tools.TOOLS}
    assert {"enhance_info", "media_enhance", "clip_enhance", "enhance_benchmark"} <= names
    from prosperos_hoard.editing import agent_undo
    assert {"media_enhance", "clip_enhance", "enhance_benchmark"} <= agent_undo.DRAFT_SAFE


@needs_ffmpeg
def test_benchmark_job_reports_the_baseline_and_each_effect(services, media_dir):
    from prosperos_hoard.editing import agent_tools

    res = agent_tools.run_enhance_benchmark(services, agent_tools.EnhanceBenchArgs(denoise="light", interpolate="blend", frames=60))
    job = services.jobs.get(res["job"])
    assert job["state"] == "done", job
    out = json.loads(job["result"]) if isinstance(job["result"], str) else job["result"]
    assert [r["effect"] for r in out["effects"]] == ["denoise", "interpolate"] and out["baseline"]["effect"] == "baseline"
    for row in [out["baseline"], *out["effects"], out["all"]]:
        assert row["ms_per_frame"] is None or row["ms_per_frame"] > 0
    assert out["frame_budget_ms"] == pytest.approx(33.33, abs=0.01)


@needs_ffmpeg
def test_rest_routes_and_agent_call_with_a_reason(client, media_dir):
    from test_agent_accountability import agent_call, ok

    mid = ok(agent_call(client, "media_import", {"path": str(media_dir / "talk.mp4")}))["id"]
    assert client.get("/api/media/enhance/info").json()["gpu"] == 2
    assert client.post(f"/api/media/{mid}/enhance", json={}).status_code == 400            # no effect named
    job = client.post(f"/api/media/{mid}/enhance", json={"upscale": 1.5}).json()
    assert job["job"].startswith("job_")
    assert agent_call(client, "media_enhance", {"media": mid, "upscale": 2}, reason=None).status_code == 400   # a write needs its reason
    assert agent_call(client, "media_enhance", {"media": mid, "upscale": 2}).status_code == 200
