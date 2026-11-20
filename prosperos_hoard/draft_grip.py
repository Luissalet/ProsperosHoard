"""A local paint guide and a visibility mask for a depth-guided draft key."""

from __future__ import annotations

import io
import json

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, PngImagePlugin


def png_bytes(image: Image.Image) -> bytes:
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


class GripRegion:
    def __init__(self, color: Image.Image, labels: Image.Image, part_ids: list[int],
                 layers: list[Image.Image], prop_masks: list[Image.Image], size: tuple[int, int],
                 paint_props: bool):
        if color.size != labels.size or any(layer.size != labels.size for layer in layers + prop_masks):
            raise ValueError("the blocking color, part IDs and accessory layers must share a pixel grid")
        if labels.mode != "L":
            raise ValueError("part IDs must be an 8-bit grayscale PNG")
        # Resample the part IDs and alpha on the same pixel grid. The visibility
        # raster has already settled which hand, digit or prop is in front.
        layers = [layer.resize(size, Image.Resampling.NEAREST) for layer in layers]
        self.props = Image.new("RGBA", size)
        for layer in layers:
            self.props.alpha_composite(layer)
        self.color = ImageEnhance.Color(color.convert("RGB").resize(size, Image.Resampling.BILINEAR)).enhance(0.35)
        ids = np.asarray(labels.resize(size, Image.Resampling.NEAREST))
        visible = np.isin(ids, part_ids)
        self.background = Image.fromarray((ids == 0).astype(np.uint8) * 255)
        self.guide = Image.fromarray(visible.astype(np.uint8) * 255)
        if self.guide.getbbox() is None:
            raise ValueError("none of the selected gripping parts are visible in this key")
        if np.any(visible & (np.asarray(self.props.getchannel("A")) == 255)):
            raise ValueError("an accessory covers visible gripping parts; use the occluded layers from the key")
        margin = max(3, round(min(size) * 0.022))
        left, top, right, bottom = self.guide.getbbox()
        # Include the area where the first pass can leave a displaced arm or
        # open hand. Only feather the perimeter where it rejoins that pass.
        envelope = Image.new("L", size)
        ImageDraw.Draw(envelope).rounded_rectangle(
            (left - 2 * margin, top - margin, right + 2 * margin, bottom + 4 * margin),
            radius=2 * margin, fill=255,
        )
        self.clean = ImageChops.multiply(envelope, self.background).filter(ImageFilter.GaussianBlur(2))
        arm = self.guide.filter(ImageFilter.MaxFilter(7)).filter(ImageFilter.GaussianBlur(1))
        self.mask = ImageChops.lighter(arm, self.clean)
        props_alpha = self.props.getchannel("A")
        full = np.logical_or.reduce([np.asarray(mask.resize(size, Image.Resampling.NEAREST)) > 0
                                     for mask in prop_masks])
        if np.any((np.asarray(props_alpha) > 0) & ~full):
            raise ValueError("an accessory's visible layer extends outside its full mask")
        # The model can paint naturally curved fingers across the contact. Let
        # the new paint survive just around pixels known to be near-side parts.
        front = Image.fromarray((visible & full).astype(np.uint8) * 255)
        fringe = max(2, margin // 2)
        fringe_mask = front.filter(ImageFilter.MaxFilter(2 * fringe + 1)).filter(ImageFilter.GaussianBlur(1))
        self.near_edge = ImageChops.multiply(fringe_mask, props_alpha)
        self.prop_paint = props_alpha.filter(ImageFilter.MinFilter(3)) if paint_props else None
        bounds = Image.fromarray(np.maximum(np.asarray(self.mask), np.asarray(props_alpha))) if paint_props else self.mask
        left, top, right, bottom = bounds.getbbox()
        side = min(max(right - left, bottom - top) + 6 * margin, min(size))
        # For a wide spread of parts, the render can use a rectangular crop.
        crop_w, crop_h = max(side, right - left), max(side, bottom - top)
        x = max(0, min(size[0] - crop_w, (left + right - crop_w) // 2))
        y = max(0, min(size[1] - crop_h, (top + bottom - crop_h) // 2))
        self.box = (x, y, x + crop_w, y + crop_h)

    def prepare(self, data: bytes) -> tuple[Image.Image, Image.Image]:
        with Image.open(io.BytesIO(data)) as source:
            base = source.convert("RGBA")
        if base.size != self.mask.size:
            raise ValueError("the draft render and blocking guides have different sizes")
        rgb = base.convert("RGB")
        # Clear the displaced paint from the camera background, but keep the
        # original body and clothing behind the arm. Use the frame's own
        # background color so the clean-up stays close to the source light.
        pixels = np.asarray(rgb)
        backdrop = tuple(np.median(np.concatenate((pixels[:, 0], pixels[:, -1])), axis=0).astype(int))
        cleared = Image.composite(Image.new("RGB", rgb.size, backdrop), rgb, self.clean)
        guide = Image.composite(self.color, cleared, self.guide).convert("RGBA")
        guide.alpha_composite(self.props)
        return base, guide.crop(self.box).convert("RGB")

    def finish(self, base: Image.Image, painted: Image.Image, recipe: dict) -> bytes:
        patch = painted.convert("RGBA").resize(self.box_size, Image.Resampling.LANCZOS)
        base.paste(patch, self.box, self.mask.crop(self.box))
        base.alpha_composite(self.props)
        if self.prop_paint is not None:
            base.paste(patch, self.box, self.prop_paint.crop(self.box))
        base.paste(patch, self.box, self.near_edge.crop(self.box))
        info = PngImagePlugin.PngInfo()
        info.add_itxt("prospero.draft_recipe", json.dumps(recipe, sort_keys=True, ensure_ascii=False))
        output = io.BytesIO()
        base.save(output, format="PNG", pnginfo=info)
        return output.getvalue()

    @property
    def box_size(self) -> tuple[int, int]:
        left, top, right, bottom = self.box
        return right - left, bottom - top
