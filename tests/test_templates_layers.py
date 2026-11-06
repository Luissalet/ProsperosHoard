"""Every text/image field a design template declares is drawn by at least
one layer of every variant (a declared field with no layer is silently
dropped from the render)."""
import pytest

from prosperos_hoard import templates


def _fields_used(layout):
    used = set()
    for layer in layout["layers"]:
        for key in ("text", "asset"):
            ref = layer.get(key)
            if isinstance(ref, dict) and ref.get("field"):
                used.add(ref["field"])
    return used


@pytest.mark.parametrize("template", ["tracklist_back"])
def test_every_declared_field_has_a_layer(template):
    declared = {f for f, (kind, _req, _d) in templates.TEMPLATE_FIELDS[template].items() if kind in ("text", "image")}
    for variant in templates.VARIANTS.get(template, [None]):
        used = _fields_used(templates.get_layout(template, variant))
        assert declared <= used, (variant, declared - used)
