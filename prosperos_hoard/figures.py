"""Retained diagram snapshots, rendered through the native image-asset path."""
from __future__ import annotations

import base64
from copy import deepcopy
import io
import re
from urllib.parse import urlsplit

from PIL import Image


def validate(value):
    """Validate the portable source without fetching or rewriting any of it."""
    if not isinstance(value, dict) or value.get("format") != "vitruvius.figure/1":
        raise ValueError("Choose a portable .vfigure diagram.")
    if not isinstance(value.get("document_id"), str) or not re.fullmatch(r"[0-9a-f]{12}", value["document_id"]):
        raise ValueError("The figure needs its source document identity.")
    revision = value.get("revision")
    if type(revision) is not int or revision < 1:
        raise ValueError("The figure needs a positive source revision.")
    content = value.get("content")
    diagram = content.get("diagram") if isinstance(content, dict) else None
    if not isinstance(diagram, dict) or not isinstance(diagram.get("nodes"), list):
        raise ValueError("The figure must retain its native diagram source.")
    page, state = value.get("page_id"), value.get("state_id")
    if page is not None:
        pages = (content.get("view") or {}).get("pages", [])
        if not isinstance(page, str) or not any(isinstance(p, dict) and p.get("id") == page for p in pages):
            raise ValueError("The captured page must exist in the retained source.")
    if state is not None:
        mechanism = content.get("mechanism")
        states = mechanism.get("states") if isinstance(mechanism, dict) else None
        if page is not None or not isinstance(state, str) or not 1 <= len(state) <= 60 or diagram.get("type") != "isometric" or not isinstance(states, list) or not any(
            isinstance(item, dict) and item.get("id") == state for item in states
        ):
            raise ValueError("The captured state must exist in the retained isometric source.")
    origin = value.get("origin", {})
    if not isinstance(origin, dict):
        raise ValueError("Figure origin links must be HTTP(S) addresses.")
    for link in origin.values():
        if not isinstance(link, str):
            raise ValueError("Figure origin links must be HTTP(S) addresses.")
        address = urlsplit(link)
        if address.scheme not in ("http", "https") or not address.netloc or address.username or address.password:
            raise ValueError("Figure origin links must be HTTP(S) addresses without credentials.")
    preview(value)
    # Return the complete supplied snapshot, including future optional source fields.
    return deepcopy(value)


def preview(value):
    capture = value.get("preview")
    if not isinstance(capture, dict) or capture.get("mime") != "image/png" or not isinstance(capture.get("data"), str):
        raise ValueError("The figure needs a retained PNG preview.")
    try:
        png = base64.b64decode(capture["data"], validate=True)
        with Image.open(io.BytesIO(png)) as image:
            if image.format != "PNG":
                raise ValueError("The figure preview must be a PNG image.")
            width, height = image.size
            image.verify()
        return png, width, height
    except (ValueError, OSError, SyntaxError, Image.DecompressionBombError) as error:
        raise ValueError(f"Invalid figure preview: {error}") from error
