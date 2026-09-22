"""Data access layer: every read/write to SQLite goes through here.

Kept free of FastAPI imports so it can be unit tested directly and reused
by the MCP adapter's own tests. Every ``list_*`` method returns a small
number of items by default and marks ``has_more`` / ``next_offset`` per the
contract's "compact tool outputs" rule.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable, Optional

from .db import connect, dumps, loads, row_to_dict
from .ids import new_id
from .util import now_iso


class NotFound(KeyError):
    def __init__(self, kind: str, id_: str):
        super().__init__(f"{kind} not found: {id_}")
        self.kind = kind
        self.id = id_


BUILTIN_STYLE_PRESETS = [
    dict(
        name="Studio portrait",
        prompt_prefix="studio portrait photography, softbox lighting, shallow depth of field,",
        prompt_suffix=", sharp focus, high detail skin texture",
        negative="blurry, deformed, extra fingers, watermark, text",
        defaults=dict(checkpoint="sd_xl_base_1.0", width=1024, height=1024, steps=30, cfg=6.5, sampler="dpmpp_2m", scheduler="karras"),
    ),
    dict(
        name="Film still 35mm",
        prompt_prefix="35mm film still, cinematic lighting, kodak portra colour grade,",
        prompt_suffix=", subtle film grain, anamorphic bokeh",
        negative="digital artifacts, oversharpened, watermark, text",
        defaults=dict(checkpoint="sd_xl_base_1.0", width=1152, height=896, steps=32, cfg=6.0, sampler="dpmpp_2m", scheduler="karras"),
    ),
    dict(
        name="Anime cel",
        prompt_prefix="anime cel shading, clean line art, vibrant flat colours,",
        prompt_suffix=", studio quality key visual",
        negative="photorealistic, blurry lines, watermark, text",
        defaults=dict(checkpoint="sd_xl_base_1.0", width=1024, height=1024, steps=28, cfg=7.0, sampler="euler_a", scheduler="normal"),
    ),
    dict(
        name="Pastel dream",
        prompt_prefix="soft pastel colour palette, dreamy diffused light, gentle gradients,",
        prompt_suffix=", airy and delicate atmosphere",
        negative="high contrast, harsh shadows, watermark, text",
        defaults=dict(checkpoint="sd_xl_base_1.0", width=1024, height=1024, steps=28, cfg=6.0, sampler="dpmpp_2m", scheduler="karras"),
    ),
    dict(
        name="Neon night city",
        prompt_prefix="neon-lit night city, cyberpunk colour grade, wet reflective streets,",
        prompt_suffix=", glowing signage, moody atmosphere",
        negative="daylight, flat lighting, watermark, text",
        defaults=dict(checkpoint="sd_xl_base_1.0", width=1024, height=1024, steps=32, cfg=7.0, sampler="dpmpp_2m", scheduler="karras"),
    ),
    dict(
        name="Album art minimal",
        prompt_prefix="minimalist album cover art, bold negative space, striking single subject,",
        prompt_suffix=", graphic design composition",
        negative="cluttered, busy background, watermark, text",
        defaults=dict(checkpoint="sd_xl_base_1.0", width=1024, height=1024, steps=30, cfg=6.5, sampler="dpmpp_2m", scheduler="karras"),
    ),
]


class Store:
    def __init__(self, data_dir: Path):
        self.data_dir = Path(data_dir)
        self.conn: sqlite3.Connection = connect(self.data_dir)
        self.assets_dir = self.data_dir / "assets"
        self.thumbs_dir = self.data_dir / "thumbs"
        self.projects_dir = self.data_dir / "projects"
        for d in (self.assets_dir, self.thumbs_dir, self.projects_dir):
            d.mkdir(parents=True, exist_ok=True)
        self._seed_builtin_presets()

    # ---------------------------------------------------------------- misc
    def _seed_builtin_presets(self) -> None:
        row = self.conn.execute(
            "SELECT COUNT(*) c FROM style_presets WHERE is_builtin=1"
        ).fetchone()
        if row["c"] >= len(BUILTIN_STYLE_PRESETS):
            return
        for preset in BUILTIN_STYLE_PRESETS:
            exists = self.conn.execute(
                "SELECT id FROM style_presets WHERE name=? AND is_builtin=1",
                (preset["name"],),
            ).fetchone()
            if exists:
                continue
            self.conn.execute(
                """INSERT INTO style_presets
                   (id, project_id, name, prompt_prefix, prompt_suffix, negative,
                    defaults_json, notes, is_builtin, created_at)
                   VALUES (?, NULL, ?, ?, ?, ?, ?, ?, 1, ?)""",
                (
                    new_id("sp"),
                    preset["name"],
                    preset["prompt_prefix"],
                    preset["prompt_suffix"],
                    preset["negative"],
                    dumps(preset["defaults"]),
                    preset.get("notes", ""),
                    now_iso(),
                ),
            )
        self.conn.commit()

    def path_for_asset_file(self, asset_id: str, ext: str) -> Path:
        return self.assets_dir / f"{asset_id}{ext}"

    def path_for_thumb(self, asset_id: str) -> Path:
        return self.thumbs_dir / f"{asset_id}.webp"

    # ------------------------------------------------------------ projects
    def create_project(self, name: str, brief: str | None = None) -> dict[str, Any]:
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
        return row_to_dict(row)

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
            counts = self.conn.execute(
                "SELECT "
                "(SELECT COUNT(*) FROM assets WHERE project_id=?) assets, "
                "(SELECT COUNT(*) FROM characters WHERE project_id=?) characters, "
                "(SELECT COUNT(*) FROM timelines WHERE project_id=?) timelines",
                (p["id"], p["id"], p["id"]),
            ).fetchone()
            p["counts"] = row_to_dict(counts)
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
    ) -> dict[str, Any]:
        aid = asset_id or new_id("a")
        self.conn.execute(
            """INSERT INTO assets
               (id, project_id, kind, file_path, mime, width, height, duration_s,
                thumb_path, waveform_json, tags_json, rating, favourite, notes,
                source, recipe_json, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,0,0,?,?,?,?)""",
            (
                aid, project_id, kind, file_path, mime, width, height, duration_s,
                thumb_path, dumps(waveform) if waveform is not None else None,
                dumps(tags or []), notes, source,
                dumps(recipe) if recipe is not None else None, now_iso(),
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
        d["favourite"] = bool(d["favourite"])
        return d

    def update_asset(self, asset_id: str, **fields: Any) -> dict[str, Any]:
        self.get_asset(asset_id)
        cols, params = [], []
        for key in ("tags", "rating", "favourite", "notes"):
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
    ) -> dict[str, Any]:
        limit = max(1, min(limit, 60))
        sql = "SELECT * FROM assets WHERE 1=1"
        params: list[Any] = []
        if project_id:
            sql += " AND project_id=?"
            params.append(project_id)
        if kind:
            sql += " AND kind=?"
            params.append(kind)
        if query:
            sql += " AND (notes LIKE ? OR tags_json LIKE ?)"
            like = f"%{query}%"
            params += [like, like]
        if tag:
            sql += " AND tags_json LIKE ?"
            params.append(f"%\"{tag}\"%")
        if favourite:
            sql += " AND favourite=1"
        sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params += [limit + 1, offset]
        rows = self.conn.execute(sql, params).fetchall()
        items = [self.get_asset(r["id"]) for r in rows[:limit]]
        has_more = len(rows) > limit
        return {"items": items, "has_more": has_more, "next_offset": offset + limit if has_more else None}

    # --------------------------------------------------------- characters
    def create_character(self, project_id: str, name: str, **fields: Any) -> dict[str, Any]:
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

    def update_character(self, character_id: str, **fields: Any) -> dict[str, Any]:
        self.get_character(character_id)
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
    def create_group(self, project_id: str, name: str, **fields: Any) -> dict[str, Any]:
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
        self.get_group(group_id)
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

    def update_board_items(self, board_id: str, items: list[dict[str, Any]]) -> dict[str, Any]:
        self.get_board(board_id)
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
                audio_asset_id, tracks_json, created_at, updated_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
            (
                tid, project_id, name, aspect, fps, width, height,
                fields.get("audio_asset_id"), dumps(fields.get("tracks", [])), ts, ts,
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
                    project_id: str | None = None) -> dict[str, Any]:
        jid = new_id("job")
        ts = now_iso()
        self.conn.execute(
            """INSERT INTO jobs (id, project_id, type, lane, params_json, inputs_json,
                state, progress, message, created_at, updated_at)
               VALUES (?,?,?,?,?,?,'queued',0.0,NULL,?,?)""",
            (jid, project_id, type_, lane, dumps(params), dumps(inputs or {}), ts, ts),
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
        return d

    def list_jobs(self, state: str | None = None, limit: int = 10, offset: int = 0) -> dict[str, Any]:
        limit = max(1, min(limit, 50))
        sql = "SELECT id FROM jobs"
        params: list[Any] = []
        if state:
            sql += " WHERE state=?"
            params.append(state)
        sql += " ORDER BY created_at DESC LIMIT ? OFFSET ?"
        params += [limit + 1, offset]
        rows = self.conn.execute(sql, params).fetchall()
        items = [self.get_job(r["id"]) for r in rows[:limit]]
        has_more = len(rows) > limit
        return {"items": items, "has_more": has_more, "next_offset": offset + limit if has_more else None}

    def update_job(self, job_id: str, **fields: Any) -> dict[str, Any]:
        self.get_job(job_id)
        cols, params = [], []
        for key in ("state", "progress", "message", "backend", "log_excerpt", "started_at", "finished_at"):
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
            "UPDATE jobs SET state='queued', progress=0.0, message='requeued after restart', updated_at=? "
            "WHERE state IN ('running','waiting_gpu')",
            (now_iso(),),
        )
        self.conn.commit()
        return cur.rowcount

    def next_queued_job(self, lane: str) -> Optional[dict[str, Any]]:
        row = self.conn.execute(
            "SELECT id FROM jobs WHERE lane=? AND state='queued' ORDER BY created_at ASC LIMIT 1",
            (lane,),
        ).fetchone()
        return self.get_job(row["id"]) if row else None

    # --------------------------------------------------------- agent calls
    def record_agent_call(self, tool: str, args_summary: str, duration_ms: float, ok: bool, error: str | None = None) -> None:
        self.conn.execute(
            "INSERT INTO agent_calls (id, tool, args_summary, duration_ms, ok, error, created_at)"
            " VALUES (?,?,?,?,?,?,?)",
            (new_id("call"), tool, args_summary[:500], duration_ms, 1 if ok else 0, error, now_iso()),
        )
        self.conn.commit()

    def list_agent_calls(self, limit: int = 20) -> list[dict[str, Any]]:
        limit = max(1, min(limit, 100))
        rows = self.conn.execute(
            "SELECT * FROM agent_calls ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [row_to_dict(r) for r in rows]
