"""Direction style cards: a look written as fixed sections, with what may change in a variant.

A card is the written form of a style: its essence (and what it is not), materials, colour, type, motion, camera,
sound, native moves and pitfalls, plus a partition of those sections into what is safe to change in a variant, what
changes the character of the style, and what is its essence. Cards are imported from two open libraries of film
styles (the packaged copy lives in ``style_library/``; ``scripts/build_style_cards.py`` rebuilds it from a checkout)
and from any folder with the same layout (:func:`parse_lemo_folder`, :func:`parse_mg_folder`).

Nothing here draws or renders: the cards feed the style tools (generation ``studio_style_cards``, editing
``motion_styles``) and the direction flow of HTML animations (treatment, cues, cuecheck, readcheck).
"""
from __future__ import annotations

import json
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

LIBRARY_DIR = Path(__file__).with_name("style_library")

#: the fixed sections of a card, in reading order
SECTION_KEYS = ("essence", "materials", "color", "type", "motion", "camera", "sound", "moves", "pitfalls", "engine", "variation", "facts",
                "output", "route", "seed", "signature")

SECTION_LABELS = {
    "essence": "Essence, and what it is not", "materials": "Materials and rendering", "color": "Colour logic", "type": "Type and subtitles",
    "motion": "Motion quality", "camera": "Camera grammar", "sound": "Sound palette", "moves": "Native moves", "pitfalls": "Pitfalls of the medium",
    "engine": "Engine", "variation": "Variation space", "facts": "Sources and facts", "output": "Output", "route": "Technical route",
    "seed": "Creative seed", "signature": "Signature features and key techniques",
}

#: What a variant may touch. The first library says it itself ("Make a style your own"): colour, type, motion tempo,
#: the sound palette and one native move are safe; materials and the camera grammar change the character; the essence
#: (with its "what it is not") and the engine make a different style. The second one asks to keep the signature
#: features and to change the story, the brand, the copy, the palette and the size.
PARTITIONS = {
    "lemo-opuscar": {
        "safe": {"sections": ["color", "type", "motion", "sound", "moves"],
                 "note": "The style stays itself: the palette within its logic, type, tempo and how much moves, the sound palette, one native move added or swapped."},
        "character": {"sections": ["materials", "camera"],
                      "note": "Possible, but the style loses something with each change: its materials and rendering, the colour logic itself, the camera grammar."},
        "essence": {"sections": ["essence", "engine", "facts"],
                    "note": "That is a different style, not a version of this one: the essence with its 'what it is not', and the engine."},
        "free": {"sections": ["variation"], "note": "Story, structure, opening, ending, cast, pacing and voice are free in every film and need no variant."},
    },
    "mg-styles-15": {
        "safe": {"sections": ["seed", "output"], "note": "Change the story, the invented brand, the copy, the palette and the size (a vertical 1080x1920, for example)."},
        "character": {"sections": ["route"], "note": "A different technical route is allowed if it clearly raises quality; say why."},
        "essence": {"sections": ["signature"], "note": "Never remove the signature features: they are what makes the style recognisable."},
        "free": {"sections": [], "note": ""},
    },
}

SOURCES = {
    "lemo-opuscar": {
        "name": "Lemo-Opuscar", "url": "https://github.com/lemomo-ai/lemo-opuscar", "author": "LemoLab",
        "license": "MIT (code and style texts). The demo films and stills are CC BY 4.0 and are not included.",
        "attribution": "Style text by LemoLab (Lemo-Opuscar), MIT License. https://github.com/lemomo-ai/lemo-opuscar",
        "commit": "413a6bd6d78ebd3bdd5abaec9538a26867f890df", "lang": "en",
    },
    "mg-styles-15": {
        "name": "MG Styles 15", "url": "https://github.com/Vincentwei1021/mg-styles-15", "author": "Vincentwei1021",
        "license": "MIT (code, prompts and docs). The films and third-party files keep their own licences and are not included.",
        "attribution": "Style prompt from mg-styles-15 by Vincentwei1021, MIT License. https://github.com/Vincentwei1021/mg-styles-15",
        "commit": "49052d80bfaf9fa8412cc9710530e5e95a3b8d8c", "lang": "zh",
    },
}

_HEX = re.compile(r"#(?:[0-9a-fA-F]{6}|[0-9a-fA-F]{3})\b")


def clean_text(text: str) -> str:
    return re.sub(r"\n{3,}", "\n\n", str(text or "").replace("\r\n", "\n")).strip()


def classify(heading: str) -> str | None:
    """The fixed section a heading of a style file belongs to (None: no match, kept under its own name)."""
    h = re.sub(r"^\s*\d+\.\s*", "", heading).strip().lower()
    for key, words in (("essence", ("essence",)), ("variation", ("variation space",)), ("pitfalls", ("pitfall",)),
                       ("moves", ("native moves",)), ("sound", ("sound",)), ("facts", ("facts",)),
                       ("engine", ("engine",)), ("type", ("type", "caption card")),
                       ("color", ("colour", "color", "grade")), ("motion", ("motion",)),
                       ("camera", ("camera", "edit grammar", "editing grammar", "shot & layout")),
                       ("materials", ("material", "footage", "picture layer", "graphics", "frame and layout", "formats", "package parts"))):
        if any(w in h for w in words):
            return key
    return None


def palette_of(*texts: str, limit: int = 5) -> list[str]:
    """The distinct #hex colours a card names, in order of appearance (at most ``limit``)."""
    seen: list[str] = []
    for text in texts:
        for found in _HEX.findall(text or ""):
            value = found.upper()
            if len(value) == 4:
                value = "#" + "".join(c * 2 for c in value[1:])
            if value not in seen:
                seen.append(value)
    return seen[:limit]


def _split_sections(markdown: str) -> tuple[str, str, list[tuple[str, str]]]:
    """(title, summary, [(heading, body)]) of a style file: the H1, the leading block quote, then every ## section."""
    lines = markdown.replace("\r\n", "\n").split("\n")
    title, summary, sections = "", [], []
    current: Optional[list] = None
    for line in lines:
        if line.startswith("# ") and not title and current is None:
            title = line[2:].strip()
        elif line.startswith("## "):
            current = [line[3:].strip(), []]
            sections.append(current)
        elif current is None:
            if line.startswith(">"):
                summary.append(line.lstrip("> ").rstrip())
        else:
            current[1].append(line)
    return title, clean_text(" ".join(s for s in summary if s)), [(h, clean_text("\n".join(b))) for h, b in sections]


def _partition(source: str, present: set[str]) -> dict[str, Any]:
    out = {}
    for group, spec in PARTITIONS[source].items():
        sections = [s for s in spec["sections"] if s in present]
        out[group] = {"sections": sections, "note": spec["note"]}
    return out


def parse_lemo_style(style_md: str, style_json: dict[str, Any], slug: str) -> dict[str, Any]:
    """A card from a folder of the first library: ``STYLE.md`` in fixed sections plus ``style.json``."""
    meta = SOURCES["lemo-opuscar"]
    title, summary, parts = _split_sections(style_md)
    sections: dict[str, dict[str, str]] = {}
    extra: dict[str, dict[str, str]] = {}
    for heading, body in parts:
        key = classify(heading)
        clean_heading = re.sub(r"^\s*\d+\.\s*", "", heading).strip()
        if key is None:
            extra[re.sub(r"[^a-z0-9]+", "_", clean_heading.lower()).strip("_")[:40] or "other"] = {"title": clean_heading, "text": body}
        elif key in sections:
            sections[key]["text"] += "\n\n" + body
        else:
            sections[key] = {"title": clean_heading, "text": body}
    # the pointer to the demo's own notes at the foot of the file is not part of the style
    for key in list(sections):
        sections[key]["text"] = re.sub(r"\n+---\n+How our demo was made.*$", "", sections[key]["text"], flags=re.S).strip()
    name = style_json.get("en") or re.sub(r"\s*[—-]\s*Style Prompt\s*$", "", title) or slug
    colour_text = (sections.get("color") or {}).get("text", "")
    card = {
        "slug": slug, "source": "lemo-opuscar", "lang": meta["lang"], "name": name, "name_alt": style_json.get("cn") or "",
        "category": style_json.get("category_en") or "", "line": style_json.get("line") or "", "uses": list(style_json.get("uses") or []),
        "summary": summary, "sections": sections, "extra": extra,
        "meta": {**{k: style_json[k] for k in ("num", "film", "frame_sec", "dur", "size") if k in style_json},
                 "swatches": palette_of(colour_text, *(s["text"] for s in sections.values()))},
        "variants": _partition("lemo-opuscar", set(sections)),
        "palette": [], "technique": _short((sections.get("materials") or {}).get("text", "")),
        "pitfalls": _short((sections.get("pitfalls") or {}).get("text", ""), 1200),
        "license": meta["license"], "attribution": meta["attribution"], "source_url": f"{meta['url']}/tree/{meta['commit']}/styles/{slug}",
        "source_commit": meta["commit"],
    }
    return card


def _short(text: str, limit: int = 600) -> str:
    text = re.sub(r"\s+", " ", text or "").strip()
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


#: two prompts repeat their English name in the title line
MG_NAME_FIXES = {"hud": ("Cyberpunk HUD / FUI (Fictional UI)", "赛博朋克 HUD / FUI"), "isometric": ("Isometric / 2.5D", "等轴 2.5D")}

_MG_HEADINGS = (("style", "name"), ("output", "output"), ("suggested technical route", "route"), ("creative seed", "seed"),
                ("signature features", "signature"), ("how to work", "how"), ("rules", "rules"), ("craft checklist", "craft"),
                ("before you deliver", "score"), ("deliver", "deliver"))


def _mg_block(heading: str) -> str | None:
    h = heading.strip().lower()
    for prefix, key in _MG_HEADINGS:
        if h.startswith(prefix):
            return key
    return None


def parse_mg_prompt(markdown: str, filename: str) -> dict[str, Any]:
    """A card from a prompt of the second library: the fenced prompt in blocks (Style, Output, route, Creative seed,
    Signature features, ...). The common blocks (how to work, rules, craft checklist) are the same in every prompt and
    are kept once in the library header, not per card."""
    meta = SOURCES["mg-styles-15"]
    m = re.match(r"(\d+)-(.+)\.md$", filename)
    num, slug = (int(m.group(1)), m.group(2)) if m else (0, Path(filename).stem)
    heading = re.search(r"^# (.+)$", markdown, re.M)
    original = re.search(r"\*\*原片\*\*：(.+)", markdown)
    fence = re.search(r"```text\n(.*?)\n```", markdown, re.S)
    body = fence.group(1) if fence else markdown
    blocks: dict[str, list[str]] = {}
    key = None
    for line in body.split("\n"):
        if line.startswith("# "):
            key = _mg_block(line[2:])
            blocks.setdefault(key or "other", [])
            continue
        if key is not None:
            blocks[key].append(line)
    name_line = clean_text("\n".join(blocks.get("name", []))) or (heading.group(1) if heading else slug)
    english = re.search(r"([A-Za-z0-9][A-Za-z0-9 /&().,+:'-]*)$", name_line)
    name = english.group(1).strip() if english else name_line
    chinese = re.sub(r"[\s:：]+$", "", name_line.replace(name, "")).strip() if english else ""
    chinese = re.sub(r"\s+Retro$", "", chinese)
    name, chinese = MG_NAME_FIXES.get(slug, (name, chinese))
    sections = {}
    for key_, label in (("output", "Output (fixed)"), ("route", "Suggested technical route"), ("seed", "Creative seed"),
                        ("signature", "Signature features and key techniques")):
        text = clean_text("\n".join(blocks.get(key_, [])))
        if key_ == "signature":
            text = re.sub(r"\n\nYou are the director:.*$", "", text, flags=re.S).strip()
        if text:
            sections[key_] = {"title": label, "text": text}
    seed_text = (sections.get("seed") or {}).get("text", "")
    # "You are the director: you may change the concept ..." closes the seed in the prompt; it is a rule, not content
    sections["seed"]["text"] = re.sub(r"\n+You are the director:.*$", "", seed_text, flags=re.S).strip() if "seed" in sections else ""
    info = original.group(1).strip() if original else ""
    film = re.split(r"\s+·\s+", info)[0] if info else ""
    size = re.search(r"(\d{3,4}×\d{3,4})", info)
    fps = re.search(r"(\d+)\s*fps", info)
    seconds = re.search(r"(\d+)\s*秒", info)
    card = {
        "slug": slug, "source": "mg-styles-15", "lang": meta["lang"], "name": name or slug, "name_alt": chinese,
        "category": "Motion graphics", "line": film, "uses": [], "summary": _short((sections.get("signature") or {}).get("text", ""), 300),
        "sections": sections, "extra": {},
        "meta": {"num": num, "film": film, "size": size.group(1) if size else "", "fps": int(fps.group(1)) if fps else 0,
                 "dur": int(seconds.group(1)) if seconds else 0, "swatches": palette_of(seed_text)},
        "variants": _partition("mg-styles-15", set(sections)),
        "palette": [], "technique": _short((sections.get("route") or {}).get("text", "")),
        "pitfalls": "", "license": meta["license"], "attribution": meta["attribution"],
        "source_url": f"{meta['url']}/blob/{meta['commit']}/prompts/{filename}", "source_commit": meta["commit"],
    }
    common = {k: clean_text("\n".join(v)) for k, v in blocks.items() if k in ("how", "rules", "craft", "score", "deliver")}
    return {"card": card, "common": common}


def parse_lemo_folder(folder: str | Path) -> list[dict[str, Any]]:
    """Every style of a checkout of the first library (``styles/*/STYLE.md`` + ``style.json``; ``_template`` is skipped)."""
    base = Path(folder)
    root = base / "styles" if (base / "styles").is_dir() else base
    cards = []
    for sub in sorted(p for p in root.iterdir() if p.is_dir() and not p.name.startswith("_")):
        style_md, style_json = sub / "STYLE.md", sub / "style.json"
        if not style_md.is_file():
            continue
        info = json.loads(style_json.read_text(encoding="utf-8")) if style_json.is_file() else {}
        cards.append(parse_lemo_style(style_md.read_text(encoding="utf-8"), info, info.get("slug") or sub.name))
    return cards


def parse_mg_folder(folder: str | Path) -> tuple[list[dict[str, Any]], dict[str, str]]:
    """(cards, common blocks) of a checkout of the second library (``prompts/*.md``)."""
    base = Path(folder)
    root = base / "prompts" if (base / "prompts").is_dir() else base
    cards, common = [], {}
    for path in sorted(root.glob("*.md")):
        if not re.match(r"\d+-", path.name):
            continue
        parsed = parse_mg_prompt(path.read_text(encoding="utf-8"), path.name)
        cards.append(parsed["card"])
        common = common or parsed["common"]
    return cards, common


# --------------------------------------------------------------------------------------------- the packaged library

def library_path(name: str) -> Path:
    return LIBRARY_DIR / name


@lru_cache(maxsize=1)
def load_library() -> dict[str, Any]:
    """The packaged cards: ``{"version", "sources", "common", "cards": [...]}`` (empty when the files are missing)."""
    cards: list[dict[str, Any]] = []
    common: dict[str, Any] = {}
    for file in sorted(LIBRARY_DIR.glob("*.json")):
        data = json.loads(file.read_text(encoding="utf-8"))
        cards.extend(data.get("cards", []))
        if data.get("common"):
            common[data["source"]] = data["common"]
    cards.sort(key=lambda c: (c["source"], c.get("category", ""), c["name"].lower()))
    version = "|".join(f"{s}:{SOURCES[s]['commit'][:10]}:{sum(1 for c in cards if c['source'] == s)}" for s in sorted({c['source'] for c in cards}))
    return {"version": version, "sources": SOURCES, "common": common, "cards": cards}


def card_id(card: dict[str, Any]) -> str:
    return f"{card['source']}/{card['slug']}"


def find(ref: str, source: str | None = None) -> dict[str, Any] | None:
    """A packaged card by ``source/slug``, by slug or by name (any case)."""
    ref = str(ref or "").strip().lower()
    if not ref:
        return None
    for card in load_library()["cards"]:
        if source and card["source"] != source:
            continue
        if ref in (card_id(card).lower(), card["slug"].lower(), card["name"].lower(), (card.get("name_alt") or "").lower()):
            return card
    return None


def search(query: str = "", *, source: str | None = None, category: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
    """Cards whose name, category, line, use, summary or sections mention every word of the query (best matches first)."""
    words = [w for w in re.split(r"\s+", (query or "").lower().strip()) if w]
    scored: list[tuple[int, dict[str, Any]]] = []
    for card in load_library()["cards"]:
        if source and card["source"] != source:
            continue
        if category and category.lower() not in (card.get("category") or "").lower():
            continue
        head = " ".join([card["name"], card.get("name_alt", ""), card.get("category", ""), card.get("line", ""), " ".join(card.get("uses", [])), card["slug"]]).lower()
        body = " ".join(s["text"] for s in card["sections"].values()).lower() if words else ""
        score, ok = 0, True
        for w in words:
            if w in head:
                score += 10 if w in card["name"].lower() else 4
            elif w in body:
                score += 1
            else:
                ok = False
                break
        if ok:
            scored.append((score, card))
    scored.sort(key=lambda item: (-item[0], item[1]["source"], item[1]["name"].lower()))
    return [c for _, c in scored[:limit]]


def brief(card: dict[str, Any]) -> dict[str, Any]:
    """The listing form of a card: who it is, what it is for and its palette, without the sections."""
    return {"id": card_id(card), "slug": card["slug"], "source": card["source"], "name": card["name"], "name_alt": card.get("name_alt", ""),
            "category": card.get("category", ""), "line": card.get("line", ""), "uses": card.get("uses", []), "palette": card.get("palette", []),
            "lang": card.get("lang", "en"), "sections": [k for k in SECTION_KEYS if k in card["sections"]], "license": card["license"]}


def classify_change(card: dict[str, Any], sections: list[str]) -> dict[str, Any]:
    """Which part of the partition each changed section falls in, and what that means for a variant."""
    part = card.get("variants") or {}
    where: dict[str, str] = {}
    for key in sections:
        for group in ("safe", "character", "essence", "free"):
            if key in (part.get(group) or {}).get("sections", []):
                where[key] = group
                break
        else:
            where[key] = "unlisted"
    return {"by_section": where, "safe": [k for k, g in where.items() if g in ("safe", "free")],
            "changes_character": [k for k, g in where.items() if g == "character"],
            "breaks_essence": [k for k, g in where.items() if g == "essence"], "unlisted": [k for k, g in where.items() if g == "unlisted"]}
