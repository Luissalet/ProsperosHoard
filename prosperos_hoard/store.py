"""Data access layer: every read/write to SQLite goes through here.

Kept free of FastAPI imports so it can be unit tested directly and reused
by the MCP adapter's own tests. Every ``list_*`` method returns a small
number of items by default and marks ``has_more`` / ``next_offset`` per the
contract's "compact tool outputs" rule.
"""

from __future__ import annotations

import re
import sqlite3
from pathlib import Path
from typing import Any, Iterable, Optional

from .db import connect, dumps, loads, row_to_dict
from .ids import new_id
from .util import now_iso


_HEX_RE = re.compile(r"^#[0-9a-fA-F]{6}([0-9a-fA-F]{2})?$")


class NotFound(KeyError):
    def __init__(self, kind: str, id_: str):
        super().__init__(f"{kind} not found: {id_}")
        self.kind = kind
        self.id = id_

    def __str__(self) -> str:  # KeyError would wrap the message in quotes
        return f"{self.kind} not found: {self.id}"


BUILTIN_STYLE_PRESETS = [
    dict(
        name="Studio portrait",
        prompt_prefix="studio portrait photography, softbox lighting, shallow depth of field,",
        prompt_suffix=", sharp focus, high detail skin texture",
        negative="blurry, deformed, extra fingers, watermark, text",
        defaults=dict(checkpoint="sd_xl_base_1.0.safetensors", width=1024, height=1024, steps=30, cfg=6.5, sampler="dpmpp_2m", scheduler="karras"),
    ),
    dict(
        name="Film still 35mm",
        prompt_prefix="35mm film still, cinematic lighting, kodak portra colour grade,",
        prompt_suffix=", subtle film grain, anamorphic bokeh",
        negative="digital artifacts, oversharpened, watermark, text",
        defaults=dict(checkpoint="sd_xl_base_1.0.safetensors", width=1152, height=896, steps=32, cfg=6.0, sampler="dpmpp_2m", scheduler="karras"),
    ),
    dict(
        name="Anime cel",
        prompt_prefix="anime cel shading, clean line art, vibrant flat colours,",
        prompt_suffix=", studio quality key visual",
        negative="photorealistic, blurry lines, watermark, text",
        defaults=dict(checkpoint="sd_xl_base_1.0.safetensors", width=1024, height=1024, steps=28, cfg=7.0, sampler="euler_ancestral", scheduler="normal"),
    ),
    dict(
        name="Pastel dream",
        prompt_prefix="soft pastel colour palette, dreamy diffused light, gentle gradients,",
        prompt_suffix=", airy and delicate atmosphere",
        negative="high contrast, harsh shadows, watermark, text",
        defaults=dict(checkpoint="sd_xl_base_1.0.safetensors", width=1024, height=1024, steps=28, cfg=6.0, sampler="dpmpp_2m", scheduler="karras"),
    ),
    dict(
        name="Neon night city",
        prompt_prefix="neon-lit night city, cyberpunk colour grade, wet reflective streets,",
        prompt_suffix=", glowing signage, moody atmosphere",
        negative="daylight, flat lighting, watermark, text",
        defaults=dict(checkpoint="sd_xl_base_1.0.safetensors", width=1024, height=1024, steps=32, cfg=7.0, sampler="dpmpp_2m", scheduler="karras"),
    ),
    dict(
        name="Album art minimal",
        prompt_prefix="minimalist album cover art, bold negative space, striking single subject,",
        prompt_suffix=", graphic design composition",
        negative="cluttered, busy background, watermark, text",
        defaults=dict(checkpoint="sd_xl_base_1.0.safetensors", width=1024, height=1024, steps=30, cfg=6.5, sampler="dpmpp_2m", scheduler="karras"),
    ),
]


class Store:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        connect(self.data_dir)  # create the schema up front
        self.assets_dir = self.data_dir / "assets"
        self.thumbs_dir = self.data_dir / "thumbs"
        self.projects_dir = self.data_dir / "projects"
        for d in (self.assets_dir, self.thumbs_dir, self.projects_dir):
            d.mkdir(parents=True, exist_ok=True)
        self._seed_builtin_presets()

    @property
    def conn(self) -> sqlite3.Connection:
        """The calling thread's own connection. The API handlers (a thread
        pool) and the two job workers each get theirs: one sqlite3
        connection shared between threads interleaves transactions."""
        return connect(self.data_dir)

    # ---------------------------------------------------------------- misc
    def _seed_builtin_presets(self) -> None:
        """Insert the built-in presets, and refresh their text/defaults on
        databases created by an older version (a built-in preset is not
        user-editable, so overwriting it loses nothing)."""
        for preset in BUILTIN_STYLE_PRESETS:
            exists = self.conn.execute(
                "SELECT id FROM style_presets WHERE name=? AND is_builtin=1", (preset["name"],)
            ).fetchone()
            if exists:
                self.conn.execute(
                    "UPDATE style_presets SET prompt_prefix=?, prompt_suffix=?, negative=?, defaults_json=? WHERE id=?",
                    (preset["prompt_prefix"], preset["prompt_suffix"], preset["negative"], dumps(preset["defaults"]), exists["id"]),
                )
                continue
            self.conn.execute(
                """INSERT INTO style_presets
                   (id, project_id, name, prompt_prefix, prompt_suffix, negative,
                    defaults_json, notes, is_builtin, created_at)
                   VALUES (?, NULL, ?, ?, ?, ?, ?, ?, 1, ?)""",
                (
                    new_id("sp"), preset["name"], preset["prompt_prefix"], preset["prompt_suffix"],
                    preset["negative"], dumps(preset["defaults"]), preset.get("notes", ""), now_iso(),
                ),
            )
        self.conn.commit()

    def path_for_asset_file(self, asset_id: str, ext: str) -> Path:
        return self.assets_dir / f"{asset_id}{ext}"

    def path_for_thumb(self, asset_id: str) -> Path:
        return self.thumbs_dir / f"{asset_id}.webp"

    # ------------------------------------------------------------ projects
    def create_project(self, name: str, brief: str | None = None) -> dict[str, Any]:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("a project needs a non-empty name")
        name = name.strip()[:120]
        pid = new_id("proj")
        ts = now_iso()
        self.conn.execute(
            "INSERT INTO projects (id, name, brief, cover_asset_id, created_at, updated_at)"
            " VALUES (?, ?, ?, NULL, ?, ?)",
            (pid, name, brief, ts, ts),
        )
        (self.projects_dir / pid).mkdir(parents=True, exist_ok=True)
        self.conn.commit()
        return self.get_project(pid)

    def get_project(self, project_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
        if not row:
            raise NotFound("project", project_id)
        d = row_to_dict(row)
        d["counts"] = self._project_counts(project_id)
        return d

    def _project_counts(self, project_id: str) -> dict[str, int]:
        row = self.conn.execute(
            "SELECT "
            "(SELECT COUNT(*) FROM assets WHERE project_id=?) assets, "
            "(SELECT COUNT(*) FROM assets WHERE project_id=? AND kind='image') images, "
            "(SELECT COUNT(*) FROM assets WHERE project_id=? AND kind='video') videos, "
            "(SELECT COUNT(*) FROM assets WHERE project_id=? AND kind='audio') audio, "
            "(SELECT COUNT(*) FROM characters WHERE project_id=?) characters, "
            "(SELECT COUNT(*) FROM groups WHERE project_id=?) groups, "
            "(SELECT COUNT(*) FROM timelines WHERE project_id=?) timelines",
            (project_id,) * 7,
        ).fetchone()
        return row_to_dict(row)

    def update_project(self, project_id: str, name: str | None = None, brief: str | None = None,
                       cover_asset_id: str | None = None, image_engine: str | None = None) -> dict[str, Any]:
        self.get_project(project_id)
        if name is not None:
            if not name.strip():
                raise ValueError("project name cannot be empty")
            self.conn.execute("UPDATE projects SET name=? WHERE id=?", (name.strip()[:120], project_id))
        if brief is not None:
            self.conn.execute("UPDATE projects SET brief=? WHERE id=?", (brief[:4000], project_id))
        if cover_asset_id is not None:
            self.get_asset(cover_asset_id)
            self.conn.execute("UPDATE projects SET cover_asset_id=? WHERE id=?", (cover_asset_id, project_id))
        if image_engine is not None:
            from .engine import IMAGE_ENGINES  # local import: engine.py imports Store at module level

            if image_engine not in IMAGE_ENGINES:
                raise ValueError(f"image_engine must be one of {', '.join(IMAGE_ENGINES)}")
            self.conn.execute("UPDATE projects SET image_engine=? WHERE id=?", (image_engine, project_id))
        self.conn.commit()
        self.touch_project(project_id)
        return self.get_project(project_id)

    def list_projects(self, query: str | None = None, limit: int = 10, offset: int = 0) -> dict[str, Any]:
        limit = max(1, min(limit, 50))
        sql = "SELECT * FROM projects"
        params: list[Any] = []
        if query:
            sql += " WHERE name LIKE ? OR brief LIKE ?"
            like = f"%{query}%"
            params += [like, like]
        sql += " ORDER BY updated_at DESC LIMIT ? OFFSET ?"
        params += [limit + 1, offset]
        rows = [row_to_dict(r) for r in self.conn.execute(sql, params).fetchall()]
        has_more = len(rows) > limit
        rows = rows[:limit]
        for p in rows:
            p["counts"] = self._project_counts(p["id"])
        return {"items": rows, "has_more": has_more, "next_offset": offset + limit if has_more else None}

    def touch_project(self, project_id: str) -> None:
        self.conn.execute(
            "UPDATE projects SET updated_at=? WHERE id=?", (now_iso(), project_id)
        )
        self.conn.commit()

    # -------------------------------------------------------------- assets
    def create_asset(
        self,
        project_id: str,
        kind: str,
        file_path: str,
        mime: str | None = None,
        width: int | None = None,
        height: int | None = None,
        duration_s: float | None = None,
        thumb_path: str | None = None,
        waveform: list[float] | None = None,
        tags: list[str] | None = None,
        source: str = "import",
        notes: str | None = None,
        recipe: dict[str, Any] | None = None,
        asset_id: str | None = None,
        name: str | None = None,
    ) -> dict[str, Any]:
        self.get_project(project_id)
        aid = asset_id or new_id("a")
        self.conn.execute(
            """INSERT INTO assets
               (id, project_id, kind, file_path, mime, width, height, duration_s,
                thumb_path, waveform_json, tags_json, rating, favourite, notes,
                source, recipe_json, created_at, name)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,0,0,?,?,?,?,?)""",
            (
                aid, project_id, kind, file_path, mime, width, height, duration_s,
                thumb_path, dumps(waveform) if waveform is not None else None,
                dumps(tags or []), notes, source,
                dumps(recipe) if recipe is not None else None, now_iso(), (name or "")[:200] or None,
            ),
        )
        self.conn.commit()
        self.touch_project(project_id)
        return self.get_asset(aid)

    def get_asset(self, asset_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
        if not row:
            raise NotFound("asset", asset_id)
        d = row_to_dict(row)
        d["tags"] = loads(d.pop("tags_json"), [])
        d["waveform"] = loads(d.pop("waveform_json"), None)
        d["recipe"] = loads(d.pop("recipe_json"), None)
        d["analysis"] = loads(d.pop("analysis_json", None), None)
        d["favourite"] = bool(d["favourite"])
        return d

    def set_asset_media(self, asset_id: str, *, waveform: list[float] | None = None, analysis: dict[str, Any] | None = None,
                        duration_s: float | None = None, width: int | None = None, height: int | None = None,
                        thumb_path: str | None = None) -> dict[str, Any]:
        self.get_asset(asset_id)
        cols, params = [], []
        for col, value in (("waveform_json", dumps(waveform) if waveform is not None else None),
                           ("analysis_json", dumps(analysis) if analysis is not None else None),
                           ("duration_s", duration_s), ("width", width), ("height", height), ("thumb_path", thumb_path)):
            if value is not None:
                cols.append(f"{col}=?")
                params.append(value)
        if cols:
            params.append(asset_id)
            self.conn.execute(f"UPDATE assets SET {', '.join(cols)} WHERE id=?", params)
            self.conn.commit()
        return self.get_asset(asset_id)

    def update_asset(self, asset_id: str, **fields: Any) -> dict[str, Any]:
        self.get_asset(asset_id)
        if fields.get("rating") is not None and not (isinstance(fields["rating"], int) and 0 <= fields["rating"] <= 5):
            raise ValueError("rating must be an integer from 0 to 5")
        if fields.get("tags") is not None:
            tags = fields["tags"]
            if not isinstance(tags, list) or len(tags) > 30 or not all(isinstance(t, str) and 0 < len(t.strip()) <= 40 for t in tags):
                raise ValueError("tags must be a list of at most 30 non-empty strings of up to 40 characters")
            fields["tags"] = sorted({t.strip().lower() for t in tags})
        if fields.get("name") is not None:
            fields["name"] = str(fields["name"]).strip()[:200]
        cols, params = [], []
        for key in ("tags", "rating", "favourite", "notes", "name"):
            if key in fields and fields[key] is not None:
                if key == "tags":
                    cols.append("tags_json=?")
                    params.append(dumps(fields[key]))
                elif key == "favourite":
                    cols.append("favourite=?")
                    params.append(1 if fields[key] else 0)
                else:
                    cols.append(f"{key}=?")
                    params.append(fields[key])
        if cols:
            params.append(asset_id)
            self.conn.execute(f"UPDATE assets SET {', '.join(cols)} WHERE id=?", params)
            self.conn.commit()
        return self.get_asset(asset_id)

    def list_assets(
        self,
        project_id: str | None = None,
        kind: str | None = None,
        query: str | None = None,
        tag: str | None = None,
        favourite: bool | None = None,
        limit: int = 12,
        offset: int = 0,
        source: str | None = None,
        min_rating: int | None = None,
        exclude_sources: tuple[str, ...] = (),
    ) -> dict[str, Any]:
        limit = max(1, min(int(limit), 60))
        offset = max(0, int(offset))
        sql = "SELECT id FROM assets WHERE 1=1"
        params: list[Any] = []
        if project_id:
            sql += " AND project_id=?"
            params.append(project_id)
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        if source:
            sql += " AND source=?"
            params.append(source)
        for src in exclude_sources:
            sql += " AND source<>?"
            params.append(src)
        if query:
            sql += " AND (name LIKE ? ESCAPE '\\' OR notes LIKE ? ESCAPE '\\' OR tags_json LIKE ? ESCAPE '\\' OR recipe_json LIKE ? ESCAPE '\\')"
            like = "%" + query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            params += [like, like, like, like]
        if tag:
            sql += " AND tags_json LIKE ?"
            params.append(f'%"{tag.strip().lower()}"%')
        if favourite:
            sql += " AND favourite=1"
        if min_rating:
            sql += " AND rating>=?"
            params.append(int(min_rating))
        sql += " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
        params += [limit + 1, offset]
        rows = self.conn.execute(sql, params).fetchall()
        items = [self.get_asset(r["id"]) for r in rows[:limit]]
        has_more = len(rows) > limit
        return {"items": items, "has_more": has_more, "next_offset": offset + limit if has_more else None}

    # --------------------------------------------------------- characters
    def create_character(self, project_id: str, name: str, **fields: Any) -> dict[str, Any]:
        self.get_project(project_id)
        if not isinstance(name, str) or not name.strip():
            raise ValueError("a character needs a non-empty name")
        name = name.strip()[:80]
        if any(c["name"].lower() == name.lower() for c in self.list_characters(project_id)):
            raise ValueError(f"this project already has a character called '{name}'; @mentions need unique names")
        self._check_character_fields(fields)
        cid = new_id("char")
        ts = now_iso()
        self.conn.execute(
            """INSERT INTO characters
               (id, project_id, name, role, bio, prompt, negative, palette_json,
                reference_asset_ids_json, canonical_asset_id, voice_json, notes,
                created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                cid, project_id, name,
                fields.get("role"), fields.get("bio"), fields.get("prompt"),
                fields.get("negative"), dumps(fields.get("palette", [])),
                dumps(fields.get("reference_asset_ids", [])),
                fields.get("canonical_asset_id"),
                dumps(fields.get("voice")) if fields.get("voice") else None,
                fields.get("notes"), ts, ts,
            ),
        )
        self.conn.commit()
        return self.get_character(cid)

    def get_character(self, character_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM characters WHERE id=?", (character_id,)).fetchone()
        if not row:
            raise NotFound("character", character_id)
        d = row_to_dict(row)
        d["palette"] = loads(d.pop("palette_json"), [])
        d["reference_asset_ids"] = loads(d.pop("reference_asset_ids_json"), [])
        d["voice"] = loads(d.pop("voice_json"), None)
        return d

    def _check_character_fields(self, fields: dict[str, Any]) -> None:
        allowed = {"role", "bio", "prompt", "negative", "palette", "reference_asset_ids", "canonical_asset_id", "voice", "notes", "name"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown character field(s): {', '.join(sorted(unknown))}; allowed: {', '.join(sorted(allowed))}")
        palette = fields.get("palette")
        if palette is not None:
            if not isinstance(palette, list) or len(palette) > 12 or not all(isinstance(c, str) and _HEX_RE.match(c) for c in palette):
                raise ValueError("palette must be a list of up to 12 hex colours like '#ff4d8d'")
        if fields.get("canonical_asset_id"):
            asset = self.get_asset(fields["canonical_asset_id"])
            if asset["kind"] != "image":
                raise ValueError("canonical_asset_id must be an image asset")
        for aid in fields.get("reference_asset_ids") or []:
            self.get_asset(aid)
        voice = fields.get("voice")
        if voice is not None and not isinstance(voice, dict):
            raise ValueError("voice must be an object like {'backend': 'piper', 'voice_id': 'es_ES-davefx-medium', 'speed': 1.0}")

    def update_character(self, character_id: str, **fields: Any) -> dict[str, Any]:
        current = self.get_character(character_id)
        self._check_character_fields(fields)
        if fields.get("name") is not None:
            new_name = str(fields["name"]).strip()[:80]
            if not new_name:
                raise ValueError("a character name cannot be empty")
            if any(c["name"].lower() == new_name.lower() and c["id"] != character_id
                   for c in self.list_characters(current["project_id"])):
                raise ValueError(f"this project already has a character called '{new_name}'")
            fields["name"] = new_name
        simple = {"name", "role", "bio", "prompt", "negative", "notes", "canonical_asset_id"}
        cols, params = [], []
        for key in simple:
            if key in fields and fields[key] is not None:
                cols.append(f"{key}=?")
                params.append(fields[key])
        for key, col in (("palette", "palette_json"), ("reference_asset_ids", "reference_asset_ids_json")):
            if key in fields and fields[key] is not None:
                cols.append(f"{col}=?")
                params.append(dumps(fields[key]))
        if "voice" in fields and fields["voice"] is not None:
            cols.append("voice_json=?")
            params.append(dumps(fields["voice"]))
        cols.append("updated_at=?")
        params.append(now_iso())
        params.append(character_id)
        self.conn.execute(f"UPDATE characters SET {', '.join(cols)} WHERE id=?", params)
        self.conn.commit()
        return self.get_character(character_id)

    def list_characters(self, project_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT id FROM characters WHERE project_id=? ORDER BY created_at ASC", (project_id,)
        ).fetchall()
        return [self.get_character(r["id"]) for r in rows]

    # -------------------------------------------------------------- groups
    def _check_group_fields(self, project_id: str, fields: dict[str, Any]) -> None:
        allowed = {"concept", "member_ids", "logo_asset_id", "colours", "name"}
        unknown = set(fields) - allowed
        if unknown:
            raise ValueError(f"unknown group field(s): {', '.join(sorted(unknown))}; allowed: {', '.join(sorted(allowed))}")
        for cid in fields.get("member_ids") or []:
            char = self.get_character(cid)
            if char["project_id"] != project_id:
                raise ValueError(f"character {cid} belongs to another project")
        if len(set(fields.get("member_ids") or [])) != len(fields.get("member_ids") or []):
            raise ValueError("member_ids lists a character twice")
        colours = fields.get("colours")
        if colours is not None and (not isinstance(colours, list) or not all(isinstance(c, str) and _HEX_RE.match(c) for c in colours)):
            raise ValueError("colours must be a list of hex colours like '#ff4d8d'")
        if fields.get("logo_asset_id"):
            if self.get_asset(fields["logo_asset_id"])["kind"] != "image":
                raise ValueError("logo_asset_id must be an image asset")

    def create_group(self, project_id: str, name: str, **fields: Any) -> dict[str, Any]:
        self.get_project(project_id)
        if not isinstance(name, str) or not name.strip():
            raise ValueError("a group needs a non-empty name")
        name = name.strip()[:80]
        self._check_group_fields(project_id, fields)
        gid = new_id("grp")
        ts = now_iso()
        self.conn.execute(
            """INSERT INTO groups (id, project_id, name, concept, member_ids_json,
                logo_asset_id, colours_json, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (
                gid, project_id, name, fields.get("concept"),
                dumps(fields.get("member_ids", [])), fields.get("logo_asset_id"),
                dumps(fields.get("colours", [])), ts, ts,
            ),
        )
        self.conn.commit()
        return self.get_group(gid)

    def get_group(self, group_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM groups WHERE id=?", (group_id,)).fetchone()
        if not row:
            raise NotFound("group", group_id)
        d = row_to_dict(row)
        d["member_ids"] = loads(d.pop("member_ids_json"), [])
        d["colours"] = loads(d.pop("colours_json"), [])
        return d

    def update_group(self, group_id: str, **fields: Any) -> dict[str, Any]:
        group = self.get_group(group_id)
        self._check_group_fields(group["project_id"], fields)
        cols, params = [], []
        for key in ("name", "concept", "logo_asset_id"):
            if key in fields and fields[key] is not None:
                cols.append(f"{key}=?")
                params.append(fields[key])
        for key, col in (("member_ids", "member_ids_json"), ("colours", "colours_json")):
            if key in fields and fields[key] is not None:
                cols.append(f"{col}=?")
                params.append(dumps(fields[key]))
        cols.append("updated_at=?")
        params.append(now_iso())
        params.append(group_id)
        self.conn.execute(f"UPDATE groups SET {', '.join(cols)} WHERE id=?", params)
        self.conn.commit()
        return self.get_group(group_id)

    def list_groups(self, project_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT id FROM groups WHERE project_id=? ORDER BY created_at ASC", (project_id,)
        ).fetchall()
        return [self.get_group(r["id"]) for r in rows]

    # -------------------------------------------------------- style presets
    def list_style_presets(self, project_id: str | None = None) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM style_presets WHERE is_builtin=1 OR project_id=? ORDER BY is_builtin DESC, created_at ASC",
            (project_id,),
        ).fetchall()
        out = []
        for r in rows:
            d = row_to_dict(r)
            d["defaults"] = loads(d.pop("defaults_json"), {})
            out.append(d)
        return out

    def get_style_preset(self, preset_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM style_presets WHERE id=?", (preset_id,)).fetchone()
        if not row:
            raise NotFound("style_preset", preset_id)
        d = row_to_dict(row)
        d["defaults"] = loads(d.pop("defaults_json"), {})
        return d

    # -------------------------------------------------------------- boards
    def create_board(self, project_id: str, name: str, kind: str = "moodboard") -> dict[str, Any]:
        self.get_project(project_id)
        if not isinstance(name, str) or not name.strip():
            raise ValueError("a board needs a name")
        if kind not in ("moodboard", "storyboard", "shotlist"):
            raise ValueError("board kind must be moodboard, storyboard or shotlist")
        name = name.strip()[:80]
        bid = new_id("board")
        ts = now_iso()
        self.conn.execute(
            "INSERT INTO boards (id, project_id, name, kind, items_json, created_at, updated_at)"
            " VALUES (?,?,?,?,'[]',?,?)",
            (bid, project_id, name, kind, ts, ts),
        )
        self.conn.commit()
        return self.get_board(bid)

    def get_board(self, board_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM boards WHERE id=?", (board_id,)).fetchone()
        if not row:
            raise NotFound("board", board_id)
        d = row_to_dict(row)
        d["items"] = loads(d.pop("items_json"), [])
        return d

    def update_board(self, board_id: str, name: str | None = None, kind: str | None = None) -> dict[str, Any]:
        self.get_board(board_id)
        if name is not None:
            if not name.strip():
                raise ValueError("a board needs a name")
            self.conn.execute("UPDATE boards SET name=?, updated_at=? WHERE id=?", (name.strip()[:80], now_iso(), board_id))
        if kind is not None:
            if kind not in ("moodboard", "storyboard", "shotlist"):
                raise ValueError("board kind must be moodboard, storyboard or shotlist")
            self.conn.execute("UPDATE boards SET kind=?, updated_at=? WHERE id=?", (kind, now_iso(), board_id))
        self.conn.commit()
        return self.get_board(board_id)

    def delete_board(self, board_id: str) -> None:
        self.get_board(board_id)
        self.conn.execute("DELETE FROM boards WHERE id=?", (board_id,))
        self.conn.commit()

    def update_board_items(self, board_id: str, items: list[dict[str, Any]]) -> dict[str, Any]:
        board = self.get_board(board_id)
        if not isinstance(items, list) or len(items) > 500:
            raise ValueError("items must be a list of at most 500 {asset_id, note} objects")
        clean = []
        for item in items:
            if not isinstance(item, dict) or not isinstance(item.get("asset_id"), str):
                raise ValueError("every board item needs an 'asset_id'")
            asset = self.get_asset(item["asset_id"])
            if asset["project_id"] != board["project_id"]:
                raise ValueError(f"asset {asset['id']} belongs to another project")
            clean.append({"asset_id": asset["id"], "note": str(item.get("note") or "")[:500]})
        items = clean
        self.conn.execute(
            "UPDATE boards SET items_json=?, updated_at=? WHERE id=?",
            (dumps(items), now_iso(), board_id),
        )
        self.conn.commit()
        return self.get_board(board_id)

    def list_boards(self, project_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT id FROM boards WHERE project_id=? ORDER BY created_at ASC", (project_id,)
        ).fetchall()
        return [self.get_board(r["id"]) for r in rows]

    # ----------------------------------------------------------- timelines
    def create_timeline(self, project_id: str, name: str, aspect: str = "9:16", fps: int = 30, **fields: Any) -> dict[str, Any]:
        tid = new_id("tl")
        ts = now_iso()
        wh = {"9:16": (1080, 1920), "16:9": (1920, 1080), "1:1": (1080, 1080)}.get(aspect, (1080, 1920))
        width = fields.get("width", wh[0])
        height = fields.get("height", wh[1])
        self.conn.execute(
            """INSERT INTO timelines (id, project_id, name, aspect, fps, width, height,
                audio_asset_id, tracks_json, finishing_json, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                tid, project_id, name, aspect, fps, width, height,
                fields.get("audio_asset_id"), dumps(fields.get("tracks", [])),
                dumps(fields.get("finishing", {})), ts, ts,
            ),
        )
        self.conn.commit()
        return self.get_timeline(tid)

    def get_timeline(self, timeline_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM timelines WHERE id=?", (timeline_id,)).fetchone()
        if not row:
            raise NotFound("timeline", timeline_id)
        d = row_to_dict(row)
        d["tracks"] = loads(d.pop("tracks_json"), [])
        d["finishing"] = loads(d.pop("finishing_json", None), {})
        return d

    def update_timeline(self, timeline_id: str, **fields: Any) -> dict[str, Any]:
        self.get_timeline(timeline_id)
        cols, params = [], []
        for key in ("name", "aspect", "fps", "width", "height", "audio_asset_id"):
            if key in fields and fields[key] is not None:
                cols.append(f"{key}=?")
                params.append(fields[key])
        if "tracks" in fields and fields["tracks"] is not None:
            cols.append("tracks_json=?")
            params.append(dumps(fields["tracks"]))
        if "finishing" in fields and fields["finishing"] is not None:
            cols.append("finishing_json=?")
            params.append(dumps(fields["finishing"]))
        cols.append("updated_at=?")
        params.append(now_iso())
        params.append(timeline_id)
        self.conn.execute(f"UPDATE timelines SET {', '.join(cols)} WHERE id=?", params)
        self.conn.commit()
        return self.get_timeline(timeline_id)

    def list_timelines(self, project_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT id FROM timelines WHERE project_id=? ORDER BY created_at DESC", (project_id,)
        ).fetchall()
        return [self.get_timeline(r["id"]) for r in rows]

    # ---------------------------------------------------------------- jobs
    def create_job(self, type_: str, lane: str, params: dict[str, Any], inputs: dict[str, Any] | None = None,
                    project_id: str | None = None, state: str = "queued") -> dict[str, Any]:
        jid = new_id("job")
        ts = now_iso()
        self.conn.execute(
            """INSERT INTO jobs (id, project_id, type, lane, params_json, inputs_json,
                state, progress, message, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,0.0,NULL,?,?)""",
            (jid, project_id, type_, lane, dumps(params), dumps(inputs or {}), state, ts, ts),
        )
        self.conn.commit()
        return self.get_job(jid)

    def get_job(self, job_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone()
        if not row:
            raise NotFound("job", job_id)
        d = row_to_dict(row)
        d["params"] = loads(d.pop("params_json"), {})
        d["inputs"] = loads(d.pop("inputs_json"), {})
        d["outputs"] = loads(d.pop("outputs_json"), None)
        d["cancel_requested"] = bool(d.get("cancel_requested"))
        return d

    def list_jobs(self, state: str | None = None, limit: int = 10, offset: int = 0,
                  project_id: str | None = None) -> dict[str, Any]:
        limit = max(1, min(int(limit), 50))
        offset = max(0, int(offset))
        states = ("queued", "waiting_gpu", "running", "done", "failed", "cancelled", "active")
        if state and state not in states:
            raise ValueError(f"unknown job state '{state}'; use one of {', '.join(states)}")
        sql = "SELECT id FROM jobs WHERE 1=1"
        params: list[Any] = []
        if state == "active":
            sql += " AND state IN ('queued','waiting_gpu','running')"
        elif state:
            sql += " AND state=?"
            params.append(state)
        if project_id:
            sql += " AND project_id=?"
            params.append(project_id)
        sql += " ORDER BY created_at DESC, id DESC LIMIT ? OFFSET ?"
        params += [limit + 1, offset]
        rows = self.conn.execute(sql, params).fetchall()
        items = [self.get_job(r["id"]) for r in rows[:limit]]
        has_more = len(rows) > limit
        return {"items": items, "has_more": has_more, "next_offset": offset + limit if has_more else None}

    def update_job(self, job_id: str, **fields: Any) -> dict[str, Any]:
        self.get_job(job_id)
        cols, params = [], []
        for key in ("state", "progress", "message", "backend", "log_excerpt", "started_at", "finished_at", "cancel_requested"):
            if key in fields and fields[key] is not None:
                cols.append(f"{key}=?")
                params.append(fields[key])
        if "outputs" in fields and fields["outputs"] is not None:
            cols.append("outputs_json=?")
            params.append(dumps(fields["outputs"]))
        cols.append("updated_at=?")
        params.append(now_iso())
        params.append(job_id)
        self.conn.execute(f"UPDATE jobs SET {', '.join(cols)} WHERE id=?", params)
        self.conn.commit()
        return self.get_job(job_id)

    def requeue_running_jobs(self) -> int:
        """On boot: any job left 'running' or 'waiting_gpu' after a restart goes back to queued."""
        cur = self.conn.execute(
            "UPDATE jobs SET state='queued', progress=0.0, message='requeued after restart', cancel_requested=0, updated_at=? "
            "WHERE state IN ('running','waiting_gpu')",
            (now_iso(),),
        )
        self.conn.commit()
        return cur.rowcount

    def request_cancel(self, job_id: str) -> dict[str, Any]:
        """Queued/waiting jobs are cancelled at once; a running job is
        flagged and its handler stops at the next checkpoint."""
        job = self.get_job(job_id)
        if job["state"] in ("queued", "waiting_gpu"):
            return self.update_job(job_id, state="cancelled", message="cancelled before it started", finished_at=now_iso())
        if job["state"] == "running":
            return self.update_job(job_id, cancel_requested=1, message="cancelling...")
        return job

    def is_cancel_requested(self, job_id: str) -> bool:
        row = self.conn.execute("SELECT cancel_requested, state FROM jobs WHERE id=?", (job_id,)).fetchone()
        return bool(row and (row["cancel_requested"] or row["state"] == "cancelled"))

    def next_queued_job(self, lane: str) -> Optional[dict[str, Any]]:
        row = self.conn.execute(
            "SELECT id FROM jobs WHERE lane=? AND state='queued' ORDER BY created_at ASC, id ASC LIMIT 1",
            (lane,),
        ).fetchone()
        return self.get_job(row["id"]) if row else None

    # ------------------------------------------------------- studio voices
    def create_studio_voice(
        self, name: str, engine_id: str, voice_ref: str | None = None, sample_path: str | None = None,
        language: str | None = None, cloned: bool = False, reference_transcript: str | None = None,
        quality: dict[str, Any] | None = None, tags: list[str] | None = None, notes: str | None = None,
        project_id: str | None = None,
    ) -> dict[str, Any]:
        if not isinstance(name, str) or not name.strip():
            raise ValueError("a voice needs a non-empty name")
        if project_id:
            self.get_project(project_id)
        vid = new_id("voice")
        now = now_iso()
        self.conn.execute(
            """INSERT INTO studio_voices
               (id, project_id, name, engine_id, voice_ref, sample_path, language, cloned,
                reference_transcript, quality_json, presets_json, tags_json, notes, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (vid, project_id, name.strip()[:120], engine_id, voice_ref, sample_path, language,
             1 if cloned else 0, reference_transcript, dumps(quality or {}), dumps([]), dumps(tags or []),
             notes, now, now),
        )
        self.conn.commit()
        return self.get_studio_voice(vid)

    def get_studio_voice(self, voice_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM studio_voices WHERE id=?", (voice_id,)).fetchone()
        if not row:
            raise NotFound("voice", voice_id)
        d = row_to_dict(row)
        d["cloned"] = bool(d["cloned"])
        d["quality"] = loads(d.pop("quality_json"), {})
        d["presets"] = loads(d.pop("presets_json"), [])
        d["tags"] = loads(d.pop("tags_json"), [])
        return d

    def list_studio_voices(self, project_id: str | None = None, engine_id: str | None = None) -> list[dict[str, Any]]:
        sql = "SELECT id FROM studio_voices WHERE 1=1"
        args: list[Any] = []
        if project_id is not None:
            sql += " AND (project_id=? OR project_id IS NULL)"
            args.append(project_id)
        if engine_id:
            sql += " AND engine_id=?"
            args.append(engine_id)
        sql += " ORDER BY created_at DESC"
        rows = self.conn.execute(sql, args).fetchall()
        return [self.get_studio_voice(r["id"]) for r in rows]

    def update_studio_voice(self, voice_id: str, **fields: Any) -> dict[str, Any]:
        self.get_studio_voice(voice_id)
        cols, args = [], []
        json_fields = {"quality": "quality_json", "presets": "presets_json", "tags": "tags_json"}
        for key, value in fields.items():
            if key in json_fields:
                cols.append(f"{json_fields[key]}=?")
                args.append(dumps(value))
            elif key in ("name", "engine_id", "voice_ref", "sample_path", "language", "reference_transcript", "notes"):
                cols.append(f"{key}=?")
                args.append(value)
            elif key == "cloned":
                cols.append("cloned=?")
                args.append(1 if value else 0)
        if not cols:
            return self.get_studio_voice(voice_id)
        cols.append("updated_at=?")
        args.append(now_iso())
        args.append(voice_id)
        self.conn.execute(f"UPDATE studio_voices SET {', '.join(cols)} WHERE id=?", args)
        self.conn.commit()
        return self.get_studio_voice(voice_id)

    def add_voice_preset(self, voice_id: str, preset: dict[str, Any]) -> dict[str, Any]:
        voice = self.get_studio_voice(voice_id)
        presets = [p for p in voice["presets"] if p.get("name") != preset.get("name")]
        presets.append(preset)
        return self.update_studio_voice(voice_id, presets=presets)

    def delete_studio_voice(self, voice_id: str) -> None:
        self.get_studio_voice(voice_id)
        self.conn.execute("DELETE FROM studio_voices WHERE id=?", (voice_id,))
        self.conn.commit()

    # --------------------------------------------------------- agent calls
    def record_agent_call(self, tool: str, args_summary: str, duration_ms: float, ok: bool, error: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO agent_calls (id, tool, args_summary, duration_ms, ok, error, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (new_id("call"), tool, (args_summary or "")[:500], round(duration_ms, 1), 1 if ok else 0,
             (error or None) and error[:500], now_iso()),
        )
        self.conn.commit()

    def list_agent_calls(self, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 100))
        rows = self.conn.execute(
            "SELECT * FROM agent_calls ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        out = []
        for r in rows:
            d = row_to_dict(r)
            d["ok"] = bool(d["ok"])
            out.append(d)
        return out
