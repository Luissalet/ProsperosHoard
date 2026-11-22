"""Pixel-preserving regional masks, perspective transfer and receipts."""

from __future__ import annotations

import base64
import hashlib
import io
import json
import math
from dataclasses import dataclass

import numpy as np
from PIL import Image, ImageChops, PngImagePlugin
from scipy.ndimage import distance_transform_edt, gaussian_filter

TEMPLATE = "qwen21_edit"
MASK_KEY = "prospero.regional_mask"


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def asset_fingerprint(asset: dict) -> str:
    return sha256(json.dumps(asset, sort_keys=True).encode("utf-8"))


def png(image: Image.Image, **options) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, "PNG", **options)
    return buffer.getvalue()


def open_image(data: bytes) -> Image.Image:
    with Image.open(io.BytesIO(data)) as image:
        if image.mode not in ("1", "L", "LA", "P", "RGB", "RGBA"):
            raise ValueError("Regional edits require 8-bit RGB or grayscale pixels / Usa una imagen RGB o gris de 8 bits")
        mode = "RGBA" if "A" in image.getbands() or "transparency" in image.info else "RGB"
        result = image.convert(mode)
        # A palette transparency chunk is already represented by the alpha channel.
        result.info.pop("transparency", None)
        if image.mode in ("1", "L", "LA"):
            result.info.pop("icc_profile", None)
        return result


def _points(values, size, field):
    width, height = size
    if any(not (math.isfinite(x) and math.isfinite(y) and 0 <= x <= width and 0 <= y <= height) for x, y in values):
        raise ValueError(f"{field}: points must be inside the image / Los puntos deben estar dentro de la imagen")
    return [tuple(point) for point in values]


def _polygon(points, x, y):
    inside = np.zeros((y.size, x.size), dtype=bool)
    for (x0, y0), (x1, y1) in zip(points, points[1:] + points[:1]):
        if y0 != y1:
            inside ^= ((y0 > y) != (y1 > y)) & (x < (x1 - x0) * (y - y0) / (y1 - y0) + x0)
    return inside


@dataclass
class Region:
    quad: list[tuple[float, float]]
    bounds: tuple[int, int, int, int]
    alpha: Image.Image
    clip_polygon: list[tuple[float, float]] | None
    feather: float

    @classmethod
    def create(cls, size, box=None, quad=None, clip_polygon=None, feather=0):
        if (box is None) == (quad is None):
            raise ValueError("Pass box or quad / Indica box o quad")
        if box is not None:
            left, top, right, bottom = box
            if left >= right or top >= bottom:
                raise ValueError("box uses left, top, right, bottom / box usa izquierda, arriba, derecha, abajo")
            quad = [(left, top), (right, top), (right, bottom), (left, bottom)]
        quad = _points(quad, size, "quad")
        edges = [(x1 - x0, y1 - y0) for (x0, y0), (x1, y1) in zip(quad, quad[1:] + quad[:1])]
        if any(ax * by - ay * bx <= 0 for (ax, ay), (bx, by) in zip(edges, edges[1:] + edges[:1])):
            raise ValueError("quad needs four convex clockwise corners from top left / quad requiere cuatro esquinas convexas en sentido horario desde arriba a la izquierda")
        xs, ys = zip(*quad)
        left, top, right, bottom = math.floor(min(xs)), math.floor(min(ys)), math.ceil(max(xs)), math.ceil(max(ys))
        x = np.arange(left, right)[None, :] + .5
        y = np.arange(top, bottom)[:, None] + .5
        selected = _polygon(quad, x, y)
        if clip_polygon is not None:
            clip_polygon = _points(clip_polygon, size, "clip_polygon")
            selected &= _polygon(clip_polygon, x, y)
        if not selected.any():
            raise ValueError("The region covers no pixels / La región no cubre ningún píxel")
        if feather:
            distance = distance_transform_edt(np.pad(selected, 1))[1:-1, 1:-1]
            opacity = np.clip((distance - .5) / feather, 0, 1)
            alpha = np.rint(opacity * 255).astype(np.uint8)
        else:
            alpha = selected.astype(np.uint8) * 255
        return cls(quad, (left, top, right, bottom), Image.fromarray(alpha), clip_polygon, feather)

    def mask(self, size):
        full = Image.new("L", size)
        full.paste(self.alpha, self.bounds[:2])
        return full

    def warp(self, drawing):
        left, top = self.bounds[:2]
        # Prefilter and supersample the projection so thin lines survive
        # reduction instead of turning into a dotted pattern.
        patch_w, patch_h = self.alpha.size
        scale = 2 if patch_w * patch_h <= 4_000_000 else 1
        width, height = min(drawing.width, patch_w * scale), min(drawing.height, patch_h * scale)
        if (width, height) != drawing.size:
            drawing = drawing.resize((width, height), Image.Resampling.LANCZOS)
        rows, values = [], []
        for (x, y), (u, v) in zip(self.quad, ((0, 0), (width, 0), (width, height), (0, height))):
            x, y = (x - left) * scale, (y - top) * scale
            rows.extend(([x, y, 1, 0, 0, 0, -u * x, -u * y], [0, 0, 0, x, y, 1, -v * x, -v * y]))
            values.extend((u, v))
        coefficients = np.linalg.solve(rows, values)
        projected = drawing.transform((patch_w * scale, patch_h * scale), Image.Transform.PERSPECTIVE,
                                      coefficients, Image.Resampling.BICUBIC)
        return projected.resize(self.alpha.size, Image.Resampling.LANCZOS) if scale == 2 else projected


def _paper(pixels):
    """Estimate paper from the brighter half of the image, away from ink."""
    lightness = pixels.mean(axis=-1)
    return np.median(pixels[lightness >= np.quantile(lightness, .55)], axis=0)


def transfer_drawing(base, drawing, region, rotation, contrast, saturation):
    if rotation:
        drawing = drawing.rotate(-rotation, expand=True)
    drawing_rgb = np.asarray(drawing.convert("RGB"), dtype=np.float32)
    drawing_paper = _paper(drawing_rgb) if drawing.mode == "RGB" or drawing.getchannel("A").getextrema() == (255, 255) else 255
    warped = region.warp(drawing)
    pigment = np.asarray(warped.convert("RGB"), dtype=np.float32) - drawing_paper
    if warped.mode == "RGBA":
        pigment *= np.asarray(warped.getchannel("A"), dtype=np.float32)[..., None] / 255

    scene = np.asarray(base.crop(region.bounds).convert("RGB"), dtype=np.float32)
    allowed = np.asarray(region.alpha) > 0
    paper = _paper(scene[allowed])
    # Estimate lighting only from near-paper pixels. Sampling old ink would
    # carry the previous drawing across into the new one.
    error = scene - paper
    clean = allowed & (np.max(np.abs(error), axis=-1) < 24)
    weight = gaussian_filter(clean.astype(np.float32), 8)
    light = gaussian_filter(error * clean[..., None], (8, 8, 0)) / np.maximum(weight[..., None], .1)
    grain = np.clip(error - light, -5, 5) * clean[..., None]
    luma = pigment @ np.array([.2126, .7152, .0722], dtype=np.float32)
    chroma = pigment - luma[..., None]
    colored = paper + light + grain + luma[..., None] * contrast + chroma * saturation
    return Image.fromarray(np.rint(np.clip(colored, 0, 255)).astype(np.uint8))


def finish(base, candidate, region, receipt):
    result = base.copy()
    patch = candidate.convert("RGB")
    if base.mode == "RGBA":
        patch.putalpha(base.getchannel("A").crop(region.bounds))
    result.paste(patch, region.bounds[:2], region.alpha)
    mask = region.mask(base.size)
    # Compare all decoded channels, including invisible RGB and alpha.
    outside = mask.point([255] + [0] * 255)
    changed = Image.new("L", base.size)
    for band in ImageChops.difference(base, result).split():
        changed = ImageChops.lighter(changed, band)
    count = ImageChops.multiply(changed, outside).histogram()
    outside_count = mask.histogram()[0]
    mask_bytes = png(mask)
    info = PngImagePlugin.PngInfo()
    info.add_text(MASK_KEY, base64.b64encode(mask_bytes).decode("ascii"), zip=True)
    data = png(result, pnginfo=info, icc_profile=base.info.get("icc_profile"))
    receipt.update(
        version=1, quad=region.quad, clip_polygon=region.clip_polygon, feather_px=region.feather,
        mask_sha256=sha256(mask_bytes), output_sha256=sha256(data), outside_pixels=outside_count,
        outside_changed_pixels=sum(count[1:]),
        outside_changed_percent=100 * sum(count[1:]) / outside_count if outside_count else 0,
    )
    return data


def extract_mask(path, expected_sha256):
    with Image.open(path) as image:
        data = base64.b64decode(image.info[MASK_KEY], validate=True)
    if sha256(data) != expected_sha256:
        raise ValueError("Regional mask hash mismatch / La huella de la máscara no coincide")
    return data
