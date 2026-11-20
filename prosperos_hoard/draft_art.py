"""Project a paper reference into a draft key and light it from the local paint pass."""

from __future__ import annotations

import numpy as np
from PIL import Image, ImageChops, ImageFilter, ImageOps


def project_paper(source: Image.Image, size: tuple[int, int], quad: list) -> Image.Image:
    """Quad points are the projected source corners: top-left, top-right, bottom-right, bottom-left."""
    if not isinstance(quad, list) or len(quad) != 4 or any(
        not isinstance(point, list) or len(point) != 2
        or any(type(v) not in (int, float) for v in point) for point in quad
    ):
        raise ValueError("paper quad must contain four [x, y] points")
    dest = np.asarray(quad, dtype=float)
    if not np.isfinite(dest).all() or np.any(abs(dest) > 2 * max(size)):
        raise ValueError("paper quad points must be finite and near the canvas")
    edges = np.roll(dest, -1, axis=0) - dest
    next_edges = np.roll(edges, -1, axis=0)
    turns = edges[:, 0] * next_edges[:, 1] - edges[:, 1] * next_edges[:, 0]
    if not (np.all(turns > 0.01) or np.all(turns < -0.01)):
        raise ValueError("paper quad must be convex and follow the source corner order")

    # Extend the colours just outside the face for the finite thickness of
    # the blocking mesh. The actual silhouette still comes from the alpha.
    original = ImageOps.exif_transpose(source).convert("RGB")
    # Band-limit the source before the perspective sample, particularly for
    # hairline marks on a map that is only a few dozen pixels wide.
    lengths = np.linalg.norm(edges, axis=1)
    sampled_size = tuple(min(n, max(2, round(2 * max(lengths[i], lengths[i + 2]))))
                         for i, n in enumerate(original.size))
    original = original.resize(sampled_size, Image.Resampling.LANCZOS)
    width, height = original.size
    pad = max(3, round(min(original.size) * 0.05))
    texture = Image.fromarray(np.pad(np.asarray(original), ((pad, pad), (pad, pad), (0, 0)), mode="edge"))
    src = [(pad, pad), (pad + width, pad), (pad + width, pad + height), (pad, pad + height)]
    rows, offsets = [], []
    for (x, y), (u, v) in zip(dest, src):
        rows.extend([[x, y, 1, 0, 0, 0, -u * x, -u * y], [0, 0, 0, x, y, 1, -v * x, -v * y]])
        offsets.extend([u, v])
    coefficients = np.linalg.solve(rows, offsets)
    return texture.transform(size, Image.Transform.PERSPECTIVE, tuple(coefficients), Image.Resampling.BICUBIC,
                             fillcolor=original.resize((1, 1)).getpixel((0, 0)))


def paper_in_light(texture: Image.Image, visible: Image.Image, paint: Image.Image) -> Image.Image:
    """Keep the source ink while borrowing low-frequency shading from the painted paper."""
    alpha = np.asarray(visible, dtype=np.float32) / 255
    interior = visible.filter(ImageFilter.MinFilter(3))
    if interior.getbbox() is None:
        interior = visible
    shade = paint.convert("L")
    values = np.asarray(shade, dtype=np.float32)[np.asarray(interior) >= 128]
    source = np.asarray(texture, dtype=np.float32)
    source_values = np.asarray(texture.convert("L"), dtype=np.float32)[alpha >= 0.5]
    if values.size and source_values.size:
        # Normalize the blur by the blurred mask so a nearby sleeve or
        # background cannot wash out the ink at the paper's edge.
        local = np.asarray(ImageChops.multiply(shade, interior).filter(ImageFilter.GaussianBlur(3)), dtype=np.float32)
        weight = np.asarray(interior.filter(ImageFilter.GaussianBlur(3)), dtype=np.float32) / 255
        median = float(np.median(values))
        light = np.divide(local, weight, out=np.full_like(local, median), where=weight > 0.05)
        anchor = max(1.0, float(np.median(source_values)))
        source = source * np.clip(light / anchor, 0.68, 1.20)[..., None]
    paper = Image.fromarray(np.rint(source).clip(0, 255).astype(np.uint8))
    # Blend the existing edge inward. No light pixels are introduced into
    # the background or into the holes reserved for foreground fingers.
    paper.putalpha(ImageChops.multiply(visible, visible.filter(ImageFilter.GaussianBlur(0.55))))
    return paper


def light_cylinder(layer: Image.Image, full_mask: Image.Image) -> Image.Image:
    """Shade the complete tube axis before cutting it at foreground occlusions."""
    y, x = np.nonzero(np.asarray(full_mask) >= 128)
    if len(x) < 4:
        return layer
    axis_points = np.column_stack((x, y)).astype(np.float32)
    center = axis_points.mean(axis=0)
    _, axes = np.linalg.eigh(np.cov(axis_points.T))
    across, along = ((axis_points - center) @ axes).T
    width, length = np.ptp(across), np.ptp(along)
    if width <= 0 or length <= 0:
        return layer
    y, x = np.nonzero(np.asarray(layer.getchannel("A")))
    coordinates = (np.column_stack((x, y)) - center) @ axes
    u = (coordinates[:, 0] - across.min()) / width
    v = (coordinates[:, 1] - along.min()) / length
    side = np.clip(2 * u - 1, -1, 1)
    lighting = 0.57 + 0.57 * np.sqrt(1 - side**2) + 0.16 * side
    ends = np.minimum(v, 1 - v)
    band = np.clip(1 - abs(ends - 0.045) / 0.024, 0, 1)[:, None]
    rgba = np.array(layer)
    color = rgba[y, x, :3] * lighting[:, None]
    rim = color * .78 + np.array([57, 37, 13])
    rgba[y, x, :3] = np.rint(color * (1 - band) + rim * band).clip(0, 255).astype(np.uint8)
    return Image.fromarray(rgba)


def grip_contacts(contacts: list, source_ids: Image.Image, prop_masks: list[Image.Image],
                  grip_ids: list[int]) -> list[tuple[dict, Image.Image]]:
    """Resolve declared hand-to-prop contacts on the blocking's source grid."""
    if not isinstance(contacts, list) or not 1 <= len(contacts) <= 2:
        raise ValueError("contacts needs one or two declared hands")
    ids = np.asarray(source_ids)
    width, height = source_ids.size
    radius = max(3, round(min(width, height) * 0.036))
    output = []
    used = set()
    for contact in contacts:
        if not isinstance(contact, dict) or set(contact) != {"hand", "layer", "target", "part_ids", "pixel"}:
            raise ValueError("a contact needs hand, layer, target, part_ids and pixel")
        hand, layer, target, parts, point = (contact[key] for key in ("hand", "layer", "target", "part_ids", "pixel"))
        if (hand not in ("left", "right") or hand in used or type(layer) is not int or not 0 <= layer < len(prop_masks)
                or not isinstance(target, str) or not target.strip() or len(target) > 60
                or not isinstance(parts, list) or not parts
                or any(type(part) is not int or part not in grip_ids for part in parts)
                or not isinstance(point, list) or len(point) != 2
                or any(type(value) not in (int, float) or not 0 <= value < size
                       for value, size in zip(point, (width, height)))):
            raise ValueError("contact must declare a distinct hand, its gripping labels and a point against a prop")
        x, y = (int(value) for value in point)
        box = (max(0, x - radius), max(0, y - radius), min(width, x + radius + 1), min(height, y + radius + 1))
        hand_mask = Image.fromarray(np.isin(ids, parts).astype(np.uint8) * 255)
        prop_mask = prop_masks[layer]
        if not hand_mask.crop(box).getbbox() or not prop_mask.crop(box).getbbox():
            raise ValueError(f"{hand} hand and {target} are separated at the declared contact")
        vicinity = Image.new("L", source_ids.size)
        vicinity.paste(255, box)
        nearby = prop_mask.filter(ImageFilter.MaxFilter(2 * radius + 1))
        output.append((contact, ImageChops.multiply(ImageChops.multiply(hand_mask, vicinity), nearby)))
        used.add(hand)
    return output
