"""Tools exposed to assistants. One catalogue drives /api/agent/*, mcp_server.py and part of the UI routes."""

from __future__ import annotations

import base64
import dataclasses
from pathlib import Path
from typing import Any, Callable, Literal, Optional

from pydantic import BaseModel, Field

from . import agent_undo, analyze, commands, derive, family_events, multicam, speakers, timeline_import
from . import broll as broll_mod
from . import music as music_mod
from . import subtitles as subtitles_mod
from . import media as media_store
from . import plan as plan_mod
from . import projects as project_store
from .errors import LumiereError
from .hoard_link import agentkit, fam_workspace
from .hoard_link.agentkit import Tool, ann as _ann
from .hoard_link.waiting import MAX_WAIT_S
from .ops import OP_NAMES, PRESETS, RAMP_PRESETS, op_reference
from .render import filters as fx
from .render import formats as formats_mod
from .render import runner
from . import compression
from .services import Services
from .timeline import TRANSITIONS
from .util import ms_to_tc, parse_time

AGENT_INSTRUCTIONS = """Lumiere's Hoard edits videos on this computer. Typical flow: media_import (a file or folder the user names; nothing is
copied) -> project_create (preset: reels, youtube, square...; media to start with) -> look with project_get (outline) and frame_snapshot ->
edit -> render_start -> job_status until done -> tell the user the file path.
For an Atlas shared file, use media_shared(file_id) instead of importing a copy. Repeat after a native edit to refresh caches
while keeping its media ID in timelines; wait for active jobs on that media first. Files are not watched continuously.
Two ways to edit: (1) plan_create with the user's words ("quita los silencios, subtítulos y vertical"), show the steps, then plan_apply;
(2) precise work: timeline_edit with operations (ids come from project_get), or edit_command for smart edits (remove_silences,
remove_fillers, cut_words, split_scenes, reframe, captions, beat_sync, match_loudness). Times are ms or '1:23.5'.
Text-based editing: timeline_transcript shows the words heard on the timeline with ids; text_cut removes words or keeps only some.
Several people talking: media_analyze kinds=['speakers'] labels every word with its speaker (S1, S2...), speakers_edit renames them
("Ana") or fixes who said what, edit_command speaker_cut removes or keeps one person's parts, captions can show names or colours.
Several cameras of one event: multicam_sync measures their offset by sound, multicam_create builds the group (one continuous master
sound), multicam_switch shows another angle at a time or range, multicam_auto cuts to whoever is speaking, multicam_get reads the group.
Smart edits and captions need analyses (transcript, scenes, focus, beats, loudness): when one is missing the tool queues it and says so;
check job_status and repeat. Background music: music_pick ranks a folder's tracks for the edit (add='best' places it); b-roll: broll_suggest
matches library clips to what is said (place='best', or timeline_edit add_overlay); reusable edits: template_save, template_list and
project_create with template + slots. Every change is one undo step (timeline_history undo/redo). Deletes need confirm=true and only when the
user asks. Never import or export outside the folders the user mentions. Renders run in the background; one at a time.
Several outputs at once: render_start with formats (['16:9','9:16','1:1']) makes one file per canvas in a single job, from copies (the project is
not changed; reframe auto|center|blur says how the picture is framed where the shape differs); every file has its own quality check.
Translated subtitles: subtitles_translate (needs the transcript and the local model; a job; cues keep the original timing) then
subtitles_export with language (srt, vtt, ass; dual = original plus translation) or render_start with captions_language (+ captions_dual) to burn them in;
a translation goes stale when the transcript or the cuts change: translate again, unchanged cues are reused.
An edit made in another app arrives with project_from_timeline (an FCP7 XML or EDL file, or a plan of clips and markers); files that cannot be found are
skipped and listed under skipped.
""" + family_events.contract_text() + '''
Family events sent: ''' + ", ".join(family_events.EMITS) + ". Accepted: " + ", ".join(family_events.ACCEPTS) + "."


class Empty(BaseModel):
    pass


ProjectId = Field(..., min_length=1, max_length=40, description="Project id (prj_...).")
MediaId = Field(..., min_length=1, max_length=40, description="Media id (med_...).")
TimeVal = Optional[float | int | str]


class MediaImportArgs(BaseModel):
    path: str = Field("", max_length=2000, description="A video, audio or image file the user named.")
    folder: str = Field("", max_length=2000, description="Or a folder: imports every media file in it.")
    recursive: bool = False


class SharedMediaArgs(BaseModel):
    file_id: str = Field(..., min_length=1, max_length=40, description="Atlas registered file ID; Lumiere must be a project member.")


class MediaListArgs(BaseModel):
    kind: Optional[Literal["video", "audio", "image"]] = None
    query: str = Field("", max_length=200)
    limit: int = Field(100, ge=1, le=500)


class MediaRef(BaseModel):
    media: str = MediaId


class MediaDeleteArgs(MediaRef):
    confirm: bool = Field(False, description="Required. Removes it from the library (the original file stays, except uploads).")
    force: bool = Field(False, description="Also when projects use it (their clips will show as missing).")


class AnalyzeArgs(MediaRef):
    kinds: list[Literal["scenes", "beats", "loudness", "motion", "focus", "transcript", "speakers"]] = Field(..., min_length=1)
    force: bool = False
    num_speakers: Optional[int] = Field(None, ge=1, le=12, description="speakers: how many people talk (empty = detected automatically).")
    speakers: bool = Field(False, description="transcript: also separate the speakers right after transcribing.")
    engine: Literal["auto", "builtin", "embeddings"] = Field("auto", description="speakers: auto = voice embeddings when installed (requirements-speakers.txt), else built in; builtin forces the built-in engine.")
    language: str = Field("", max_length=10, description="Transcript language (es, en...); empty = detect.")
    model: str = Field("", max_length=80, description="Whisper model; empty = the setting (large-v3-turbo on GPU).")


class SilencesArgs(MediaRef):
    threshold_db: Optional[float] = Field(None, ge=-80, le=0, description="Empty = automatic from the noise floor.")
    min_silence_ms: int = Field(500, ge=100, le=10000)
    margin_ms: int = Field(150, ge=0, le=2000)


class TranscriptArgs(MediaRef):
    offset: int = Field(0, ge=0)
    limit: int = Field(400, ge=1, le=3000)
    format: Literal["words", "text"] = "text"


class TranscriptFixArgs(MediaRef):
    changes: list[dict[str, str]] = Field(..., min_length=1, max_length=500, description="[{id: 'w12', text: 'corrected'}]")


class SpeakersEditArgs(MediaRef):
    action: Literal["diarize", "rename", "assign", "merge", "clear"] = Field(..., description="diarize: (re)separate the voices; rename: names={'S1': 'Ana'}; "
                                                                           "assign: say who spoke word_ids or a from_ms-to_ms range; merge: source into one speaker; "
                                                                           "clear: back to a single voice.")
    num_speakers: Optional[int] = Field(None, ge=1, le=12, description="diarize: how many people talk (empty = automatic).")
    engine: Literal["auto", "builtin", "embeddings"] = Field("auto", description="diarize: builtin forces the built-in engine, embeddings requires the optional voice-embedding library.")
    names: dict[str, str] = Field(default_factory=dict, description="rename: {speaker id or current name: new name}.")
    speaker: str = Field("", max_length=40, description="assign: speaker id or name (a new name creates the speaker).")
    word_ids: list[str] = Field(default_factory=list, max_length=5000, description="assign: transcript word ids.")
    from_ms: Optional[int] = Field(None, ge=0, description="assign: start of the source range (media time, ms).")
    to_ms: Optional[int] = Field(None, ge=0, description="assign: end of the source range.")
    source: str = Field("", max_length=40, description="merge: the speaker that disappears.")
    into: str = Field("", max_length=40, description="merge: the speaker that keeps all the words.")


class MulticamSyncArgs(BaseModel):
    media: list[str] = Field(..., min_length=2, max_length=12, description="Media ids of recordings of the same event (media_list).")
    reference: Optional[str] = Field(None, description="Media id whose clock is the reference (default: the first).")
    offsets: dict[str, float] = Field(default_factory=dict, description="Manual start in ms per media id, overriding the measurement.")


class MulticamCreateArgs(BaseModel):
    project: str = ProjectId
    media: list[str] = Field(..., min_length=2, max_length=12, description="Media ids recorded at the same time (cameras, an external microphone).")
    name: str = Field("Multicámara", max_length=80)
    offsets: dict[str, float] = Field(default_factory=dict, description="Manual sync in ms per media id; the rest is measured by sound.")
    master: Optional[str] = Field(None, description="Media id (or label) whose sound is heard throughout; default: the first with sound.")
    angle: Optional[str] = Field(None, description="Angle shown first (media id, label or 1-based number).")
    at: TimeVal = Field(None, description="Timeline time where the group starts; empty = the end of the main track.")


class MulticamSwitchArgs(BaseModel):
    project: str = ProjectId
    angle: Optional[str | int] = Field(None, description="Angle to show: 1-based number, label ('Cámara B') or media id.")
    at: TimeVal = Field(None, description="Timeline time where the new angle starts (the playhead).")
    end: TimeVal = Field(None, description="Timeline time where it ends; empty = the end of that shot.")
    clip: Optional[str] = Field(None, description="Or a clip of the group: its whole shot is switched.")
    group: Optional[str] = Field(None, description="Group id (multicam_get); empty = the only one.")
    cuts: Optional[list[list[Any]]] = Field(None, description="Many at once: [[timeline_ms, angle], ...]; each holds until the next.")


class MulticamAutoArgs(BaseModel):
    project: str = ProjectId
    group: Optional[str] = None
    mode: Literal["loudness", "speakers"] = Field("loudness", description="loudness: the camera whose own microphone is loudest above its noise; "
                                                                           "speakers: the diarized speakers of the master sound, each mapped to a camera.")
    min_shot_ms: int = Field(2000, ge=300, le=60000, description="No shot shorter than this.")
    hysteresis_db: float = Field(4.0, ge=0, le=40, description="How much louder the other camera must be to cut (avoids flicker).")
    dwell_ms: int = Field(400, ge=0, le=5000)
    lead_ms: int = Field(150, ge=0, le=2000, description="Cut this long before the voice starts.")
    wide: Optional[str] = Field(None, description="Angle to return to after long silences (e.g. a wide shot).")
    speaker_map: dict[str, str] = Field(default_factory=dict, description="speakers mode: {speaker name: angle label}; empty = measured.")
    start: TimeVal = None
    end: TimeVal = None


class MulticamGetArgs(BaseModel):
    project: str = ProjectId
    group: Optional[str] = None


class HighlightsArgs(MediaRef):
    count: int = Field(5, ge=1, le=30)
    length_s: float = Field(30, ge=3, le=600)
    mode: Literal["signals", "model"] = Field("signals", description="signals: sound, motion, cuts, speech. model: also read the transcript with the local model "
                                              "for moments that stand on their own (hook, punchline, complete thought), merged into one ranked list; "
                                              "falls back to signals when there is no transcript or model (the answer says why).")


class MediaTagArgs(MediaRef):
    tags: list[str] = Field(..., max_length=30, description="Labels for the media (replaces the old ones); b-roll suggestions match them.")


class ProjectCreateArgs(BaseModel):
    name: str = Field("Proyecto", max_length=120)
    preset: Optional[str] = Field(None, description="Canvas: " + ", ".join(PRESETS))
    media: list[str] = Field(default_factory=list, max_length=200, description="Media ids to put on the timeline in order.")
    from_project: Optional[str] = Field(None, description="Start as a copy of this project.")
    template: str = Field("", max_length=120, description="A template (id or name, see template_list): the new project keeps its titles, captions, music and "
                          "effects and replaces the media of its slots with `slots`.")
    slots: dict[str, Any] = Field(default_factory=dict, description="With template: {slot name: media id or name}, e.g. {'intro': 'logo.mp4', 'main': 'take3'}. "
                                  "A slot named main* takes its media's full length and the rest moves; slots left out keep the template's sample media.")


class TemplateSaveArgs(BaseModel):
    project: str = ProjectId
    name: str = Field(..., min_length=1, max_length=120)
    slots: dict[str, str] = Field(default_factory=dict, description="{clip id: slot name} (intro, main, outro...) on top of slots the clips already have.")


class MusicPickArgs(BaseModel):
    project: str = ProjectId
    folder: str = Field(..., max_length=2000, description="A folder with audio files (inside the folders the user named).")
    recursive: bool = False
    count: int = Field(5, ge=1, le=20)
    add: str = Field("", max_length=2000, description="'best' or the path of one suggested track: also put it under the edit (trimmed and faded to length, "
                     "ducking on) as one undo step.")
    volume_db: float = Field(-8.0, ge=-40, le=6)


class BrollArgs(BaseModel):
    project: str = ProjectId
    start: TimeVal = Field(None, description="Look for pictures for this range of the edit as a whole (ms or '1:23.5')...")
    end: TimeVal = Field(None, description="...to this time; both omitted = one query per sentence of the main track's speech.")
    per_sentence: int = Field(3, ge=1, le=8)
    use_model: bool = Field(False, description="Also ask the local model for what could be shown while each sentence is said.")
    place: Literal["none", "best"] = Field("none", description="best: put the best option of every suggestion over its sentence (muted overlays, fit cover), "
                                           "one undo step. Or place one yourself: timeline_edit add_overlay {media, start, length, src_in}.")


class ProjectGetArgs(BaseModel):
    project: str = ProjectId
    detail: Literal["outline", "full"] = Field("outline", description="outline: compact tracks/clips; full: the whole document (large).")
    track: str = Field("", max_length=40, description="Only this track (id, or kind: video|audio|text).")
    start: TimeVal = Field(None, description="Only clips that overlap from this time...")
    end: TimeVal = Field(None, description="...to this time (ms or '1:23.5').")
    max_clips: int = Field(40, ge=1, le=2000, description="Per track; longer tracks say how many clips were left out.")


class ProjectDeleteArgs(BaseModel):
    project: str = ProjectId
    confirm: bool = Field(False, description="Required. Deletes the project and its history (media and renders stay).")


class EditArgs(BaseModel):
    project: str = ProjectId
    ops: list[dict[str, Any]] = Field(..., min_length=1, max_length=500, description="Operations {op: ..., ...}: " + ", ".join(OP_NAMES) + ". Title over the start: {op: 'add_text', text, start: 0, "
                                  "length: 3000, style: {size, color, position: top|middle|bottom, animation: fade|pop|slide_up|typewriter}}. "
                                  "A wrong op or field returns every operation with its fields.")
    label: str = Field("", max_length=120, description="Name of the undo step.")
    base_rev: Optional[int] = Field(None, description="Fail if the project changed since this revision.")
    request_id: str = Field('', max_length=100, description='Optional unique edit key: retry identical ops/label/base_rev after an interrupted response without applying twice. A new edit needs a new key.')


class HistoryArgs(BaseModel):
    project: str = ProjectId
    action: Literal["list", "undo", "redo", "restore"] = "list"
    seq: Optional[int] = Field(None, description="restore: the history step to go back to.")


class CommandArgs(BaseModel):
    project: str = ProjectId
    command: Literal["remove_silences", "remove_fillers", "cut_words", "split_scenes", "reframe", "captions", "beat_sync", "match_loudness",
                     "script_assemble", "zoom_cuts", "speaker_cut", "multicam_create", "multicam_resync", "multicam_auto", "music_add"]
    args: dict[str, Any] = Field(default_factory=dict, description="See the command list in plan docs / presets_list.")
    preview: bool = Field(False, description="Report what would change without saving.")


class TimelineTranscriptArgs(BaseModel):
    project: str = ProjectId
    track: Optional[str] = None
    offset: int = Field(0, ge=0)
    limit: int = Field(600, ge=1, le=4000)


class TextCutArgs(BaseModel):
    project: str = ProjectId
    media: str = MediaId
    word_ids: list[str] = Field(default_factory=list, max_length=5000)
    phrase: str = Field("", max_length=500, description="Exact phrase to cut (every occurrence).")
    keep: bool = Field(False, description="true: keep ONLY these words (cut everything else of that media).")


class PlanCreateArgs(BaseModel):
    project: str = ProjectId
    instruction: str = Field(..., min_length=2, max_length=4000)
    use_model: bool = True


class PlanApplyArgs(BaseModel):
    plan: str = Field(..., min_length=1, max_length=40)
    steps: Optional[list[dict[str, Any]]] = Field(None, description="Edited steps (same shape as plan_create returned); omit to apply as is.")
    wait_s: float = Field(1800, ge=0, le=7200, description="How long the plan may wait for analyses it needs.")


class RenderArgs(BaseModel):
    project: str = ProjectId
    preset: str = Field("final", description="Export preset: " + ", ".join(runner.EXPORTS))
    start: TimeVal = None
    end: TimeVal = None
    filename: str = Field("", max_length=120)
    folder: str = Field("", max_length=2000, description="Output folder the user named; empty = the app's renders folder.")
    lufs: Optional[float] = Field(-999, description="Loudness target (-14 social/YouTube, -16 podcast); null = untouched; omit = preset.")
    subtitles: bool = Field(False, description="Also write an .srt next to the video.")
    mode: Literal["render", "copy"] = Field("render", description="copy: lossless cut at keyframes (one source, no effects).")
    formats: list[str | dict[str, Any]] = Field(default_factory=list, max_length=6, description="Several canvases in ONE job, one file each: "
                                                "['16:9','9:16','1:1'], presets (reels, youtube, square) or {aspect|width,height, reframe}. The project is not changed.")
    reframe: Literal["auto", "center", "blur"] = Field("auto", description="Framing where an output's shape differs from the project's: auto = the camera "
                                                       "path the clip has (else the subject analysis, else centred), center, blur = whole picture over a blurred fill.")
    captions_language: str = Field("", max_length=40, description="Burn the subtitles translated into this language (en, fr, 'inglés'); translates first if needed.")
    captions_dual: bool = Field(False, description="With captions_language: the original line and its translation under it.")


class JobArgs(BaseModel):
    job: Optional[str] = Field(None, description="Job id; omit to list the active jobs.")


class JobCancelArgs(BaseModel):
    job: str


class RendersArgs(BaseModel):
    project: Optional[str] = None


class FrameArgs(BaseModel):
    project: str = ProjectId
    t: float | int | str = Field(..., description="Timeline time (ms or '0:12.5').")
    width: int = Field(640, ge=64, le=3840)
    show: bool = Field(True, description="Also return the picture itself so it can be looked at (MCP image).")


class ContactSheetArgs(BaseModel):
    project: str = ProjectId
    mode: Literal["overview", "boundaries", "adaptive"] = Field("overview", description="Overview samples a uniform grid; boundaries samples before/at main-track clip starts; adaptive scans bounded low-resolution native composites for color/pixel change and keeps temporal endpoints. It is not source-scene detection.")
    count: int = Field(12, ge=2, le=16, description="Maximum frame count; odd boundary counts use complete pairs. Adaptive may return fewer frames on very short timelines.")
    width: int = Field(320, ge=128, le=640, description="Tile width in pixels; extreme aspect ratios fit into a height of at most 1280.")
    times: Optional[list[int]] = Field(None, min_length=1, max_length=16, description="Explicit timeline milliseconds inside duration; quantized to native output frames and deduplicated.")
    show: bool = Field(True, description="Return the JPEG as an MCP image; false returns text/artifact paths only.")


class SubtitlesArgs(BaseModel):
    project: str = ProjectId
    format: Literal["srt", "vtt", "ass"] = "srt"
    path: str = Field("", max_length=2000, description="Write to this file (in a folder the user named); empty = return the text.")
    language: str = Field("", max_length=40, description="Export the translation into this language (subtitles_translate first); empty = the original.")
    dual: bool = Field(False, description="With language: two lines, the original and its translation.")


class SubtitlesTranslateArgs(BaseModel):
    project: str = ProjectId
    language: str = Field("", max_length=40, description="Target language: en, fr, de, pt, it, ca... or its name ('inglés'). Needed except for status.")
    action: Literal["translate", "status", "show", "fix", "delete"] = Field("translate", description="translate: queue the job; status: which languages exist and "
                                                                         "whether they are up to date; show: read the translated cues; fix: correct cues; delete.")
    glossary: dict[str, str] = Field(default_factory=dict, description="Fixed translations {'term': 'translation'}.")
    keep: list[str] = Field(default_factory=list, max_length=80, description="Names and terms that must NOT be translated.")
    force: bool = Field(False, description="Translate again even when it is up to date.")
    wait_s: float = Field(0, ge=0, le=MAX_WAIT_S, description="Wait this long for the job and return its result (0 = return the job at once).")
    changes: list[dict[str, Any]] = Field(default_factory=list, max_length=500, description="fix: [{n: cue number, text: 'corrected'}].")
    offset: int = Field(0, ge=0)
    limit: int = Field(60, ge=1, le=300)


class MediaReceiveArgs(BaseModel):
    path: str = Field("", max_length=2000, description="A media file on this machine (inside the folders the app may read).")
    url: str = Field("", max_length=2000, description="Or file:// or http://127.0.0.1:port/... (an app on this machine serving the file).")
    name: str = Field("", max_length=200, description="Name in the library.")
    project: str = Field("", max_length=40, description="Add the media at the end of this project.")
    create_project: str | bool = Field(False, description="Or start a new project with it (true, or its name).")
    preset: str = Field("", max_length=30, description="Canvas of a new project (reels, youtube, square...); empty = by the media's shape.")
    transcribe: bool = Field(False, description="Queue the transcription.")
    source: str = Field("", max_length=80, description="The app that sends it.")


class ProjectFromTimelineArgs(BaseModel):
    title: str = Field("", max_length=120, description="Name of the new project (empty = the timeline's own name).")
    fcpxml_path: str = Field("", max_length=2000, description="An FCP7 XML (xmeml) timeline file, e.g. the one Prospero exports.")
    edl_path: str = Field("", max_length=2000, description="Or a CMX 3600 EDL file (clip files are found by name next to it or in media_dirs).")
    otio_path: str = Field("", max_length=2000, description="Or an OpenTimelineIO JSON file: tracks, gaps, transitions, markers and Lumiere metadata. Reports omissions and approximations.")
    plan: Optional[dict[str, Any] | str] = Field(None, description="Or a plan: {clips: [{path, in_s, out_s, track, start_s?}], markers: [{t, text}]} "
                                                 "(seconds; clips without start_s follow the previous one on their track).")
    fps: Optional[float] = Field(None, ge=1, le=240, description="Frame rate of an EDL (it carries none; 24 when empty) or of a plan (30).")
    media_dirs: list[str] = Field(default_factory=list, max_length=8, description="Folders to look for the clips' files by name (inside the allowed folders).")


class ShortArgs(MediaRef):
    start: float | int | str
    end: float | int | str
    name: str = Field("Corto", max_length=120)
    preset: Literal["reels", "shorts", "tiktok", "square", "portrait_4_5"] = "reels"
    captions: bool = True


class StabilizeArgs(BaseModel):
    project: str = ProjectId
    clip: str
    smoothing: int = Field(15, ge=1, le=60)


class FreezeArgs(BaseModel):
    project: str = ProjectId
    clip: str
    at: float | int | str
    length: float | int | str = 2000


class NestArgs(BaseModel):
    project: str = ProjectId
    action: Literal["nest", "unnest", "add", "list", "prepare"] = Field(
        "list", description="nest: clips -> one sequence clip (a new project); unnest: a sequence clip -> its clips; add: put another project "
                            "on this timeline; list: nested sequences here and where this project is used; prepare: render a sequence now.")
    clips: list[str] = Field(default_factory=list, max_length=2000, description="nest: the clips to move into the new project.")
    clip: str = Field("", max_length=40, description="unnest: the sequence clip.")
    name: str = Field("", max_length=120, description="nest: name of the new project.")
    sequence: str = Field("", max_length=40, description="add / prepare: the nested project (prj_...).")
    at: TimeVal = Field(None, description="add: timeline time; omitted = end of the main track.")
    track: str = Field("", max_length=40, description="add: the track (default the main track).")


class SettingsArgs(BaseModel):
    patch: dict[str, Any] = Field(default_factory=dict, description="Omit to read the settings.")


def _t(value: Any) -> Optional[int]:
    if value is None:
        return None
    return parse_time(value) if isinstance(value, str) else int(round(float(value)))


# ---------------- runners

def run_status(svc: Services, a: Empty) -> dict:
    return svc.status()


def run_media_import(svc: Services, a: MediaImportArgs) -> dict:
    if a.folder:
        return media_store.import_folder(svc, a.folder, recursive=a.recursive)
    if not a.path:
        raise LumiereError("Give a path or a folder.")
    m = media_store.import_path(svc, a.path)
    return {k: m[k] for k in ("id", "name", "kind", "duration_ms", "width", "height", "fps", "has_audio", "existing", "path")}


def run_media_shared(svc: Services, a: SharedMediaArgs) -> dict:
    result = fam_workspace.resolve(a.file_id)
    if not result.get("ok"):
        raise LumiereError(result.get("error") or "Atlas shared file is unavailable.")
    file = result.get("file", {})
    if not file.get("exists") or not file.get("live_file") or not file.get("path"):
        raise LumiereError("The shared original is missing. Restore its path before importing.")
    # The ordinary importer keeps the original path and enforces file_roots.
    # Atlas membership does not bypass Lumiere's local folder policy.
    media = media_store.open_shared(svc, file["path"], name=file.get("title", ""))
    return {**{k: media[k] for k in ("id", "name", "kind", "path", "existing")},
            "shared_file_id": a.file_id, "source_revision": file["revision"], "live_file": True,
            "refreshed": media.get("refreshed", False)}


def run_media_list(svc: Services, a: MediaListArgs) -> dict:
    items = media_store.list_media(svc, kind=a.kind, query=a.query, limit=a.limit)
    return {"media": [{"id": m["id"], "name": m["name"], "kind": m["kind"], "duration": ms_to_tc(m["duration_ms"]), "size": f"{m['width']}x{m['height']}",
                       "analysis": m["analysis"], "missing": m["missing"], "proxy": m["proxy"]} for m in items]}


def run_media_get(svc: Services, a: MediaRef) -> dict:
    m = media_store.get(svc, a.media)
    out = {k: v for k, v in m.items() if k != "urls"}
    out["duration"] = ms_to_tc(m["duration_ms"])
    out["analysis_summary"] = {k: analyze.summarize(k, media_store.get_analysis(svc, a.media, k) or {}) for k in m["analysis"] if k in analyze.KINDS}
    lv = media_store.get_analysis(svc, a.media, "levels")
    if lv:
        out["levels"] = lv
    out["jobs"] = [{"id": j["id"], "kind": j["kind"], "state": j["state"], "progress": j["progress"]} for j in svc.jobs.list(state="active", media_id=a.media)]
    return out


def run_media_delete(svc: Services, a: MediaDeleteArgs) -> dict:
    if not a.confirm:
        raise LumiereError("Removing a media needs confirm=true; ask the user first.", code="confirm_required")
    return media_store.delete(svc, a.media, force=a.force)


def run_media_analyze(svc: Services, a: AnalyzeArgs) -> dict:
    return {"media": a.media, "jobs": analyze.schedule(svc, a.media, list(a.kinds), force=a.force, language=a.language, model=a.model,
                                                       num_speakers=a.num_speakers, speakers=a.speakers, engine=a.engine)}


def run_media_silences(svc: Services, a: SilencesArgs) -> dict:
    s = analyze.silences_for(svc, a.media, threshold_db=a.threshold_db, min_silence_ms=a.min_silence_ms, margin_ms=a.margin_ms)
    s["ranges"] = [[x, y, f"{ms_to_tc(x)}–{ms_to_tc(y)}"] for x, y in s["ranges"][:400]]
    return s


def run_transcript_get(svc: Services, a: TranscriptArgs) -> dict:
    t = analyze.transcript(svc, a.media)
    if t is None:
        raise LumiereError("No transcript yet: run media_analyze with kinds=['transcript'].", code="no_transcript")
    words = t["words"][a.offset: a.offset + a.limit]
    out: dict[str, Any] = {"media": a.media, "language": t.get("language"), "total_words": len(t["words"]), "offset": a.offset}
    if a.format == "words":
        out["words"] = words
    else:
        lines: list[str] = []
        cur: list[dict[str, Any]] = []
        for w in words:
            cur.append(w)
            if w["text"][-1:] in ".?!" or len(cur) >= 18:
                lines.append(f"[{ms_to_tc(cur[0]['t0'])} {cur[0]['id']}] " + " ".join(x["text"] for x in cur))
                cur = []
        if cur:
            lines.append(f"[{ms_to_tc(cur[0]['t0'])} {cur[0]['id']}] " + " ".join(x["text"] for x in cur))
        out["text"] = "\n".join(lines)
    return out


def run_transcript_fix(svc: Services, a: TranscriptFixArgs) -> dict:
    return analyze.update_words(svc, a.media, a.changes)


def run_speakers_get(svc: Services, a: MediaRef) -> dict:
    return speakers.summary(svc, a.media)


def run_speakers_edit(svc: Services, a: SpeakersEditArgs) -> dict:
    if a.action == "diarize":
        return {"media": a.media, "jobs": analyze.schedule(svc, a.media, ["speakers"], force=True, num_speakers=a.num_speakers, engine=a.engine),
                "next": "When the job is done, read the result with speakers_get."}
    if a.action == "rename":
        if not a.names:
            raise LumiereError("rename needs names, e.g. {'S1': 'Ana'}.")
        return speakers.rename(svc, a.media, a.names)
    if a.action == "assign":
        if not a.speaker:
            raise LumiereError("assign needs speaker.")
        return speakers.assign(svc, a.media, a.speaker, word_ids=a.word_ids or None, from_ms=a.from_ms, to_ms=a.to_ms)
    if a.action == "merge":
        if not (a.source and a.into):
            raise LumiereError("merge needs source and into.")
        return speakers.merge(svc, a.media, a.source, a.into)
    return speakers.clear(svc, a.media)


def run_multicam_sync(svc: Services, a: MulticamSyncArgs) -> dict:
    return multicam.sync(svc, list(a.media), reference=a.reference, offsets=a.offsets or None)


def run_multicam_create(svc: Services, a: MulticamCreateArgs) -> dict:
    args: dict[str, Any] = {"media": list(a.media), "name": a.name, "offsets": a.offsets or None, "master": a.master, "angle": a.angle, "at": _t(a.at)}
    try:
        return commands.run(svc, a.project, "multicam_create", {k: v for k, v in args.items() if v is not None}, actor="agent")
    except commands.NeedsAnalysis as need:
        return {"done": False, "needs": need.jobs, "message": str(need)}


def run_multicam_switch(svc: Services, a: MulticamSwitchArgs) -> dict:
    op: dict[str, Any] = {"op": "multicam_switch"}
    for key, value in (("angle", a.angle), ("at", _t(a.at)), ("end", _t(a.end)), ("clip", a.clip), ("group", a.group), ("cuts", a.cuts)):
        if value is not None:
            op[key] = value
    return project_store.edit(svc, a.project, [op], label="Cambiar de cámara", actor="agent")


def run_multicam_auto(svc: Services, a: MulticamAutoArgs) -> dict:
    args: dict[str, Any] = {"group": a.group, "mode": a.mode, "min_shot_ms": a.min_shot_ms, "hysteresis_db": a.hysteresis_db, "dwell_ms": a.dwell_ms,
                            "lead_ms": a.lead_ms, "wide": a.wide, "speaker_map": a.speaker_map or None, "start": _t(a.start), "end": _t(a.end)}
    try:
        return commands.run(svc, a.project, "multicam_auto", {k: v for k, v in args.items() if v is not None}, actor="agent")
    except commands.NeedsAnalysis as need:
        return {"done": False, "needs": need.jobs, "message": str(need)}


def run_multicam_get(svc: Services, a: MulticamGetArgs) -> dict:
    return multicam.view(project_store.doc(svc, a.project), a.group)


def run_highlights(svc: Services, a: HighlightsArgs) -> dict:
    return commands.highlights(svc, a.media, count=a.count, length_ms=int(a.length_s * 1000), mode=a.mode)


def run_media_tag(svc: Services, a: MediaTagArgs) -> dict:
    m = media_store.set_tags(svc, a.media, a.tags)
    return {"media": m["id"], "name": m["name"], "tags": m["tags"]}


def run_project_create(svc: Services, a: ProjectCreateArgs) -> dict:
    if a.template:
        if a.media or a.from_project:
            raise LumiereError("With a template give slots (slot name -> media), not media or from_project.")
        v = project_store.create_from_template(svc, a.name, a.template, a.slots)
        return {**{k: v[k] for k in ("id", "name", "duration", "canvas", "clips", "rev")}, "template": v["template"], "unfilled": v["unfilled"],
                "filled": [{"slot": r["slot"], "media": r["media"], "start": ms_to_tc(r["start"]), "end": ms_to_tc(r["end"]), "rule": r["rule"]}
                           for r in v["filled"]]}
    if a.slots:
        raise LumiereError("slots only go with a template (see template_list).")
    v = project_store.create(svc, a.name, preset=a.preset, media=a.media or None, from_project=a.from_project)
    return {k: v[k] for k in ("id", "name", "duration", "canvas", "clips", "rev")}


def run_template_save(svc: Services, a: TemplateSaveArgs) -> dict:
    return project_store.save_template(svc, a.project, a.name, a.slots)


def run_template_list(svc: Services, a: Empty) -> dict:
    out = []
    for t in project_store.list_templates(svc):
        out.append({"id": t["id"], "name": t["name"], "duration": t["duration"], "canvas": t["canvas"],
                    "slots": [{"slot": s["slot"], "kind": s["track_kind"], "length": ms_to_tc(s["length_ms"]), "sample": s["name"], "rule": s["rule"]}
                              for s in t["slots"]]})
    return {"templates": out}


def run_music_pick(svc: Services, a: MusicPickArgs) -> dict:
    out = music_mod.suggest(svc, a.project, a.folder, recursive=a.recursive, count=a.count)
    if a.add:
        if a.add == "best":
            if not out["tracks"]:
                raise LumiereError("There is no track to add.")
            path = out["tracks"][0]["path"]
        else:
            path = a.add
        out["added"] = commands.run(svc, a.project, "music_add", {"path": path, "volume_db": a.volume_db}, actor="agent")["summary"]
    return out


def run_broll(svc: Services, a: BrollArgs) -> dict:
    try:
        return broll_mod.suggest(svc, a.project, start=_t(a.start), end=_t(a.end), per_sentence=a.per_sentence, use_model=a.use_model, place=a.place)
    except commands.NeedsAnalysis as need:
        return {"done": False, "needs": need.jobs, "message": str(need)}


def run_project_list(svc: Services, a: Empty) -> dict:
    return {"projects": project_store.list_projects(svc)}


def run_project_get(svc: Services, a: ProjectGetArgs) -> dict:
    if a.detail == "full":
        v = project_store.view(svc, a.project)
        return {k: v[k] for k in ("id", "name", "rev", "doc", "media", "issues", "can_undo", "can_redo")}
    out = project_store.outline(svc, a.project)
    out["rev"] = project_store.summary(svc, svc.db.one("SELECT * FROM projects WHERE id = ?", (a.project,)))["rev"]
    p = project_store.doc(svc, a.project)
    styles = {c.id: c.style for t in p.tracks for c in t.clips if c.type == "text" and c.style}
    lo, hi = _t(a.start), _t(a.end)
    tracks = []
    for t in out["tracks"]:
        if a.track and a.track not in (t["id"], t["kind"]):
            continue
        clips = [c for c in t["clips"] if (lo is None or c["end_ms"] > lo) and (hi is None or c["start_ms"] < hi)]
        for c in clips:
            if c["id"] in styles:  # titles: what an assistant needs to restyle them
                st = styles[c["id"]]
                c["style"] = {"position": st.position, "size": st.size, "color": st.color, "animation": st.animation}
        t = dict(t, clip_count=len(clips), clips=clips[: a.max_clips])
        if len(clips) > a.max_clips:
            t["more"] = f"{len(clips) - a.max_clips} more clips until {clips[-1]['end']}: ask with start/end or track"
        tracks.append(t)
    # titles and overlays first: they are few and usually what a request is about
    out["tracks"] = sorted(tracks, key=lambda t: {"text": 0, "audio": 2}.get(t["kind"], 1))
    return out


def run_project_delete(svc: Services, a: ProjectDeleteArgs) -> dict:
    if not a.confirm:
        raise LumiereError("Deleting a project needs confirm=true; ask the user first.", code="confirm_required")
    return project_store.delete(svc, a.project)


def run_edit(svc: Services, a: EditArgs) -> dict:
    return project_store.edit(svc, a.project, a.ops, label=a.label, actor="agent", base_rev=a.base_rev, request_id=a.request_id)


def run_history(svc: Services, a: HistoryArgs) -> dict:
    if a.action == "undo":
        return project_store.undo(svc, a.project)
    if a.action == "redo":
        return project_store.redo(svc, a.project)
    if a.action == "restore":
        if a.seq is None:
            raise LumiereError("restore needs seq (from action=list).")
        return project_store.restore(svc, a.project, a.seq)
    return project_store.history(svc, a.project)


def run_command(svc: Services, a: CommandArgs) -> dict:
    try:
        return commands.run(svc, a.project, a.command, a.args, actor="agent", preview=a.preview)
    except commands.NeedsAnalysis as need:
        return {"done": False, "needs": need.jobs, "message": str(need)}


def run_timeline_transcript(svc: Services, a: TimelineTranscriptArgs) -> dict:
    p = project_store.doc(svc, a.project)
    out = commands.timeline_transcript(svc, p, track=a.track)
    words = out["words"]
    out["total_words"] = len(words)
    out["words"] = [{k: v for k, v in {"id": w["id"], "media": w["media"], "text": w["text"], "t": ms_to_tc(w["t"]), "speaker": w.get("speaker_name")}.items()
                     if v is not None} for w in words[a.offset: a.offset + a.limit]]
    return out


def run_text_cut(svc: Services, a: TextCutArgs) -> dict:
    try:
        return commands.run(svc, a.project, "cut_words", {"media": a.media, "word_ids": a.word_ids or None, "text": a.phrase or None, "keep": a.keep},
                            actor="agent")
    except commands.NeedsAnalysis as need:
        return {"done": False, "needs": need.jobs, "message": str(need)}


def run_plan_create(svc: Services, a: PlanCreateArgs) -> dict:
    return plan_mod.create(svc, a.project, a.instruction, use_model=a.use_model)


def run_plan_apply(svc: Services, a: PlanApplyArgs) -> dict:
    if a.steps is not None:
        plan_mod.update(svc, a.plan, a.steps)
    plan = plan_mod.get(svc, a.plan)
    if plan["state"] != "draft":
        raise LumiereError(f"The plan is {plan['state']}.")
    job = svc.jobs.submit("plan_apply", {"plan": a.plan, "wait_s": a.wait_s}, label=f"Plan: {plan['instruction'][:60]}", project_id=plan["project"])
    return {"job": job["id"], "state": job["state"], "hint": "Follow it with job_status; the result lists the steps done and any renders queued."}


def run_render(svc: Services, a: RenderArgs) -> dict:
    lufs: Any = "default" if a.lufs == -999 else a.lufs
    job = svc.start_render(a.project, preset=a.preset, start=_t(a.start), end=_t(a.end), filename=a.filename, folder=a.folder, lufs=lufs,
                           subtitles=a.subtitles, mode=a.mode, formats=a.formats or None, reframe=a.reframe, captions_language=a.captions_language,
                           captions_dual=a.captions_dual)
    out = {"job": job["id"], "state": job["state"], "label": job["label"]}
    if a.formats:
        out["outputs"] = formats_mod.formats_info(list(a.formats), a.reframe)
    return out


def run_job_status(svc: Services, a: JobArgs) -> dict:
    if a.job:
        return svc.jobs.get(a.job)
    return {"active": svc.jobs.list(state="active"), "recent": svc.jobs.list(limit=10)}


def run_job_cancel(svc: Services, a: JobCancelArgs) -> dict:
    return svc.jobs.cancel(a.job)


def run_renders(svc: Services, a: RendersArgs) -> dict:
    return {"renders": runner.renders_list(svc, a.project)}


def run_frame(svc: Services, a: FrameArgs) -> dict:
    t = _t(a.t) or 0
    path = runner.render_frame(svc, a.project, t, width=a.width)
    out: dict = {"path": str(path), "t": ms_to_tc(t), "url": f"/api/frames/{path.name}", "shows": _frame_layers(svc, a.project, t)}
    if a.show:
        data = path.read_bytes()
        if a.width > 960:  # keep the picture light; the file on disk has the full size
            data = runner.render_frame(svc, a.project, t, width=960).read_bytes()
        out["_image"] = {"mime": "image/jpeg", "data": base64.b64encode(data).decode("ascii")}
    return out


def run_contact_sheet(svc: Services, a: ContactSheetArgs) -> dict:
    from .contact_sheet import create
    out = create(svc, a.project, mode=a.mode, count=a.count, width=a.width, times=a.times)
    if a.show:
        out["_image"] = {"mime": "image/jpeg", "data": base64.b64encode(Path(out["path"]).read_bytes()).decode("ascii")}
    return out


def _frame_layers(svc: Services, project: str, t: int) -> list[dict]:
    """What the project draws at t, top layer first, so an assistant looking at the frame knows which text comes from the
    edit (titles, burned-in captions) and which was already in the footage."""
    p = project_store.doc(svc, project)
    layers: list[dict] = []
    if p.captions.enabled:
        try:
            words = commands.timeline_transcript(svc, p)["words"]
        except Exception:  # noqa: BLE001 - no transcript yet: captions draw nothing
            words = []
        near = [w["text"] for w in words if abs(w["t"] - t) <= 1500]
        layers.append({"layer": "captions", "style": p.captions.style, "position": p.captions.position,
                       "text_near": " ".join(near)[:200], "note": "burned in by this project from the transcript"})
    for track in reversed(p.tracks):
        if track.hidden or track.kind == "audio":
            continue
        for c in track.clips:
            if c.start <= t < c.end:
                item: dict = {"layer": "text" if c.type == "text" else track.role, "track": track.id, "clip": c.id}
                if c.type == "text":
                    item.update(text=(c.text or "")[:200], position=c.style.position if c.style else None)
                elif c.type == "sequence":
                    info = svc.db.one("SELECT name FROM projects WHERE id = ?", (c.media,))
                    item.update(sequence=c.media, media=info["name"] if info else c.media)
                else:
                    info = svc.db.one("SELECT name FROM media WHERE id = ?", (c.media,))
                    item["media"] = info["name"] if info else c.media
                layers.append(item)
    return layers


def run_subtitles(svc: Services, a: SubtitlesArgs) -> dict:
    text, _ = runner.subtitles_export(svc, a.project, a.format, language=a.language, dual=a.dual)
    if a.path:
        dest = Path(a.path).expanduser()
        media_store._check_root(svc, dest.parent)
        if not dest.parent.is_dir():
            raise LumiereError(f"The folder {dest.parent} does not exist.")
        dest.write_text(text, encoding="utf-8")
        return {"path": str(dest), "bytes": len(text.encode())}
    return {"format": a.format, "language": a.language or None, "text": text}


def run_subtitles_translate(svc: Services, a: SubtitlesTranslateArgs) -> dict:
    if a.action == "status":
        return subtitles_mod.status(svc, a.project)
    if not a.language:
        raise LumiereError("Say the language (en, fr, 'inglés'...).", code="language_required")
    if a.action == "show":
        return subtitles_mod.show(svc, a.project, a.language, a.offset, a.limit)
    if a.action == "fix":
        if not a.changes:
            raise LumiereError("fix needs changes: [{n, text}].")
        return subtitles_mod.fix(svc, a.project, a.language, a.changes)
    if a.action == "delete":
        return subtitles_mod.delete(svc, a.project, a.language)
    job = subtitles_mod.start_translation(svc, a.project, a.language, glossary=a.glossary, keep=a.keep, force=a.force)
    if a.wait_s:
        job = svc.jobs.wait(job["id"], a.wait_s)
    out: dict[str, Any] = {"job": job["id"], "state": job["state"], "label": job["label"]}
    if job["state"] in ("done", "failed", "canceled"):
        out["result" if job["state"] == "done" else "error"] = job["result"] if job["state"] == "done" else job["error"]
    else:
        out["hint"] = "Follow it with job_status; then subtitles_export with language, or render_start with captions_language."
    return out


def run_media_receive(svc: Services, a: MediaReceiveArgs) -> dict:
    return family_events.receive_media(svc, path=a.path, url=a.url, name=a.name, project=a.project, create_project=a.create_project, preset=a.preset,
                                       transcribe=a.transcribe, source=a.source)


def run_project_from_timeline(svc: Services, a: ProjectFromTimelineArgs) -> dict:
    return timeline_import.project_from_timeline(svc, title=a.title, fcpxml_path=a.fcpxml_path, edl_path=a.edl_path, otio_path=a.otio_path, plan=a.plan, fps=a.fps,
                                                 media_dirs=a.media_dirs, actor="agent")


class ProjectExportOtioArgs(BaseModel):
    project: str = ProjectId


def run_project_export_otio(svc: Services, a: ProjectExportOtioArgs) -> dict:
    from .interchange import export_file
    return export_file(svc, a.project)


def run_short(svc: Services, a: ShortArgs) -> dict:
    return commands.short_from_range(svc, a.media, _t(a.start) or 0, _t(a.end) or 0, name=a.name, preset=a.preset, with_captions=a.captions)


def run_stabilize(svc: Services, a: StabilizeArgs) -> dict:
    project_store.doc(svc, a.project).find(a.clip)
    job = svc.jobs.submit("stabilize", {"project": a.project, "clip": a.clip, "smoothing": a.smoothing}, label="Estabilizar clip", project_id=a.project)
    return {"job": job["id"], "state": job["state"]}


def run_freeze(svc: Services, a: FreezeArgs) -> dict:
    return derive.freeze_frame(svc, a.project, a.clip, _t(a.at) or 0, _t(a.length) or 2000)


def run_presets(svc: Services, a: Empty) -> dict:
    return {"canvas_presets": PRESETS, "export_presets": runner.export_presets(), "transitions": list(TRANSITIONS),
            "effects": {k: {p: v[0] for p, v in spec.items()} for k, spec in fx.SPECS.items()},
            "caption_styles": ["clean", "bold", "karaoke", "pop", "boxed", "minimal"],
            "multi_formats": [{"id": a, "width": w, "height": h} for a, (w, h) in formats_mod.ASPECT_SIZES.items()], "reframe_modes": list(formats_mod.REFRAMES),
            "subtitle_languages": {c: n for c, (n, _) in subtitles_mod.LANGUAGES.items()}, "family": family_events.contract(), "operations": OP_NAMES, "operation_fields": op_reference().split("\n"), "commands": plan_mod.COMMAND_DOCS,
            "speed_curves": list(RAMP_PRESETS), "mask_shapes": ["rectangle", "rounded", "ellipse"]}


def run_nest(svc: Services, a: NestArgs) -> dict:
    from .render import sequences

    if a.action == "nest":
        if not a.clips:
            raise LumiereError("nest needs clips (ids from project_get).")
        return project_store.nest(svc, a.project, a.clips, a.name, actor="agent")
    if a.action == "unnest":
        if not a.clip:
            raise LumiereError("unnest needs the sequence clip id.")
        return project_store.edit(svc, a.project, [{"op": "unnest", "clip": a.clip}], label="Desanidar", actor="agent")
    if a.action == "add":
        if not a.sequence:
            raise LumiereError("add needs sequence (the project id to nest).")
        op: dict[str, Any] = {"op": "add_sequence", "project": a.sequence}
        if a.at is not None:
            op.update(at=a.at, mode="overwrite")
        if a.track:
            op["track"] = a.track
        return project_store.edit(svc, a.project, [op], label="Añadir secuencia", actor="agent")
    if a.action == "prepare":
        target = a.sequence or a.project
        job = svc.jobs.submit("sequence", {"project": target}, label="Secuencia anidada", project_id=target)
        return {"job": job["id"], "state": job["state"]}
    p = project_store.doc(svc, a.project)
    look = project_store.media_lookup(svc)
    items = []
    for sid in sorted(p.sequence_ids()):
        info = look(sid) or {}
        clips = [{"clip": c.id, "start": ms_to_tc(c.start), "end": ms_to_tc(c.end)} for _, c in p.all_clips() if c.media == sid]
        items.append({"project": sid, "name": info.get("name"), "duration": ms_to_tc(info.get("duration_ms") or 0),
                      "ready": sequences.cached(svc, sid) is not None, "clips": clips})
    return {"project": a.project, "sequences": items, "used_by": project_store.used_by(svc, a.project)}


def run_settings(svc: Services, a: SettingsArgs) -> dict:
    return svc.update_settings(a.patch) if a.patch else svc.get_settings()


class CreativeToolsArgs(BaseModel):
    engine: Literal['motion','edit']


class CreativeListArgs(BaseModel):
    engine: Optional[Literal['motion','edit']] = None
    project_id: Optional[str] = None


class CreativeCallArgs(BaseModel):
    creative_id: str = Field(...,min_length=32,max_length=32)
    calls: list[dict[str,Any]] = Field(...,min_length=1,max_length=32)


class CreativeRenderArgs(BaseModel):
    creative_id: str = Field(...,pattern=r'^[0-9a-f]{32}$')


class CreativeTitleArgs(BaseModel):
    text: str = Field(...,min_length=1,max_length=300)
    width: int = Field(1280,ge=16,le=8192)
    height: int = Field(720,ge=16,le=8192)
    fps: float = Field(24,ge=1,le=120)
    duration: float = Field(3,ge=.5,le=30)
    project_id: Optional[str] = None


class CreativeFilmArgs(BaseModel):
    media_id: str = MediaId
    project_id: Optional[str] = None
    width: Optional[int] = Field(None,ge=16,le=8192)
    height: Optional[int] = Field(None,ge=16,le=8192)
    fps: Optional[float] = Field(None,ge=1,le=120)


def _creative(svc: Services):
    from .creative_engines import CreativeEngines
    return CreativeEngines(svc.config.data_dir,port=svc.config.port)


def run_creative_tools(svc: Services,a: CreativeToolsArgs):
    return _creative(svc).tools(a.engine)


def run_creative_list(svc: Services,a: CreativeListArgs):
    return _creative(svc).list(a.engine,a.project_id)


def run_creative_get(svc: Services,a: CreativeRenderArgs):
    return _creative(svc).get(a.creative_id)


def run_creative_call(svc: Services,a: CreativeCallArgs):
    import base64
    engines=_creative(svc)
    result=engines.call(a.creative_id,a.calls)
    folder=engines._project_dir(a.creative_id)
    for call in reversed(result.get('calls',[])):
        for block in reversed(call.get('content',[])):
            if block.get('type')=='image' and block.get('path'):
                path=Path(block['path']).resolve()
                if path.is_relative_to(folder) and path.is_file():
                    result['_image']={'mime':'image/png','data':base64.b64encode(path.read_bytes()).decode('ascii')}
                    return result
    return result


def run_creative_render(svc: Services,a: CreativeRenderArgs):
    return _creative(svc).render_title_video(a.creative_id)


def run_creative_title(svc: Services,a: CreativeTitleArgs):
    if a.project_id:project_store.doc(svc,a.project_id)
    return _creative(svc).create_title_card(**a.model_dump())


def run_creative_film(svc: Services,a: CreativeFilmArgs):
    if a.project_id:project_store.doc(svc,a.project_id)
    info=media_store.get(svc,a.media_id)
    if info['kind']!='video' or not info['has_video']:raise LumiereError('Lumiere Edit needs a video source.')
    return _creative(svc).create_film_sequence(Path(info['path']),source_media_id=a.media_id,width=a.width or info['width'],
                                              height=a.height or info['height'],fps=a.fps or info['fps'],project_id=a.project_id)


from .html_motion import HtmlMotion
from .html_motion_args import (HtmlMotionSaveArgs, HtmlMotionRefArgs, HtmlMotionGetArgs,
                               HtmlMotionFrameArgs, HtmlMotionRestoreArgs, HtmlMotionStoryboardArgs)


def run_html_motion_list(svc: Services, a: Empty):
    return HtmlMotion(svc.config.data_dir).list()


from .html_starters import HtmlStarterArgs, catalogue as starters_catalogue, save_args as starter_source


def run_html_motion_starters(svc: Services, a: Empty):
    return starters_catalogue()


def run_html_motion_starter_create(svc: Services, a: HtmlStarterArgs):
    return run_html_motion_save(svc, starter_source(a))


def run_html_motion_save(svc: Services, a: HtmlMotionSaveArgs):
    if a.project_id:
        project_store.doc(svc, a.project_id)
    return HtmlMotion(svc.config.data_dir).save(**a.model_dump())


def run_html_motion_get(svc: Services, a: HtmlMotionGetArgs):
    store = HtmlMotion(svc.config.data_dir)
    manifest = store.get(a.composition, a.revision)
    source = store.source(manifest)
    end = min(len(source), a.offset + a.limit)
    return {**manifest, "html": source[a.offset:end], "source_chars": len(source),
            "offset": a.offset, "next_offset": end if end < len(source) else None}


def run_html_motion_frame(svc: Services, a: HtmlMotionFrameArgs):
    data, result = HtmlMotion(svc.config.data_dir).frame(a.composition, a.at, a.revision)
    manifest = HtmlMotion(svc.config.data_dir).get(a.composition, result["revision"])
    result["url"] = manifest["frame_url"] + f"?at={a.at}"
    if a.show:
        result["_image"] = {"mime": "image/png", "data": base64.b64encode(data).decode("ascii")}
    return result


def run_html_motion_render(svc: Services, a: HtmlMotionRefArgs):
    return HtmlMotion(svc.config.data_dir).queue_render(svc, a.composition, a.revision)


def run_html_motion_storyboard(svc: Services, a: HtmlMotionStoryboardArgs):
    store = HtmlMotion(svc.config.data_dir)
    result = store.storyboard(a.composition, a.times, a.revision)
    if a.show:
        data = store.storyboard_image(a.composition, result['revision'], result['id'])
        result['_image'] = {'mime':'image/png','data':base64.b64encode(data).decode('ascii')}
    return result


def run_html_motion_restore(svc: Services, a: HtmlMotionRestoreArgs):
    return HtmlMotion(svc.config.data_dir).restore(**a.model_dump())


TOOLS: list[Tool] = [
    Tool("html_motion_starters", "List own editable motion starters. Escenas de partida.\nFour distinct sources: kinetic title, layered landscape, artistic oscillators and elastic lettering. Use html_motion_starter_create, then edit the ordinary HTML source and review moments. Keywords: motion, plantilla, parallax, letras, spring.", Empty, _ann(True), run_html_motion_starters),
    Tool("html_motion_starter_create", "Create a new composition from an own motion starter. Crear escena editable.\nLiteral text, duration and dimensions; every frame is a pure function of time. Creates a separate composition, leaving earlier sources intact. Use existing frame/storyboard/render/get/save tools and the ordinary workshop. No external assets or runtime. Keywords: escena, title, parallax, oscillators, elastic lettering.", HtmlStarterArgs, _ann(False, False, False), run_html_motion_starter_create),
    Tool("html_motion_save", "Create or edit an HTML composition with exact-time window.seek(t). Crear y editar animacion HTML.\nReturns an editable revision and source hash. Keywords: motion graphics, canvas, SVG, HTML video, seek.", HtmlMotionSaveArgs, _ann(False, False, False), run_html_motion_save),
    Tool("html_motion_list", "List saved HTML motion compositions and browser runtime. Listar animaciones HTML.\nKeywords: compositions, motion graphics, browser renderer.", Empty, _ann(True), run_html_motion_list),
    Tool("html_motion_get", "Read an HTML composition and its editable source by character pages. Leer fuente HTML.\nUse next_offset for the complete source. Keywords: source, revision, composition, motion graphics.", HtmlMotionGetArgs, _ann(True), run_html_motion_get),
    Tool("html_motion_frame", "Render an exact-time PNG from an HTML composition. Revisar fotograma HTML.\nTime is seconds; returns an MCP image, source revision and frame hash. Keywords: snapshot, visual critique, storyboard, seek.", HtmlMotionFrameArgs, _ann(True), run_html_motion_frame),
    Tool('html_motion_storyboard', 'Review multiple exact-time frames from one HTML revision. Revisar varios momentos.\nReturns a real contact-sheet MCP image, original PNG links, times and source/frame hashes. Keywords: storyboard, contact sheet, motion review, visual critique.', HtmlMotionStoryboardArgs, _ann(True), run_html_motion_storyboard),
    Tool("html_motion_render", "Queue an HTML composition as verified silent H264 MP4. Exportar animacion HTML a video.\nFollow job_status; use media_receive(path=result.render_path) to add the video to a timeline. Keywords: render, export, MP4, motion graphics.", HtmlMotionRefArgs, _ann(False, False, True), run_html_motion_render),
    Tool("html_motion_restore", "Restore a saved HTML composition revision as a new revision. Recuperar una animacion anterior.\nLater versions remain available. Keywords: undo, restore, revisions, HTML motion.", HtmlMotionRestoreArgs, _ann(False, False, False), run_html_motion_restore),
    Tool('creative_render_title_video','Render an editable Lumiere Motion title to verified H264 video. Renderizar cartela animada.\nReturns actual video path, metadata and media_receive_url. Keywords: animation, render title, mp4, motion graphics.',CreativeRenderArgs,_ann(False,False,True),run_creative_render),
    Tool('creative_tools','Discover Lumiere motion or editing operations. Herramientas nativas.\nKeywords: motion graphics, layers, compositing, editing, effects.',CreativeToolsArgs,_ann(True),run_creative_tools),
    Tool('creative_list','List saved editable creative projects. Listar composiciones guardadas.\nFilter by own engine or linked timeline. Keywords: composition, saved, project library, motion, edit.',CreativeListArgs,_ann(True),run_creative_list),
    Tool('creative_get','Read a saved creative project and its artifacts. Abrir composicion guardada.\nHistorical source files stay intact; edits use the own-format copy. Keywords: project, preview, video, native source.',CreativeRenderArgs,_ann(True),run_creative_get),
    Tool('creative_call','Run native MCP calls on a saved creative project. Ejecutar herramientas nativas.\nEach batch reopens/saves; in-memory selection and undo do not persist between batches.\nKeywords: effects, animation, layers, native tools.',CreativeCallArgs,_ann(False,True,False),run_creative_call),
    Tool('creative_title_card','Create an editable Lumiere Motion title with keyframes and PNG preview. Cartela editable.\nNo encoded video render is implied. Keywords: title, keyframes, text animation.',CreativeTitleArgs,_ann(False,True,False),run_creative_title),
    Tool('creative_film_sequence','Create a Lumiere Edit sequence from a video copy and export H264 MP4. Secuencia nativa.\nThe original source is preserved. Keywords: native video editor, sequence, video export.',CreativeFilmArgs,_ann(False,True,False),run_creative_film),
    Tool("lumiere_status", "Video editor status: ffmpeg, GPU encoder, speech model, jobs. Estado del editor de vídeo.\n"
         "Sinónimos: estado, capacidades, qué puede hacer.\nKeywords: status, video editor, editor de vídeo.", Empty, _ann(True), run_status),
    Tool("media_import", "Import a video, audio or image file (or a folder) by path; nothing is copied. Importar vídeo.\n"
         "Makes a proxy, filmstrip and waveform in the background. Sinónimos: añadir vídeo, cargar, abrir archivo.\nKeywords: import, add media, video file.",
         MediaImportArgs, _ann(False, False, True), run_media_import),
    Tool("media_list", "List the media library (videos, audios, images) with analyses done. Biblioteca de medios.\n"
         "Sinónimos: mis vídeos, archivos importados.\nKeywords: media, library, list.", MediaListArgs, _ann(True), run_media_list),
    Tool("media_shared", "Open an Atlas shared original without copying it. Abrir un archivo compartido de Atlas.\n"
         "Uses the same live path as other project members; local folder restrictions still apply.\n"
         "Keywords: Atlas, shared file, common storage, archivo compartido.", SharedMediaArgs, _ann(False, False, True), run_media_shared),
    Tool("media_get", "One media in detail: size, length, streams, levels, analyses and running jobs. Ver medio.\n"
         "Keywords: media info, probe.", MediaRef, _ann(True), run_media_get),
    Tool("media_delete", "Remove a media from the library (needs confirm=true). Quitar de la biblioteca.\n"
         "The original file stays. Keywords: delete media.", MediaDeleteArgs, _ann(False, True, True), run_media_delete),
    Tool("media_analyze", "Run analyses on a media: transcript, speakers, scenes, beats, loudness, motion, focus. Analizar vídeo.\n"
         "Background jobs; results are cached. kinds=['speakers'] tells who says each word. Sinónimos: transcribir, detectar escenas, ritmo, separar hablantes, quién habla.\n"
         "Keywords: analyze, transcribe, whisper, scenes, beats, speakers, diarization.",
         AnalyzeArgs, _ann(False, False, True), run_media_analyze),
    Tool("media_silences", "Silent ranges of a media (automatic threshold from the noise floor). Silencios.\n"
         "Preview before remove_silences. Keywords: silence, pauses, dead air.", SilencesArgs, _ann(True), run_media_silences),
    Tool("transcript_get", "Read a media's transcript (text with times and word ids, or words). Transcripción.\n"
         "Sinónimos: qué dice, texto del vídeo.\nKeywords: transcript, words, speech.", TranscriptArgs, _ann(True), run_transcript_get),
    Tool("transcript_fix", "Correct transcript words (captions use the corrected text). Corregir transcripción.\n"
         "Keywords: fix transcript, typo, captions text.", TranscriptFixArgs, _ann(False, False, True), run_transcript_fix),
    Tool("speakers_get", "Who speaks in a media: speakers with names, colours, talk time and turns. Hablantes.\n"
         "Needs media_analyze kinds=['speakers']. Sinónimos: quién habla, interlocutores, voces.\nKeywords: speakers, diarization, who speaks, voices.",
         MediaRef, _ann(True), run_speakers_get),
    Tool("speakers_edit", "Separate voices again, rename speakers (S1 -> Ana), fix who said what, merge or clear. Editar hablantes.\n"
         "Captions and text cuts then use the names. Sinónimos: renombrar hablante, cambiar de quién es la frase, juntar voces.\n"
         "Keywords: rename speaker, diarize, reassign speaker, merge speakers.", SpeakersEditArgs, _ann(False, False, False), run_speakers_edit),
    Tool("multicam_sync", "Measure the time offset between recordings of one event by their sound. Sincronizar cámaras.\n"
         "Reports confidence per recording; nothing is edited. Sinónimos: sincronizar audio, desfase entre cámaras.\nKeywords: multicam, sync, offset, cross-correlation.",
         MulticamSyncArgs, _ann(True), run_multicam_sync),
    Tool("multicam_create", "Group 2+ recordings of one event into a synced multicam clip with one master sound. Crear multicámara.\n"
         "Syncs by audio (manual offsets allowed). Sinónimos: multicámara, varias cámaras, cámara A y B.\nKeywords: multicam, multi-camera, angles, sync.",
         MulticamCreateArgs, _ann(False), run_multicam_create),
    Tool("multicam_switch", "Show another camera angle at a time, for a range or a whole shot of a multicam group. Cambiar de cámara.\n"
         "The sound never changes. Sinónimos: cortar a la cámara B, cambiar ángulo, plano.\nKeywords: switch angle, cut to camera, multicam.",
         MulticamSwitchArgs, _ann(False, True, False), run_multicam_switch),
    Tool("multicam_auto", "Cut between the cameras automatically by who is speaking (loudness or speakers). Cambio automático.\n"
         "Minimum shot length, hysteresis, optional wide angle. Sinónimos: cambio automático por quién habla, montaje de entrevista.\n"
         "Keywords: auto switch, active speaker, multicam, interview.", MulticamAutoArgs, _ann(False, True, False), run_multicam_auto),
    Tool("multicam_get", "Read a project's multicam groups: angles, sync confidence, master sound, current shots. Ver multicámara.\n"
         "Keywords: multicam, angles, shots.", MulticamGetArgs, _ann(True), run_multicam_get),
    Tool("highlights_find", "Find the best moments of a long video (sound, motion, speech; mode=model reads it). Mejores momentos.\n"
         "mode=model asks the local model for hooks, punchlines and complete thoughts and merges them with the signals, with reasons.\n"
         "Sinónimos: highlights, clips virales, momentos clave, gameplay.\nKeywords: highlights, best moments, clips, hook, punchline.", HighlightsArgs, _ann(True), run_highlights),
    Tool("media_tag", "Label a media with keywords (replaces its tags); b-roll suggestions match them. Etiquetar medio.\n"
         "Sinónimos: etiquetas, palabras clave, describir clip.\nKeywords: tags, labels, keywords, b-roll library.", MediaTagArgs, _ann(False, False, True), run_media_tag),
    Tool("short_from_range", "Make a vertical short (reframed, captions) from one range of a media. Crear corto vertical.\n"
         "Sinónimos: reel, tiktok, short desde un momento.\nKeywords: short, reel, vertical clip.", ShortArgs, _ann(False), run_short),
    Tool("project_create", "Create an editing project (canvas preset and media, or a template with slots). Nuevo proyecto de vídeo.\n"
         "Presets: reels, youtube, square... With template + slots {name: media} it fills a saved template (template_list).\n"
         "Keywords: new project, timeline, from template, slots.", ProjectCreateArgs, _ann(False), run_project_create),
    Tool("template_save", "Save a project as a template with replaceable slots (intro, main, outro...). Guardar plantilla.\n"
         "Titles, captions, music and effects stay; slot clips are replaced later with project_create template+slots.\n"
         "Sinónimos: plantilla, formato reutilizable, intro y outro fijos.\nKeywords: template, slots, reusable edit.", TemplateSaveArgs, _ann(False), run_template_save),
    Tool("template_list", "List the project templates with their slots. Plantillas de vídeo.\n"
         "Keywords: templates, slots, reusable.", Empty, _ann(True), run_template_list),
    Tool("project_list", "List the editing projects. Proyectos de vídeo.\nKeywords: projects, list.", Empty, _ann(True), run_project_list),
    Tool("project_get", "Read a project's timeline: tracks, clips with ids and times, captions, issues. Ver timeline.\n"
         "Titles first; filter by track or time on long timelines. Keywords: timeline, outline, clips, tracks, titles.", ProjectGetArgs, _ann(True), run_project_get),
    Tool("project_delete", "Delete a project and its history (needs confirm=true). Borrar proyecto.\nKeywords: delete project.",
         ProjectDeleteArgs, _ann(False, True, True), run_project_delete),
    Tool("timeline_edit", "Edit the timeline with operations (split, trim, move, delete, titles, speed...). Editar timeline.\n"
         "All or nothing, one undo step. Speed curves (speed_ramp), shape masks (mask), nested sequences (add_sequence, unnest).\n"
         "Animation (keyframes {clip, prop, keys: [{t, v, ease}]}): x, y, scale, opacity, rotation, volume_db, mask_*, and the clip's colour effect: brightness (-1..1), saturation (0..3).\n"
         "Sinónimos: cortar, recortar, mover, añadir texto, título, rótulo, rampa de velocidad, cámara lenta, máscara.\n"
         "Use request_id for interrupted-response retries: identical ops/label/base_rev recover original IDs without applying twice.\n"
         "Keywords: edit, cut, trim, split, title, text overlay, ops, speed ramp, slow motion, mask, retry, reintentar.",
         EditArgs, _ann(False, True, False), run_edit),
    Tool("timeline_history", "Undo, redo, list or restore history steps of a project. Deshacer.\n"
         "Sinónimos: deshacer, rehacer, historial, volver atrás.\nKeywords: undo, redo, history.", HistoryArgs, _ann(False, False, False), run_history),
    Tool("edit_command", "Smart edits: remove_silences, remove_fillers, reframe, captions, beat_sync... Edición inteligente.\n"
         "Sinónimos: quitar silencios, muletillas, vertical, subtítulos, al ritmo.\nKeywords: jump cut, captions, reframe, beat sync.",
         CommandArgs, _ann(False, True, False), run_command),
    Tool("timeline_transcript", "Words heard on the timeline, in order, with ids: the text editor view. Texto del montaje.\n"
         "Keywords: transcript, text-based editing.", TimelineTranscriptArgs, _ann(True), run_timeline_transcript),
    Tool("text_cut", "Text-based editing: cut words or a phrase (or keep only them) from the video. Editar por texto.\n"
         "Sinónimos: borrar frase, quitar lo que dice, quedarse con.\nKeywords: text edit, cut words, transcript edit.",
         TextCutArgs, _ann(False, True, False), run_text_cut),
    Tool("plan_create", "Turn the user's words into an editable edit plan (nothing changes yet). Plan de edición.\n"
         "Sinónimos: edítalo así, haz que, instrucciones.\nKeywords: plan, natural language edit, instructions.", PlanCreateArgs, _ann(False), run_plan_create),
    Tool("plan_apply", "Apply a plan (optionally edited) as one undo step; exports in it are queued. Aplicar plan.\n"
         "Runs in the background. Keywords: apply plan, run.", PlanApplyArgs, _ann(False, True, False), run_plan_apply),
    Tool("music_pick", "Rank the audio files of a folder as background music for an edit (tempo, length, energy). Elegir música.\n"
         "Fits the edit's length and cuts per minute, with reasons; add='best' puts it under the edit, trimmed, faded, ducking.\n"
         "Sinónimos: música de fondo, banda sonora, canción para el vídeo.\nKeywords: background music, bpm, tempo, soundtrack, ducking.",
         MusicPickArgs, _ann(False), run_music_pick),
    Tool("broll_suggest", "Suggest library clips as b-roll for each sentence (or a range) of the edit. Sugerir b-roll.\n"
         "Matches what clips say, their names and tags; place='best' puts them as muted overlays; add_overlay op places one.\n"
         "Sinónimos: planos de recurso, imágenes de apoyo, cubrir con vídeo.\nKeywords: b-roll, cutaway, overlay, stock footage, keywords.",
         BrollArgs, _ann(False), run_broll),
    Tool("video_compress", "Compress a local video under a maximum MB size, preserving the original. Comprimir vídeo.\n"
         "Two-pass H.264, same picture size (odd dimensions padded by one pixel), first audio track; AAC audio is copied unchanged. "
         "A job with progress, cancellation and a checked byte limit. max_mb uses decimal MB. No loudness change. "
         "Keywords: compress, smaller video, 25 MB, file size limit.", compression.CompressArgs, _ann(False),
         lambda s, a: compression.start(s, **a.model_dump())),
    Tool("render_start", "Export the project to a video file (MP4, HEVC, ProRes, GIF, MP3...). Exportar vídeo.\n"
         "GPU encoder when available. Sinónimos: renderizar, sacar el vídeo, descargar, varios formatos a la vez, 16:9 y 9:16, subtítulos traducidos.\n"
         "Keywords: render, export, mp4, multi-format, aspect ratios, burned translated captions.", RenderArgs, _ann(False), run_render),
    Tool("job_status", "State and progress of a background job (or the active ones). Estado de tareas.\n"
         "Keywords: job, progress, render status.", JobArgs, _ann(True), run_job_status),
    Tool("job_cancel", "Cancel a background job (render, transcription, analysis). Cancelar tarea.\nKeywords: cancel, stop.",
         JobCancelArgs, _ann(False, False, True), run_job_cancel),
    Tool("renders_list", "Exported files with their quality check (duration, size, loudness). Exportaciones.\n"
         "Keywords: renders, exports, output files.", RendersArgs, _ann(True), run_renders),
    Tool("frame_snapshot", "Save the frame the timeline shows at a time as an image (exactly as rendered). Fotograma.\n"
         "Use it to look at the edit. Keywords: frame, snapshot, preview image.", FrameArgs, _ann(False, False, True), run_frame),
    Tool("project_contact_sheet", "Export timecoded native timeline frames in one contact sheet. Hoja de fotogramas del montaje.\n"
         "Overview samples a uniform grid; boundaries samples before/at main-track clip starts. Adaptive scans at most 120 low-resolution native composites (target 4/s), selects color/pixel changes plus endpoints and spread coverage; it is not source-scene detection. Explicit milliseconds snap to output frames. Returns JPEG/PNG, portable HTML and JSON receipt with scores, provenance, hashes and project revision. "
         "Inspect image/receipt; this does not prove motion or audio continuity. Sinónimos: revisar cortes, hoja de contacto, storyboard.\n"
         "Keywords: contact sheet, review cuts, timeline overview, provenance, hoja de fotogramas, revisar montaje.",
         ContactSheetArgs, _ann(False, False, False), run_contact_sheet),
    Tool("subtitles_export", "Export the captions as SRT, VTT or ASS (to a file or as text). Exportar subtítulos.\n"
         "Keywords: srt, vtt, subtitles file.", SubtitlesArgs, _ann(False, False, True), run_subtitles),
    Tool("subtitles_translate", "Translate the project's subtitles with the local model (keeps the timing). Traducir subtítulos.\n"
         "Background job; stored per language. Sinónimos: subtítulos en inglés, traducción, doblar subtítulos, subtítulos bilingües.\n"
         "Keywords: translate subtitles, translation, srt, language, glossary, dual language.", SubtitlesTranslateArgs, _ann(False, False, True), run_subtitles_translate),
    Tool("media_receive", "Receive a media file from another app (path or local URL), optionally into a project. Recibir medio.\n"
         "Family import: used by sibling apps through the hub. Sinónimos: importar desde otra app, pasar vídeo, enviar a Lumiere.\n"
         "Keywords: receive media, family, hub, import from app, send to editor.", MediaReceiveArgs, _ann(False, False, True), run_media_receive),
    Tool("project_export_otio", "Export a multitrack montage as OpenTimelineIO. Exportar montaje OTIO.\n"
         "Standard clips, tracks, gaps, dissolves and markers, with explicit source-sound audio tracks and lossless stereo files. "
         "Returns native counts, OTIO counts, audio_sources and a real file/report. Untouched imports coalesce source sound; external audio edits stay independent. "
         "Nested sequences are not supported. Keywords: otio, export timeline, interchange, editable montage.",
         ProjectExportOtioArgs, _ann(False, False, True), run_project_export_otio),
    Tool("project_from_timeline", "Open an edit made elsewhere (FCP7 XML, EDL, OTIO or plan) as a project. Proyecto desde línea de tiempo.\n"
         "Reads Prospero's XML/EDL export or a plan of clips and markers; missing files are skipped and listed. Sinónimos: importar montaje, importar XML, "
         "importar EDL, abrir corte, llevar a Lumiere. OTIO returns a per-item loss/rounding report.\nKeywords: import timeline, otio, fcpxml, xmeml, edl, plan, project from timeline, family.",
         ProjectFromTimelineArgs, _ann(False, False, False), run_project_from_timeline),
    Tool("clip_stabilize", "Stabilize a shaky clip (background job; the clip is pointed at the stable copy). Estabilizar.\n"
         "Keywords: stabilize, shaky, gimbal.", StabilizeArgs, _ann(False), run_stabilize),
    Tool("clip_freeze", "Insert a freeze frame of a clip at a time. Congelar imagen.\nKeywords: freeze frame, still.", FreezeArgs, _ann(False), run_freeze),
    Tool("timeline_nest", "Nested sequences: nest clips into a new project, un-nest, add a project as a clip, list. Anidar.\n"
         "Sinónimos: anidar, secuencia anidada, agrupar clips, desanidar, compound clip.\nKeywords: nest, nested sequence, compound, group clips.",
         NestArgs, _ann(False, False, False), run_nest),
    Tool("presets_list", "Canvas and export presets, transitions, effects, caption styles, operations. Opciones.\n"
         "Keywords: presets, effects, transitions, vocabulary.", Empty, _ann(True), run_presets),
    Tool("settings", "Read or change settings (speech model, language, GPU decoding, export folder). Ajustes.\n"
         "Keywords: settings, whisper model, config.", SettingsArgs, _ann(False, False, True), run_settings),
]



def _accountable(tool: Tool) -> Tool:
    """Attach the capture / track / undo hooks and the draft-safe flag (see agent_undo.py)."""
    hooks = agent_undo.HOOKS.get(tool.name, {})
    changes: dict[str, Any] = dict(hooks)
    if tool.name in agent_undo.DRAFT_SAFE:
        changes["annotations"] = {**tool.annotations, "draftSafeHint": True}
    return dataclasses.replace(tool, **changes) if changes else tool


TOOLS = [_accountable(t) for t in TOOLS]

TOOLS_BY_NAME = {t.name: t for t in TOOLS}
RESULT_CAP = 24_000


def tool_catalog() -> list[dict]:
    return agentkit.tool_catalog(TOOLS)


def cap_result(result: Any, limit: int = RESULT_CAP) -> Any:
    return agentkit.cap_result(result, limit)


def call_tool(svc: Services, name: str, arguments: dict | None, *, caller: Optional[str] = None, cap: bool = False) -> Any:
    result = agentkit.call_tool(TOOLS_BY_NAME, svc, name, arguments, cap=False)
    image = result.pop("_image", None) if isinstance(result, dict) else None
    if cap:
        result = cap_result(result)
    if image:
        result["_image"] = image  # a picture is never trimmed; the MCP bridge turns it into an image block
    return result
