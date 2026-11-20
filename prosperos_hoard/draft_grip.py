"""A local paint guide and a visibility mask for a depth-guided draft key."""

from __future__ import annotations

import io
import json

import numpy as np
from PIL import Image, ImageChops, ImageDraw, ImageEnhance, ImageFilter, PngImagePlugin

from . import draft_art


def png_bytes(image: Image.Image) -> bytes:
    output = io.BytesIO()
    image.save(output, format="PNG")
    return output.getvalue()


class GripRegion:
    def __init__(self, color: Image.Image, labels: Image.Image, part_ids: list[int],
                 layers: list[Image.Image], prop_masks: list[Image.Image], size: tuple[int, int],
                 paint_props: bool, preserve_ids: list[int], *,
                 paper: tuple[int, Image.Image] | None = None,
                 contacts: list | None = None,
                 cylinder_layers: tuple[int, ...] | list[int] = (),
                 continuity: tuple[Image.Image, Image.Image] | None = None):
        if color.size != labels.size or any(layer.size != labels.size for layer in layers + prop_masks):
            raise ValueError("the blocking color, part IDs and accessory layers must share a pixel grid")
        if labels.mode != "L":
            raise ValueError("part IDs must be an 8-bit grayscale PNG")
        # Resample the part IDs and alpha on the same pixel grid. The visibility
        # raster has already settled which hand, digit or prop is in front.
        labels_at_source_size = labels
        layers = [
            (draft_art.light_cylinder(layer, prop_masks[index]) if index in cylinder_layers else layer)
            .resize(size, Image.Resampling.NEAREST)
            for index, layer in enumerate(layers)
        ]
        self.layers = layers
        self.paper = paper
        self.props = Image.new("RGBA", size)
        for index, layer in enumerate(layers):
            if paper and index == paper[0]:
                layer = paper[1].copy()
                layer.putalpha(layers[index].getchannel("A"))
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
            (left - 2 * margin, top - 2 * margin, right + 2 * margin, bottom + 4 * margin),
            radius=2 * margin, fill=255,
        )
        preserved = Image.fromarray(np.isin(ids, preserve_ids).astype(np.uint8) * 255)
        preserved = preserved.filter(ImageFilter.MaxFilter(2 * max(2, margin // 2) + 1))
        self.continuity = None
        if continuity:
            anchor, previous_labels = continuity
            if anchor.size != size or previous_labels.size != labels.size or previous_labels.mode != "L":
                raise ValueError("the continuity key must use the same output and blocking pixel grids")
            previous = np.asarray(previous_labels.resize(size, Image.Resampling.NEAREST))
            stable = np.isin(previous, preserve_ids)
            if not stable.any() or (ids[stable] == previous[stable]).mean() < .95:
                raise ValueError("the continuity key must share the same framing of the preserved parts")
            subject = Image.fromarray(((previous != 0) | (ids != 0)).astype(np.uint8) * 255)
            subject = ImageChops.lighter(subject, self.props.getchannel("A"))
            subject = ImageChops.subtract(subject, preserved)
            changes = Image.new("L", size)
            if (box := subject.getbbox()) is not None:
                left, top, right, bottom = box
                # Include the whole moving subject since its painted silhouette
                # can extend outside the blocking labels in either pose.
                padding = 3 * margin
                ImageDraw.Draw(changes).rounded_rectangle(
                    (left - padding, top - padding, right + padding, bottom + padding),
                    radius=padding, fill=255,
                )
                changes = ImageChops.lighter(subject, changes.filter(ImageFilter.GaussianBlur(margin)))
            fixed = ImageChops.lighter(preserved, preserved.filter(ImageFilter.GaussianBlur(3)))
            self.continuity = (anchor, ImageChops.subtract(changes, fixed))
        self.clean = ImageChops.multiply(envelope, self.background).filter(ImageFilter.GaussianBlur(2))
        self.clean = ImageChops.subtract(self.clean, preserved)
        self.mask = envelope.filter(ImageFilter.GaussianBlur(max(1, margin / 3)))
        self.mask = ImageChops.subtract(self.mask, preserved)
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
        self.contact_prompt = ""
        if contacts is None:
            self.near_edge = ImageChops.multiply(fringe_mask, props_alpha)
        else:
            self.near_edge = Image.new("L", size)
            instructions = []
            for contact, digits in draft_art.grip_contacts(contacts, labels_at_source_size, prop_masks, part_ids):
                digits = digits.resize(size, Image.Resampling.NEAREST)
                edge = digits.filter(ImageFilter.MaxFilter(2 * fringe + 1)).filter(ImageFilter.GaussianBlur(1))
                prop_alpha = layers[contact["layer"]].getchannel("A")
                self.near_edge = ImageChops.lighter(self.near_edge, ImageChops.multiply(edge, prop_alpha))
                target = contact["target"].replace("_", " ")
                instructions.append(f"Her {contact['hand']} hand must curl around the {target} precisely at the contact in <image1>.")
            self.contact_prompt = " ".join(instructions)
        self.paper_cleanup = None
        if paper:
            paper_alpha = layers[paper[0]].getchannel("A")
            full_paper = prop_masks[paper[0]].resize(size, Image.Resampling.NEAREST)
            # The paint model can overshoot the paper outline with a light
            # rim. Keep the first pass outside it, except at the visible hand.
            outer = ImageChops.subtract(full_paper.filter(ImageFilter.MaxFilter(2 * margin + 1)), full_paper)
            near_outline = ImageChops.subtract(full_paper.filter(ImageFilter.MaxFilter(2 * fringe + 1)), full_paper)
            rim_blend = ImageChops.lighter(near_outline, outer.filter(ImageFilter.GaussianBlur(max(1, margin / 3))))
            self.mask = ImageChops.subtract(self.mask, ImageChops.subtract(rim_blend, self.guide))
            outside = ImageChops.multiply(outer, self.background)
            self.paper_cleanup = ImageChops.subtract(outside, self.guide.filter(ImageFilter.MaxFilter(2 * fringe + 1)))
            props_alpha = ImageChops.subtract(props_alpha, paper_alpha)
        for index in cylinder_layers:
            props_alpha = ImageChops.subtract(props_alpha, layers[index].getchannel("A"))
        self.prop_paint = props_alpha.filter(ImageFilter.MinFilter(3)) if paint_props else None
        bounds = ImageChops.lighter(self.mask, self.props.getchannel("A")) if paint_props or paper else self.mask
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
        background = Image.new("RGB", rgb.size, backdrop)
        if self.paper_cleanup is not None:
            # Inpaint just the outside rim by pulling from the surrounding
            # scene through pixels the blocking marks as background.
            rim = self.paper_cleanup.filter(ImageFilter.GaussianBlur(1))
            rim = ImageChops.multiply(rim, self.background)
            background = rgb.filter(ImageFilter.MedianFilter(2 * max(2, round(min(rgb.size) * .012)) + 1))
            base.paste(background, (0, 0), rim)
            rgb = base.convert("RGB")
            background = Image.new("RGB", rgb.size, backdrop)
        cleared = Image.composite(background, rgb, self.clean)
        guide = Image.composite(self.color, cleared, self.guide).convert("RGBA")
        guide.alpha_composite(self.props)
        return base, guide.crop(self.box).convert("RGB")

    def finish(self, base: Image.Image, painted: Image.Image, recipe: dict) -> bytes:
        patch = painted.convert("RGBA").resize(self.box_size, Image.Resampling.LANCZOS)
        base.paste(patch, self.box, self.mask.crop(self.box))
        if self.paper:
            index, texture = self.paper
            scene_paint = Image.new("RGBA", base.size)
            scene_paint.paste(patch, self.box)
            for number, layer in enumerate(self.layers):
                if number == index:
                    layer = draft_art.paper_in_light(texture, layer.getchannel("A"), scene_paint)
                base.alpha_composite(layer)
        else:
            base.alpha_composite(self.props)
        if self.prop_paint is not None:
            base.paste(patch, self.box, self.prop_paint.crop(self.box))
        base.paste(patch, self.box, self.near_edge.crop(self.box))
        if self.continuity:
            anchor, changes = self.continuity
            base = Image.composite(base, anchor, changes)
        info = PngImagePlugin.PngInfo()
        info.add_itxt("prospero.draft_recipe", json.dumps(recipe, sort_keys=True, ensure_ascii=False))
        output = io.BytesIO()
        base.save(output, format="PNG", pnginfo=info)
        return output.getvalue()

    @property
    def box_size(self) -> tuple[int, int]:
        left, top, right, bottom = self.box
        return right - left, bottom - top
