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
