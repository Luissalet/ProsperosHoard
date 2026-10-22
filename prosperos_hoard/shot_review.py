"""Timecoded shot reviews, revision-linked comments, and selective dependent regeneration.

Allows adding review comments anchored to exact timestamps and shot revisions.
Supports selective regeneration of only dependent shots (direct targets plus
downstream continuation chains) while strictly preserving all other approved and
unrelated shots.

Persisted atomically to ensure comments survive reopenings and preserve historical revisions.
"""

from __future__ import annotations

import json
import hashlib
import math
from pathlib import Path
from typing import Any, Optional

from .ids import new_id
from .util import now_iso, write_text_atomic

FORMAT = "prospero.shot_reviews/1"


def revision_id(state: dict[str, Any], key: str) -> str:
    """Identify the actual editable shot and current takes, including ordinary edits."""
    from . import productions as prod
    shot = next((s for s in state["spec"].get("shots", []) if s["key"] == key), None)
    if shot is None:
        raise ShotReviewError("shot_not_found", f"Shot '{key}' not found")
    done = state.get("done") or {}
    content = {"shot": {k: v for k, v in shot.items() if k != "locked"},
               "frame": ((done.get("frames") or {}).get("items") or {}).get(key),
               "clips": {k: v for k, v in ((done.get("clips") or {}).get("items") or {}).items() if prod.split_key(k)[0] == key}}
    return hashlib.sha256(json.dumps(content, sort_keys=True, ensure_ascii=False).encode("utf-8")).hexdigest()[:20]


class ShotReviewError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def reviews_file_path(data_dir: Path, slug: str) -> Path:
    from . import productions as prod
    p_dir = prod.production_dir(data_dir, slug)
    return p_dir / "shot_reviews.json"


def load_shot_reviews(data_dir: Path, slug: str) -> dict[str, Any]:
    file_path = reviews_file_path(data_dir, slug)
    if not file_path.is_file():
        return {
            "format": FORMAT,
            "slug": slug,
            "updated_at": now_iso(),
            "shot_revisions": {},
            "comments": [],
        }
    try:
        return json.loads(file_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ShotReviewError("corrupted_reviews", f"Reviews file corrupted for {slug}: {exc}")


def save_shot_reviews(data_dir: Path, slug: str, data: dict[str, Any]) -> None:
    data["updated_at"] = now_iso()
    file_path = reviews_file_path(data_dir, slug)
    content = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
    write_text_atomic(file_path, content)


def get_shot_revision(data_dir: Path, slug: str, shot_key: str) -> int:
    data = load_shot_reviews(data_dir, slug)
    return int((data.get("shot_revisions") or {}).get(str(shot_key), 1))


def add_shot_comment(
    data_dir: Path,
    slug: str,
    shot_key: str,
    time_s: float,
    content: str,
    author: str = "reviewer",
    kind: str = "request_change",
) -> dict[str, Any]:
    from . import productions as prod

    key_str = str(shot_key).strip()
    if not key_str:
        raise ShotReviewError("empty_shot_key", "Shot key cannot be empty")
    text = str(content).strip()
    if not text:
        raise ShotReviewError("empty_comment", "Comment text cannot be empty")

    with prod.lock_for(slug):
        state = prod._editable(data_dir, slug)
        shots = (state.get("spec") or {}).get("shots") or []
        target_shot = next((s for s in shots if s["key"] == key_str), None)
        if not target_shot:
            raise ShotReviewError("shot_not_found", f"Shot '{key_str}' not found in production '{slug}'")
        instant = float(time_s)
        span = prod.shot_span(target_shot)
        if not math.isfinite(instant) or instant < 0 or (span and not span[0] <= instant <= span[1]):
            raise ShotReviewError("bad_time", "Comment timestamp must be finite and inside the shot's production interval")

        reviews = load_shot_reviews(data_dir, slug)
        revs = reviews.setdefault("shot_revisions", {})
        current_rev = int(revs.get(key_str, 1))
        revs[key_str] = current_rev

        comment_id = f"c_{new_id()}"
        comment = {
            "id": comment_id,
            "shot_key": key_str,
            "time_s": round(float(time_s), 3),
            "shot_revision": current_rev,
            "revision_id": revision_id(state, key_str),
            "author": author.strip() or "reviewer",
            "content": text,
            "kind": kind if kind in ("request_change", "critique", "note", "approved") else "request_change",
            "resolved": False,
            "resolution_note": None,
            "created_at": now_iso(),
            "resolved_at": None,
        }

        reviews.setdefault("comments", []).append(comment)
        save_shot_reviews(data_dir, slug, reviews)

        prod.log(state, "shot_review", "comment_added", comment_id=comment_id, shot_key=key_str, revision=current_rev)
        prod.save_state(data_dir, state)

        return comment


def list_shot_comments(data_dir: Path, slug: str, shot_key: Optional[str] = None) -> list[dict[str, Any]]:
    from . import productions as prod
    state = prod.load_state(data_dir, slug)
    reviews = load_shot_reviews(data_dir, slug)
    revs = reviews.get("shot_revisions") or {}
    comments = reviews.get("comments") or []
    out = []
    filter_key = str(shot_key).strip() if shot_key else None
    for c in comments:
        sk = str(c.get("shot_key"))
        if filter_key and sk != filter_key:
            continue
        curr_rev = int(revs.get(sk, 1))
        c_rev = int(c.get("shot_revision", 1))
        try:
            current_id = revision_id(state, sk)
        except ShotReviewError:
            current_id = None
        out.append({
            **c,
            "current_shot_revision": curr_rev,
            "current_revision_id": current_id,
            "is_stale": (c.get("revision_id") != current_id) if c.get("revision_id") else (c_rev != curr_rev),
        })
    return out


def resolve_shot_comment(data_dir: Path, slug: str, comment_id: str, note: str = "") -> dict[str, Any]:
    from . import productions as prod

    with prod.lock_for(slug):
        reviews = load_shot_reviews(data_dir, slug)
        comments = reviews.get("comments") or []
        found = next((c for c in comments if c["id"] == comment_id), None)
        if not found:
            raise ShotReviewError("comment_not_found", f"Comment '{comment_id}' not found")
        found["resolved"] = True
        found["resolution_note"] = str(note).strip() or "Resolved"
        found["resolved_at"] = now_iso()
        save_shot_reviews(data_dir, slug, reviews)
        return found


def regenerate_dependent_shots(
    data_dir: Path,
    slug: str,
    target_keys: list[str],
    reason: str = "",
    stage: str = "clips",
    force: bool = False,
) -> dict[str, Any]:
    """Regenerates only the specified shots and their downstream dependent continuation chains
    while strictly preserving all approved and unrelated shots.

    Preserves earlier takes in the shot's history so they can be restored if needed.
    Increments shot revision for every regenerated shot so timecoded comments know
    which shot revision they were made against.
    """
    from . import productions as prod

    if stage not in ("frames", "clips"):
        raise ShotReviewError("bad_stage", "stage must be 'frames' or 'clips'")

    target_set = {str(k).strip() for k in target_keys if str(k).strip()}
    if not target_set:
        raise ShotReviewError("empty_targets", "Must provide at least one target shot key to regenerate")

    with prod.lock_for(slug):
        state = prod._editable(data_dir, slug)
        spec = state.get("spec") or {}
        shots = spec.get("shots") or []
        shot_map = {s["key"]: s for s in shots}

        unknown = sorted(target_set - set(shot_map.keys()))
        if unknown:
            raise ShotReviewError("unknown_shots", f"Unknown shot keys: {', '.join(unknown)}")

        # Build continuation dependency graph: parent -> list of downstream shots
        downstream: dict[str, list[str]] = {}
        for s in shots:
            parent = s.get("continue_from")
            if parent:
                downstream.setdefault(str(parent), []).append(s["key"])

        # Expand dependencies transitively
        dependent_keys: set[str] = set()
        queue = list(target_set)
        while queue:
            curr = queue.pop(0)
            if curr not in dependent_keys:
                dependent_keys.add(curr)
                for child in downstream.get(curr, []):
                    if child not in dependent_keys:
                        queue.append(child)

        # Check locked status
        for k in dependent_keys:
            s = shot_map[k]
            if s.get("locked"):
                if k in target_set and not force:
                    raise ShotReviewError("shot_locked", f"Shot '{k}' is locked; pass force=True to unlock and regenerate")
                elif k in target_set and force:
                    s["locked"] = False
                elif k not in target_set:
                    # A downstream shot is locked, unlock with chain warning or preserve?
                    # In Prospero, a continuation chain is invalid if parent changes
                    if not force:
                        raise ShotReviewError("downstream_locked", f"Downstream dependent shot '{k}' is locked; pass force=True to regenerate chain")
                    s["locked"] = False

        reviews = load_shot_reviews(data_dir, slug)
        revs = reviews.setdefault("shot_revisions", {})

        frames_entry = state["done"].setdefault("frames", {"complete": False, "items": {}})
        frames_items = frames_entry.setdefault("items", {})
        clips_entry = state["done"].setdefault("clips", {"complete": False, "items": {}})
        clips_items = clips_entry.setdefault("items", {})

        regenerated_list: list[str] = []
        preserved_list: list[str] = []

        for shot in shots:
            key = shot["key"]
            if key not in dependent_keys:
                preserved_list.append(key)
                continue

            # Increment revision for the regenerated shot
            revs[key] = int(revs.get(key, 1)) + 1

            mine_clips = [ck for ck in clips_items if prod.split_key(ck)[0] == key]
            if stage == "frames":
                shot["seed"] = int(shot.get("seed", 1000)) + 1000
                if frames_items.get(key):
                    prod.remember_take(state, "frames", key, dict(frames_items[key]))
                frames_items.pop(key, None)
            shot["clip_seed"] = int(shot.get("clip_seed", 2000)) + 1000
            for ck in mine_clips:
                prod.remember_take(state, "clips", ck, {"asset_id": clips_items[ck],
                    "quality": (clips_entry.get("quality") or {}).get(ck, "final")})
                clips_items.pop(ck, None)
                (clips_entry.get("quality") or {}).pop(ck, None)
                (clips_entry.get("chained") or {}).pop(ck, None)

            regenerated_list.append(key)

        # Clean stale chain records
        chained_dropped = prod.drop_stale_chains(state)

        # Requeue dependent render stages
        msg = reason.strip() or f"Regenerated dependent shot(s) {', '.join(sorted(regenerated_list))}"
        prod._requeue(state, msg)

        prod.log(
            state,
            "shot_review",
            "regenerated_dependent",
            targets=list(target_set),
            regenerated=regenerated_list,
            preserved=preserved_list,
            chained=chained_dropped,
        )

        prod.save_state(data_dir, state)
        save_shot_reviews(data_dir, slug, reviews)

        return {
            "slug": slug,
            "stage": stage,
            "targets": sorted(target_set),
            "regenerated_shots": sorted(regenerated_list),
            "preserved_shots": sorted(preserved_list),
            "chained_dropped": chained_dropped,
            "shot_revisions": {k: revs[k] for k in sorted(revs.keys())},
            "status": state["status"],
        }
