"""Parameters of an HTML motion composition, and variants rendered from data.

A composition becomes a template by writing ``{{name}}`` where a value goes (text, a colour, a number in a style) and, if it wants the
values in its script too, a JSON block with the defaults::

    <script type="application/json" id="motion-props">{"title": "Hola", "accent": "#b5583d", "count": 3}</script>
    ... const P = JSON.parse(document.getElementById('motion-props').textContent);

``props`` lists what a composition takes (the defaults of that block plus every placeholder, with its type). ``apply`` returns the source
with values: placeholders are replaced (HTML-escaped) and the JSON block carries the merged values, so the page script reads the same
values the markup shows. ``variants`` makes one new composition per data row (a CSV with a header, or a list of objects), each a normal
revisioned composition named after the row, and can queue each as an MP4 export: personalised or localised versions of one animation.
The template itself is never changed.
"""
from __future__ import annotations

import csv
import html as html_lib
import io
import json
import re
from typing import Any

from .errors import LumiereError

PLACEHOLDER = re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]{0,40})\s*\}\}")
_BLOCK = re.compile(r"(<script\b[^>]*\bid\s*=\s*[\"']motion-props[\"'][^>]*>)(.*?)(</script\s*>)", re.S | re.I)
MAX_VARIANTS = 50


def _kind(value: Any) -> str:
    if isinstance(value, bool):
        return "boolean"
    if isinstance(value, (int, float)):
        return "number"
    if isinstance(value, str) and re.fullmatch(r"#[0-9a-fA-F]{3,8}", value):
        return "color"
    return "text"


def defaults(source: str) -> dict[str, Any]:
    """The values of the ``motion-props`` JSON block ({} without one). A block that is not a JSON object is an error the author must fix."""
    match = _BLOCK.search(source)
    if not match or not match.group(2).strip():
        return {}
    try:
        value = json.loads(match.group(2))
    except json.JSONDecodeError as exc:
        raise LumiereError(f"The motion-props block is not valid JSON: {exc.msg} (line {exc.lineno}).", code="html_motion_props") from None
    if not isinstance(value, dict):
        raise LumiereError("The motion-props block must be a JSON object of name: value.", code="html_motion_props")
    return value


def props(source: str) -> dict[str, Any]:
    """What the composition takes: ``{"props": [{name, default, type, in_markup, in_script}], "placeholders": [...]}``."""
    base = defaults(source)
    marked = list(dict.fromkeys(PLACEHOLDER.findall(_BLOCK.sub("", source))))
    names = list(dict.fromkeys([*base, *marked]))
    rows = [{"name": n, "default": base.get(n), "type": _kind(base.get(n, "")), "in_markup": n in marked, "in_script": n in base} for n in names]
    return {"props": rows, "placeholders": marked, "has_block": bool(_BLOCK.search(source))}


def _text(value: Any) -> str:
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)


def _coerce(value: Any, like: Any) -> Any:
    """CSV cells are text: give them the type of the default (number, boolean) when they read as one."""
    if not isinstance(value, str) or like is None or isinstance(like, str):
        return value
    text = value.strip()
    if isinstance(like, bool):
        return text.lower() in ("1", "true", "sí", "si", "yes", "verdadero")
    if isinstance(like, (int, float)):
        try:
            number = float(text.replace(",", "."))
            return int(number) if isinstance(like, int) and number.is_integer() else number
        except ValueError:
            return value
    return value


def apply(source: str, values: dict[str, Any]) -> tuple[str, dict[str, Any]]:
    """``(source with values, merged values)``. Unknown names are refused so a typo in a column never renders silently with a default."""
    info = props(source)
    known = {p["name"] for p in info["props"]}
    unknown = sorted(k for k in values if k not in known and not k.startswith("_"))
    if unknown:
        raise LumiereError(f"Unknown parameters: {', '.join(unknown)}. This composition takes: {', '.join(sorted(known)) or 'nothing'}.",
                           code="html_motion_props")
    base = defaults(source)
    merged = {**base, **{k: _coerce(v, base.get(k)) for k, v in values.items() if not k.startswith("_")}}
    missing = [p for p in info["placeholders"] if p not in merged]
    if missing:
        raise LumiereError(f"No value for {', '.join(missing)} (and no default in the motion-props block).", code="html_motion_props")

    def block(match: re.Match) -> str:
        payload = json.dumps(merged, ensure_ascii=False).replace("</", "<\\/")
        return match.group(1) + payload + match.group(3)

    body = _BLOCK.sub(block, source, count=1)
    pieces = []
    last = 0
    for match in _BLOCK.finditer(body):  # placeholders inside the JSON block are data, not markup
        pieces.append(PLACEHOLDER.sub(lambda m: html_lib.escape(_text(merged[m.group(1)]), quote=True), body[last:match.start()]))
        pieces.append(match.group(0))
        last = match.end()
    pieces.append(PLACEHOLDER.sub(lambda m: html_lib.escape(_text(merged[m.group(1)]), quote=True), body[last:]))
    return "".join(pieces), merged


def rows_from(data: Any) -> list[dict[str, Any]]:
    """Rows from a list of objects, a JSON array text or CSV text with a header row (comma or semicolon)."""
    if isinstance(data, list):
        rows = data
    elif isinstance(data, str):
        text = data.strip()
        if not text:
            rows = []
        elif text[0] in "[{":
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as exc:
                raise LumiereError(f"The rows are not valid JSON: {exc.msg}.", code="html_motion_props") from None
            rows = parsed if isinstance(parsed, list) else [parsed]
        else:
            try:
                dialect = csv.Sniffer().sniff(text.splitlines()[0], delimiters=",;\t")
            except csv.Error:  # a single column has no delimiter to find
                dialect = csv.excel
            rows = [dict(r) for r in csv.DictReader(io.StringIO(text), dialect=dialect)]
            rows = [{(k or "").strip(): (v or "").strip() for k, v in r.items() if k} for r in rows]
    else:
        raise LumiereError("Rows must be a list of objects, a JSON array or CSV text.", code="html_motion_props")
    if not rows:
        raise LumiereError("Give at least one row of values.", code="html_motion_props")
    if not all(isinstance(r, dict) for r in rows):
        raise LumiereError("Every row must be an object of name: value.", code="html_motion_props")
    if len(rows) > MAX_VARIANTS:
        raise LumiereError(f"At most {MAX_VARIANTS} variants per call.", code="html_motion_props")
    return rows


def variants(store, svc, composition: str, data: Any, *, revision: int | None = None, name_field: str = "", render: bool = False,
             audio: bool = True) -> dict[str, Any]:
    """One new composition per row of ``data`` from a revision of ``composition``; each optionally queued as an MP4 export."""
    manifest = store.get(composition, revision)
    source = store.source(manifest)
    rows = rows_from(data)
    prepared = [apply(source, row) for row in rows]  # every row is checked before anything is saved
    made = []
    for index, (row, (html, merged)) in enumerate(zip(rows, prepared), 1):
        label = str(row.get("_name") or (row.get(name_field) if name_field else "") or index).strip()[:60]
        saved = store.save(html=html, name=f"{manifest['name']} · {label}", width=manifest["width"], height=manifest["height"],
                           fps=manifest["fps"], duration=manifest["duration"], project_id=manifest.get("project_id"),
                           sound=manifest.get("sound") or {})
        item = {"composition": saved["id"], "revision": saved["revision"], "name": saved["name"], "values": merged}
        if render:
            queued = store.queue_render(svc, saved["id"], saved["revision"], audio=audio)
            item["job"] = queued["job"]
        made.append(item)
    return {"template": {"composition": manifest["id"], "revision": manifest["revision"], "name": manifest["name"]},
            "variants": made, "count": len(made), "rendering": bool(render)}


__all__ = ["MAX_VARIANTS", "apply", "defaults", "props", "rows_from", "variants"]
