"""Editing a clip from an instruction ("make it night and raining", "put
her in a red coat", "swap the guitar for a violin"): the task, the text and
the instruction rewrite for Bernini-R, a Wan renderer fine-tuned for video
editing (ByteDance, Apache-2.0; the `BerniniConditioning` core node).

Pure logic: no ComfyUI, no FastAPI. `engine.clip_edit_job` runs it.

Bernini's pipeline writes the positive text as the task's system line
followed directly by the instruction (`system_prompt + prompt`), and
expects instructions in the shape its own prompt enhancer writes: what
changes, in concrete detail, and what stays the same. `enhance_messages`
builds that rewrite for the family's language model (with frames of the
clip when a vision model is there, with the clip's own prompt otherwise).
"""

from __future__ import annotations

import json
import re
from typing import Any, Optional

# the system line of each Bernini task (bernini/prompt_enhancer.py)
SYSTEM_LINES = {
    "v2v": "You are a helpful assistant specialized in video editing.",
    "vi2v": "You are a helpful assistant specialized in video editing on content propagation.",
    "rv2v": "You are a helpful assistant specialized in video editing with reference.",
    "mv2v": ("You are a helpful assistant for editing. You might need to adjust the video's style, lighting, colors, "
             "textures, and the subject's pose or action."),
}

# what the user picks -> the Bernini task
MODES = {
    "edit": "v2v",          # replace, add, remove, recolour: the motion stays
    "restyle": "mv2v",      # a new look, light, colours, or a change of pose or action
    "reference": "rv2v",    # with reference images (a cast member, a garment, a place)
    "propagate": "vi2v",    # the first frame was edited as a picture: carry it through the clip
}

PROPAGATE_PROMPT = "edit the video following the first frame."
MAX_REFERENCES = 4
KIND_WORDS = {"character": "the person", "location": "the place", "prop": "the object"}


class ClipEditError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code, self.message = code, message


def resolve_mode(mode: Optional[str], references: int, first_frame: bool) -> str:
    """The Bernini task for a request. `auto` picks propagation when an
    edited first frame is given, the reference task when there are
    references, plain editing otherwise."""
    mode = (mode or "auto").strip().lower()
    if mode == "auto":
        return "vi2v" if first_frame else ("rv2v" if references else "v2v")
    if mode not in MODES:
        raise ClipEditError("bad_mode", "mode is auto, edit, restyle, reference or propagate")
    task = MODES[mode]
    if task == "vi2v" and not first_frame:
        raise ClipEditError("first_frame_required", "propagate needs the edited first frame (first_frame_asset_id): "
                                                    "take the clip's first frame, edit it as a picture, then pass it")
    if task == "rv2v" and not references:
        raise ClipEditError("reference_required", "the reference mode needs at least one reference image "
                                                  "(reference_asset_ids, or @Name of a cast member with a picture)")
    return task


def positive_text(task: str, instruction: str) -> str:
    """The text the renderer is trained on: system line + instruction."""
    text = re.sub(r"\s+", " ", str(instruction or "")).strip()
    if task == "vi2v" and not text:
        text = PROPAGATE_PROMPT
    if not text:
        raise ClipEditError("empty_prompt", "say what to change in the clip")
    return SYSTEM_LINES[task] + text


def name_references(pieces: list[Any], offset: int = 0) -> tuple[str, list[dict[str, Any]]]:
    """A prompt split by `engine._scan_mentions` (text and cast entries) as
    the renderer's text: a cast entry with a picture becomes "the person in
    image0" (references are read as image0, image1... in order; `offset`
    references come before them), one without a picture its written look.
    Returns the text and the entries used as references, in order (at most
    MAX_REFERENCES in all)."""
    used: list[dict[str, Any]] = []
    out: list[str] = []
    for piece in pieces:
        if isinstance(piece, str):
            out.append(piece)
            continue
        word = KIND_WORDS.get(piece.get("element") or "character", "the subject")
        if piece.get("canonical_asset_id") and (piece in used or offset + len(used) < MAX_REFERENCES):
            if piece not in used:
                used.append(piece)
            out.append(f"{word} in image{offset + used.index(piece)}")
        else:
            look = str(piece.get("prompt") or "").strip()
            out.append(f"{word}, {look}," if look else str(piece.get("name") or word))
    return "".join(out), used


# --- the instruction rewrite (adapted from Bernini's prompt enhancer) -------

V2V_RULES = """You write editing instructions for a video editing model. Rewrite the user's instruction into one detailed English paragraph:
1. Modifications: describe exactly what changes - appearance, colours, materials, position, light, and how it follows the motion.
2. Preservations: say plainly what stays the same - the people, their motion, camera framing and movement, background, light, shadows - unless the instruction changes it.
3. Concrete, not vague: "different clothes" becomes a specific outfit, "a cartoon style" a named, described style.
Use the verb that fits: "Replace X with Y", "Add X at/on Y", "Delete X from Y", "Convert the video to <style>: <details>", "Change the light to ...", "Change the person's motion so that ...".
Describe the result directly (never "after editing"); left and right are from the camera's point of view; no parentheses.
Never invent things that are not in the video. Answer with the paragraph only, no quotes, no preamble.

Example: Add a pair of realistic sunglasses to the man centered in the frame: thin matte-black rectangular frame and dark gray lenses that subtly reflect the green foliage and sky, resting on the bridge of his nose with temple arms over his ears. Match the soft outdoor daylight with gentle reflections on the lenses and soft contact shadows on his cheeks. Keep his face, hair, clothes, motion, the camera framing and every other part of the scene unchanged."""

RV2V_RULES = """The reference images are image0, image1... in the order given. Infer each one's role from the instruction (the person or object to put in, a style, a garment). Start with the editing sentence ("Replace the woman with the person in image0, ...", "Dress the man in the jacket from image1, ..."), then describe the resulting video as one paragraph: the subject's look must match the reference exactly, everything the instruction does not change stays as it is (motion, framing, light, background)."""


def enhance_messages(task: str, instruction: str, scene: Optional[str] = None, references: int = 0,
                     with_frames: bool = False) -> list[dict[str, Any]]:
    """Chat messages that rewrite `instruction` into the detailed shape the
    renderer was trained on. `scene` is what the clip shows (its own
    generation prompt) when the model cannot see frames of it."""
    rules = V2V_RULES + ("\n\n" + RV2V_RULES if task == "rv2v" else "")
    context = []
    if with_frames:
        context.append("The first images are frames of the source video, in order"
                       + (f"; the last {references} are the reference images image0..image{references - 1}." if references else "."))
    elif scene:
        context.append(f"The source video shows: {scene.strip()[:900]}")
    if references and not with_frames:
        context.append(f"There are {references} reference image(s), image0..image{references - 1}; keep every "
                       "'imageN' mention as it is.")
    user = "\n".join(context + [f"Instruction: {instruction.strip()}"])
    return [{"role": "system", "content": rules}, {"role": "user", "content": user}]


def clean_rewrite(text: str, fallback: str) -> str:
    """The model's answer as one plain paragraph (it sometimes wraps it in
    quotes, a JSON object or a "Prompt:" label); the original instruction
    when it gave nothing usable."""
    out = (text or "").strip()
    if out.startswith("{"):
        try:
            out = str(json.loads(out).get("rewritten_text") or "")
        except (ValueError, AttributeError):
            pass
    out = out.strip().strip('"').strip("'").strip()
    out = re.sub(r"^(?:\*\*)?(?:enhanced |final |rewritten )?(?:prompt|instruction)(?:\*\*)?\s*:\s*", "", out, flags=re.I)
    out = re.sub(r"\s+", " ", out.strip().strip('"').strip("'")).strip()
    out = re.sub(r"\s*\([^)]*\)", "", out)  # the renderer was trained without parentheses
    return out if len(out) >= 12 else fallback


def window(duration_s: float, start_s: float = 0.0, fps: int = 16, max_frames: int = 81) -> dict[str, int]:
    """The frames an edit renders: from `start_s`, up to `max_frames`
    (Bernini's 5 s at 16 fps), as a 4n+1 count. A longer clip is edited a
    window at a time and the window is spliced back."""
    total = max(1, int(round(duration_s * fps)))
    a = max(0, int(round(start_s * fps)))
    if a >= total - 8:
        raise ClipEditError("bad_range", "start_s is at (or past) the end of the clip")
    length = min(max_frames, total - a)
    length -= (length - 1) % 4
    if length < 9:
        raise ClipEditError("clip_too_short", "a clip edit needs at least about half a second of video")
    return {"start": a, "length": length, "total": total}
