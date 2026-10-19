"""Face detection for reframing, best available first: YuNet (OpenCV's FaceDetectorYN with a ~230 KB ONNX model from the
OpenCV model zoo, downloaded once into the data dir and checked by size and hash), then OpenCV's classic Haar cascades,
then no detector at all (the focus track falls back to motion and detail). Every step is optional: the app runs without
OpenCV, and without network it simply uses the next detector. Which one ran is reported in the analysis and the status."""

from __future__ import annotations

import logging
import threading
import time
import urllib.request
from pathlib import Path
from typing import Any, Callable, Optional

import numpy as np

from ..util import sha256_file

log = logging.getLogger("lumiere.faces")

MODEL_FILE = "face_detection_yunet_2023mar.onnx"
MODEL_SIZE = 232589
MODEL_SHA256 = "8f2383e4dd3cfbb4553ea8718107fc0423210dc964f9f4280604804ed2552fa4"
# The model lives in Git LFS, so the plain "raw" address returns a pointer file: the media host serves the real bytes.
MODEL_URLS = (
    "https://media.githubusercontent.com/media/opencv/opencv_zoo/main/models/face_detection_yunet/" + MODEL_FILE,
    "https://github.com/opencv/opencv_zoo/raw/main/models/face_detection_yunet/" + MODEL_FILE,
)
CHOICES = ("auto", "yunet", "haar", "saliency")
RETRY_AFTER_S = 600  # a failed download is not retried for ten minutes, so offline analyses do not stall on every media

Fetch = Callable[[str, Path], None]
_failed_at: dict[str, float] = {}
_lock = threading.Lock()


def reset() -> None:
    """Forget remembered download failures (tests, and the 'try again' button)."""
    with _lock:
        _failed_at.clear()


def _cv2() -> Any:
    try:
        import cv2  # type: ignore

        return cv2
    except Exception:  # noqa: BLE001 - OpenCV is optional
        return None


def download(url: str, dest: Path) -> None:
    """Fetch ``url`` into ``dest`` (default fetcher; tests and offline setups replace it)."""
    req = urllib.request.Request(url, headers={"User-Agent": "lumiere-hoard"})
    with urllib.request.urlopen(req, timeout=20) as resp, open(dest, "wb") as out:  # noqa: S310 - fixed https addresses above
        while True:
            block = resp.read(1 << 16)
            if not block:
                break
            out.write(block)


def model_path(models_dir: Path) -> Path:
    return Path(models_dir) / MODEL_FILE


def _valid(path: Path) -> bool:
    try:
        return path.is_file() and path.stat().st_size == MODEL_SIZE and sha256_file(path) == MODEL_SHA256
    except OSError:
        return False


def ensure_model(models_dir: Path, *, fetch: Optional[Fetch] = None, allow_download: bool = True) -> tuple[Optional[Path], str]:
    """The YuNet model file and a reason when there is none. A cached file is used as is once it passed the size and hash
    check; otherwise it is downloaded (to a temporary name, verified, then moved into place)."""
    dest = model_path(models_dir)
    if _valid(dest):
        return dest, ""
    if not allow_download:
        return None, "model not downloaded yet"
    with _lock:
        failed = _failed_at.get(str(dest))
    if failed and time.time() - failed < RETRY_AFTER_S:
        return None, "download failed recently; using the next detector"
    fetch = fetch or download
    Path(models_dir).mkdir(parents=True, exist_ok=True)
    tmp = dest.with_name(dest.name + ".part")
    errors: list[str] = []
    for url in MODEL_URLS:
        try:
            fetch(url, tmp)
            if _valid(tmp):
                tmp.replace(dest)
                return dest, ""
            errors.append("the downloaded file failed the size/hash check")
        except Exception as error:  # noqa: BLE001 - any network problem means "use the next detector"
            errors.append(f"{type(error).__name__}: {str(error)[:100]}")
        finally:
            tmp.unlink(missing_ok=True)
    with _lock:
        _failed_at[str(dest)] = time.time()
    log.info("YuNet model not available (%s)", "; ".join(errors))
    return None, "could not download the model (" + errors[-1] + ")"


class FaceDetector:
    """One detector behind a single call: ``detect(rgb_frame) -> [(x, y, w, h, score)]`` in pixels, biggest first."""

    name = "none"

    def detect(self, frame: np.ndarray) -> list[tuple[float, float, float, float, float]]:
        raise NotImplementedError


class YuNetDetector(FaceDetector):
    name = "yunet"

    def __init__(self, cv2: Any, model: Path, *, score: float = 0.6):
        self.cv2 = cv2
        self.model = str(model)
        self.score = score
        self.size: Optional[tuple[int, int]] = None
        self.net: Any = None

    def detect(self, frame: np.ndarray) -> list[tuple[float, float, float, float, float]]:
        h, w = frame.shape[:2]
        if self.net is None or self.size != (w, h):
            self.net = self.cv2.FaceDetectorYN.create(self.model, "", (w, h), self.score, 0.3, 500)
            self.size = (w, h)
        bgr = self.cv2.cvtColor(frame, self.cv2.COLOR_RGB2BGR)
        _, found = self.net.detect(bgr)
        faces = [] if found is None else [(float(r[0]), float(r[1]), float(r[2]), float(r[3]), float(r[-1])) for r in found]
        return sorted(faces, key=lambda f: -f[2] * f[3])


class HaarDetector(FaceDetector):
    name = "haar"

    def __init__(self, cv2: Any, front: Any, profile: Any):
        self.cv2, self.front, self.profile = cv2, front, profile

    def detect(self, frame: np.ndarray) -> list[tuple[float, float, float, float, float]]:
        gray = self.cv2.cvtColor(frame, self.cv2.COLOR_RGB2GRAY)
        side = max(14, frame.shape[0] // 18)
        faces = list(self.front.detectMultiScale(gray, scaleFactor=1.15, minNeighbors=5, minSize=(side, side)))
        if not faces and not self.profile.empty():
            faces = list(self.profile.detectMultiScale(gray, scaleFactor=1.15, minNeighbors=5, minSize=(side, side)))
        out = [(float(x), float(y), float(w), float(h), 1.0) for x, y, w, h in faces]
        return sorted(out, key=lambda f: -f[2] * f[3])


def _haar(cv2: Any) -> Optional[HaarDetector]:
    try:
        front = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
        profile = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_profileface.xml")
        if front.empty():
            return None
        return HaarDetector(cv2, front, profile)
    except Exception:  # noqa: BLE001
        return None


def pick(models_dir: Path, *, prefer: str = "auto", fetch: Optional[Fetch] = None, allow_download: bool = True) -> tuple[Optional[FaceDetector], dict[str, Any]]:
    """The detector to use and what happened on the way: ``{detector, requested, fallback_from?, reason?}``. ``prefer`` is
    auto (YuNet, then Haar, then none), yunet or haar (each still falls back down the list) or saliency (no detector)."""
    prefer = prefer if prefer in CHOICES else "auto"
    info: dict[str, Any] = {"requested": prefer}
    if prefer == "saliency":
        return None, {**info, "detector": "saliency", "reason": "face detection switched off in the settings"}
    cv2 = _cv2()
    if cv2 is None:
        return None, {**info, "detector": "saliency", "reason": "OpenCV is not installed"}
    skipped: Optional[str] = None
    if prefer in ("auto", "yunet"):
        if not hasattr(cv2, "FaceDetectorYN"):
            skipped = "this OpenCV has no FaceDetectorYN (needs 4.5.4 or newer)"
        else:
            model, why = ensure_model(models_dir, fetch=fetch, allow_download=allow_download)
            if model is not None:
                try:
                    return YuNetDetector(cv2, model), {**info, "detector": "yunet"}
                except Exception as error:  # noqa: BLE001
                    skipped = f"YuNet failed to start ({type(error).__name__})"
            else:
                skipped = why
    haar = _haar(cv2)
    if haar is not None:
        return haar, {**info, "detector": "haar", **({"fallback_from": "yunet", "reason": skipped} if skipped else {})}
    return None, {**info, "detector": "saliency", "reason": (skipped + "; " if skipped else "") + "no Haar cascades found"}


def status(models_dir: Path, *, prefer: str = "auto") -> dict[str, Any]:
    """What face detection can do right now, without downloading anything (shown in lumiere_status and the settings)."""
    cv2 = _cv2()
    path = model_path(models_dir)
    downloaded = _valid(path)
    out: dict[str, Any] = {"opencv": cv2 is not None, "opencv_version": getattr(cv2, "__version__", None), "preferred": prefer,
                           "yunet": {"supported": bool(cv2 is not None and hasattr(cv2, "FaceDetectorYN")), "downloaded": downloaded,
                                     "model": MODEL_FILE, "size": MODEL_SIZE}, "haar": bool(cv2 is not None and _haar(cv2) is not None)}
    wants_yunet = out["yunet"]["supported"] and prefer in ("auto", "yunet")
    if prefer == "saliency" or cv2 is None:
        out["active"] = "saliency"
    elif wants_yunet and downloaded:
        out["active"] = "yunet"
    elif prefer != "saliency" and out["haar"]:
        out["active"] = "haar"
    else:
        out["active"] = "saliency"
    if wants_yunet and not downloaded:
        out["note"] = "The YuNet model (230 KB) is downloaded on the first reframing analysis; without network the Haar cascades are used."
    return out
