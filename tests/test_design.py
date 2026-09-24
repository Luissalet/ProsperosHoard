from prosperos_hoard import design, templates


def _resolver(_asset_id):
    return None


def test_render_layout_is_deterministic_for_fixed_seed():
    """Golden test: a layout with no non-seeded randomness (no `grain`,
    which uses PIL's own non-deterministic noise) renders to byte-identical
    pixels every time for the same fields."""
    layout = {
        "width": 400, "height": 300,
        "layers": [
            {"type": "rect", "x": 0, "y": 0, "w": 400, "h": 300, "fill": "#141018ff"},
            {"type": "text", "x": 20, "y": 20, "w": 360, "h": 100, "text": "Hello Prospero",
             "font": "inter", "size": 40, "colour": "#ff4d8dff", "align": "center"},
            {"type": "badge", "x": 20, "y": 200, "w": 200, "h": 50, "text": "No. 007/250",
             "fill": "#00000080", "colour": "#ffffffff"},
        ],
    }
    img1 = design.render_layout(layout, {}, _resolver)
    img2 = design.render_layout(layout, {}, _resolver)
    assert design.pixel_hash(img1) == design.pixel_hash(img2)


def test_thumbnail_template_is_deterministic():
    layout = templates.get_layout("thumbnail")
    fields = {"title": "Episode One", "accent": "#ff4d8d"}
    img1 = design.render_layout(layout, fields, _resolver)
    img2 = design.render_layout(layout, fields, _resolver)
    assert design.pixel_hash(img1) == design.pixel_hash(img2)
    assert img1.size == (1280, 720)


def test_text_auto_fit_shrinks_long_text_to_fit_box():
    from PIL import Image, ImageDraw

    canvas = Image.new("RGBA", (10, 10))
    draw = ImageDraw.Draw(canvas)
    long_text = "A very long line of text that should not fit at the max size " * 3
    font, lines = design._fit_text(draw, long_text, "inter", (300, 120), max_size=80, min_size=10)
    line_height = (font.getbbox("Ay")[3] - font.getbbox("Ay")[1]) * 1.25
    assert line_height * len(lines) <= 120 + 1
    for line in lines:
        bbox = draw.textbbox((0, 0), line, font=font)
        assert bbox[2] - bbox[0] <= 300 + 1


def test_all_named_templates_render_without_error():
    for name, (w, h) in templates.TEMPLATE_DIMENSIONS.items():
        variant = "center_title" if name == "album_cover" else None
        layout = templates.get_layout(name, variant)
        fields = {
            "member_name": "Test", "role": "Vocal", "accent": "#ff4d8d", "group_logo": None,
            "serial": "No. 001/100", "message": "hi", "title": "Title", "subtitle": "Subtitle",
            "tagline": "tag", "quote": "a quote", "attribution": "- someone", "group_name": "Group",
            "tracks": "1. Track\n2. Track", "image": None, "cover_image": None,
        }
        img = design.render_layout(layout, fields, _resolver)
        assert img.size == (w, h)


def test_image_bytes_png_and_print_dpi():
    from PIL import Image

    img = Image.new("RGBA", (10, 10), (255, 0, 0, 255))
    normal = design.image_bytes(img, print_mode=False)
    printed = design.image_bytes(img, print_mode=True)
    assert normal.startswith(b"\x89PNG")
    assert printed.startswith(b"\x89PNG")
    assert normal != printed  # different DPI metadata


def _ink_outside(img, box):
    """Pixels brighter than the black background outside `box`."""
    import numpy as np

    x, y, w, h = box
    arr = np.asarray(img.convert("L")).copy()
    arr[y:y + h, x:x + w] = 0
    return int((arr > 40).sum())


def test_tracklist_columns_never_draw_outside_their_box():
    """A one-column row used to be left out of the width check, so a long
    one ran off the box (and the canvas); too many rows ran off the bottom."""
    box = (40, 30, 220, 120)
    rows = ["Side A"] + [f"{i:02d}  A song title that goes on and on and on  3:{i:02d}" for i in range(1, 30)]
    rows.insert(3, "An extremely long one-column heading that cannot possibly fit in the box at any size")
    layout = {"width": 320, "height": 200, "layers": [
        {"type": "rect", "x": 0, "y": 0, "w": 320, "h": 200, "fill": "#000000ff"},
        {"type": "text", "x": box[0], "y": box[1], "w": box[2], "h": box[3], "text": "\n".join(rows),
         "font": "inter", "size": 40, "colour": "#ffffffff", "columns": {"indent": 0.12}, "muted_colour": "#ffffffff"},
    ]}
    img = design.render_layout(layout, {}, _resolver)
    assert _ink_outside(img, box) == 0
    # a short list still renders at the largest size that fits (no needless shrinking)
    short = dict(layout, layers=[layout["layers"][0], dict(layout["layers"][1], text="01  Intro  1:00\nOne column row")])
    assert _ink_outside(design.render_layout(short, {}, _resolver), box) == 0


def test_image_layer_honours_exif_orientation(tmp_path):
    """A phone photo stored sideways with an EXIF orientation is laid out
    the way it is viewed."""
    from PIL import Image

    stored = Image.new("RGB", (80, 40), (220, 20, 20))
    stored.paste((20, 20, 220), (40, 0, 80, 40))  # stored: red left, blue right
    exif = Image.Exif()
    exif[0x0112] = 6  # rotate 90 degrees clockwise to view: red on top
    path = tmp_path / "phone.jpg"
    stored.save(path, exif=exif, quality=95)
    layout = {"width": 40, "height": 80, "layers": [
        {"type": "image", "x": 0, "y": 0, "w": 40, "h": 80, "asset": "a", "fit": "cover"}]}
    img = design.render_layout(layout, {}, lambda _a: path).convert("RGB")
    top, bottom = img.getpixel((20, 10)), img.getpixel((20, 70))
    assert top[0] > 150 and top[2] < 100, top
    assert bottom[2] > 150 and bottom[0] < 100, bottom
