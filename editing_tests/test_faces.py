"""Face detection for reframing: which detector is chosen, what happens without network or without OpenCV, and a real detection
with the downloaded YuNet model (skipped when the model cannot be fetched here)."""

import subprocess

import numpy as np
import pytest

from conftest import make_services, needs_ffmpeg
from prosperos_hoard.editing import analyze, commands
from prosperos_hoard.editing import media as media_store
from prosperos_hoard.editing import projects as store
from prosperos_hoard.editing.analysis import faces

cv2 = pytest.importorskip("cv2")


@pytest.fixture(autouse=True)
def forget_failures():
    faces.reset()
    yield
    faces.reset()


def drawn_face(width=480, height=360, cx=300):
    """A cartoon face (skin-coloured oval, hair, eyes, brows, nose, mouth) centred at x=cx on a green wall; RGB."""
    img = np.full((height, width, 3), (90, 120, 90), np.uint8)
    c = (cx, height // 2)
    cv2.ellipse(img, c, (70, 90), 0, 0, 360, (235, 200, 170), -1)
    cv2.ellipse(img, (c[0], c[1] - 60), (75, 45), 0, 180, 360, (60, 40, 30), -1)
    for dx in (-28, 28):
        cv2.ellipse(img, (c[0] + dx, c[1] - 15), (14, 8), 0, 0, 360, (255, 255, 255), -1)
        cv2.circle(img, (c[0] + dx, c[1] - 15), 6, (20, 30, 40), -1)
        cv2.line(img, (c[0] + dx - 16, c[1] - 32), (c[0] + dx + 16, c[1] - 34), (40, 30, 30), 3)
    cv2.line(img, (c[0], c[1] - 10), (c[0] - 5, c[1] + 20), (190, 150, 120), 3)
    cv2.line(img, (c[0] - 5, c[1] + 20), (c[0] + 6, c[1] + 20), (190, 150, 120), 3)
    cv2.ellipse(img, (c[0], c[1] + 45), (26, 12), 0, 0, 180, (180, 60, 60), -1)
    return img


@pytest.fixture(scope="session")
def yunet_model(tmp_path_factory):
    """The real model, downloaded once for the session into a temp dir; the tests that need it skip when there is no network."""
    folder = tmp_path_factory.mktemp("models")
    path, why = faces.ensure_model(folder)
    if path is None:
        pytest.skip(f"the YuNet model cannot be downloaded here ({why})")
    return folder


# ---------------------------------------------------------------- selection logic (no network)

def test_without_network_the_haar_cascades_take_over(tmp_path):
    calls = []

    def offline(url, dest):
        calls.append(url)
        raise OSError("no route")

    detector, info = faces.pick(tmp_path / "models", fetch=offline)
    assert detector is not None and detector.name == "haar"
    assert info["detector"] == "haar" and info["fallback_from"] == "yunet" and "could not download" in info["reason"]
    assert len(calls) == len(faces.MODEL_URLS)  # every address tried once
    # a failed download is remembered: the next media does not wait for the network again
    detector, info = faces.pick(tmp_path / "models", fetch=offline)
    assert info["detector"] == "haar" and len(calls) == len(faces.MODEL_URLS) and "recently" in info["reason"]
    assert not list((tmp_path / "models").glob("*.part"))


def test_a_download_that_fails_the_hash_check_is_discarded(tmp_path):
    def corrupt(url, dest):
        dest.write_bytes(b"not an onnx file")

    path, why = faces.ensure_model(tmp_path / "models", fetch=corrupt)
    assert path is None and "size/hash" in why
    assert not faces.model_path(tmp_path / "models").exists() and not list((tmp_path / "models").glob("*.part"))


def test_a_verified_model_is_cached_and_chosen(tmp_path, monkeypatch):
    payload = b"pretend this is the 230 KB model"
    monkeypatch.setattr(faces, "MODEL_SIZE", len(payload))
    monkeypatch.setattr(faces, "MODEL_SHA256", __import__("hashlib").sha256(payload).hexdigest())
    fetched = []

    def serve(url, dest):
        fetched.append(url)
        dest.write_bytes(payload)

    detector, info = faces.pick(tmp_path / "models", fetch=serve)
    assert detector is not None and detector.name == "yunet" and info == {"requested": "auto", "detector": "yunet"}
    assert len(fetched) == 1 and faces.model_path(tmp_path / "models").read_bytes() == payload
    faces.pick(tmp_path / "models", fetch=serve)
    assert len(fetched) == 1  # cached: no second download
    assert faces.status(tmp_path / "models")["active"] == "yunet"
    # a damaged cache file is fetched again instead of trusted
    faces.model_path(tmp_path / "models").write_bytes(b"x")
    detector, _ = faces.pick(tmp_path / "models", fetch=serve)
    assert detector.name == "yunet" and len(fetched) == 2


def test_preferences_and_missing_opencv(tmp_path, monkeypatch):
    never = lambda url, dest: pytest.fail("must not download")  # noqa: E731
    detector, info = faces.pick(tmp_path, prefer="saliency", fetch=never)
    assert detector is None and info["detector"] == "saliency" and "settings" in info["reason"]
    detector, info = faces.pick(tmp_path, prefer="haar", fetch=never)
    assert detector.name == "haar" and "fallback_from" not in info
    monkeypatch.setattr(faces, "_cv2", lambda: None)
    detector, info = faces.pick(tmp_path, fetch=never)
    assert detector is None and info["detector"] == "saliency" and "OpenCV is not installed" in info["reason"]
    st = faces.status(tmp_path)
    assert st["opencv"] is False and st["active"] == "saliency" and st["yunet"]["supported"] is False


def test_status_says_what_is_downloaded(tmp_path):
    st = faces.status(tmp_path / "models")
    assert st["opencv"] and st["haar"] and st["yunet"]["downloaded"] is False and st["active"] == "haar" and "downloaded on the first" in st["note"]


# ---------------------------------------------------------------- real detection

def test_yunet_finds_the_drawn_face(yunet_model):
    detector, info = faces.pick(yunet_model, fetch=lambda u, d: pytest.fail("cached"))
    assert info["detector"] == "yunet"
    found = detector.detect(drawn_face(cx=300))
    assert len(found) == 1
    x, y, w, h, score = found[0]
    assert abs((x + w / 2) - 300) < 25 and 90 < h < 260 and score > 0.6
    assert detector.detect(np.full((360, 480, 3), 120, np.uint8)) == []


@needs_ffmpeg
def test_focus_analysis_reports_and_follows_the_detector(tmp_path, yunet_model):
    img = tmp_path / "face.png"
    cv2.imwrite(str(img), cv2.cvtColor(drawn_face(cx=330), cv2.COLOR_RGB2BGR))
    video = tmp_path / "face.mp4"
    subprocess.run(["ffmpeg", "-hide_banner", "-loglevel", "error", "-y", "-loop", "1", "-i", str(img), "-t", "3", "-r", "10", "-c:v", "libx264",
                    "-pix_fmt", "yuv420p", str(video)], check=True)
    svc = make_services(tmp_path / "app")
    svc.config.models_dir.mkdir(parents=True, exist_ok=True)
    for f in yunet_model.iterdir():
        (svc.config.models_dir / f.name).write_bytes(f.read_bytes())  # the cached model, as after a first download
    svc.start()
    try:
        mid = media_store.import_path(svc, str(video))["id"]
        analyze.schedule(svc, mid, ["focus"], force=True)
        res = media_store.get_analysis(svc, mid, "focus")
        assert res["detector"] == "yunet" and res["faces"] and res["face_samples"] >= 8
        xs = [s[1] for s in res["samples"] if s[4] == 1]
        assert abs(float(np.median(xs)) - 330 / 480) < 0.05  # the face sits right of centre and the samples say so
        assert analyze.summarize("focus", res)["detector"] == "yunet"
        assert svc.status()["faces"]["active"] == "yunet"
        pid = store.create(svc, "Cara", preset="hd720", media=[mid])["id"]
        out = commands.run(svc, pid, "reframe", {"aspect": "9:16"})["summary"]
        assert out["face_detector"] == "yunet" and out["detector"] == "yunet+saliency"
        clip = store.doc(svc, pid).main_track().clips[0]
        assert clip.reframe and clip.reframe.path[0][1] > 0.55  # the vertical window sits on the face, right of centre
    finally:
        svc.stop()


@needs_ffmpeg
def test_analysis_without_network_falls_back_and_says_so(tmp_path, media_dir):
    svc = make_services(tmp_path)  # offline: the fake fetcher refuses every download
    svc.start()
    try:
        mid = media_store.import_path(svc, str(media_dir / "talk.mp4"))["id"]
        analyze.schedule(svc, mid, ["focus"])
        res = media_store.get_analysis(svc, mid, "focus")
        assert res["detector"] == "haar" and res["detector_info"]["fallback_from"] == "yunet" and res["detector_info"]["reason"]
        pid = store.create(svc, "x", preset="hd720", media=[mid])["id"]
        media_store.put_analysis(svc, mid, "scenes", {"cuts": []})
        out = commands.run(svc, pid, "reframe", {"aspect": "1:1"})["summary"]
        assert out["face_detector"] == "haar" and "could not download" in out["detector_note"]
        svc.update_settings({"face_detector": "saliency"})
        analyze.schedule(svc, mid, ["focus"], force=True)
        res = media_store.get_analysis(svc, mid, "focus")
        assert res["detector"] == "saliency" and res["faces"] is False and res["face_samples"] == 0
        with pytest.raises(Exception, match="face_detector must be one of"):
            svc.update_settings({"face_detector": "magic"})
    finally:
        svc.stop()
