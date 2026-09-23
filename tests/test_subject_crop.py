"""Photocard fronts keep a tall studio subject's head in frame."""
from PIL import Image, ImageDraw

from prosperos_hoard import design


def _studio(w=832, h=1248, top=20, bottom=1240, backdrop=(244, 190, 200)):
    img = Image.new("RGB", (w, h), backdrop)
    d = ImageDraw.Draw(img)
    d.ellipse([w * 0.35, top, w * 0.65, top + h * 0.2], fill=(210, 150, 60))      # a lantern head at the very top
    d.rectangle([w * 0.38, top + h * 0.2, w * 0.62, bottom], fill=(25, 25, 30))  # a long dark coat
    return img


def test_tall_subject_keeps_its_top():
    src = _studio()
    cx, cy = design.subject_centering(src, (1100, 1360))
    assert cx == 0.5
    assert cy < 0.05  # crop from (almost) the very top, not 35 % down the excess


def test_small_subject_is_centred():
    src = _studio(top=500, bottom=800)
    _, cy = design.subject_centering(src, (1100, 1360))
    assert 0.3 < cy < 0.8


def test_busy_background_falls_back_to_the_default():
    import numpy as np

    rng = np.random.default_rng(1)
    noisy = Image.fromarray(rng.integers(0, 255, (1248, 832, 3), dtype=np.uint8))
    assert design.subject_centering(noisy, (1100, 1360), (0.5, 0.35)) == (0.5, 0.35)


def test_photocard_front_uses_the_subject_crop():
    from prosperos_hoard import templates

    layer = next(lay for lay in templates.photocard_front()["layers"] if lay["type"] == "image")
    assert layer["focal"] == "subject"
