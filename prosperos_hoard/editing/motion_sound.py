"""Sound for HTML animations: events {t, role} become effects from the foley kit, placed by their measured sync point,
mixed with optional music and mastered to -14 LUFS with a true peak under -1 dBTP.

A composition declares its sound as data (`sound` in its manifest, or a `<script type="application/json" id="lumiere-sound">`
block in the HTML):

    {"kit": "launch", "events": [{"t": 0.5, "role": "tap"}, {"t": 1.2, "role": "whoosh"}],
     "music": {"media": "med_..."}, "lufs": -14, "true_peak_db": -1}

A role is a name of the kit (tick, tock, hit, land, land.light, pop, whoosh.short, click, key, bell...) or an alias in
Spanish or English (tap, golpe, clic, whoosh...). The kit knows where each role's sync point sits (its onset, peak or
end) and how far from the picture event it should land (taps lead by 60 ms, knocks by 40, clicks by 11, whoosh peaks
follow by 37); `place` puts the take's sync point there, not the start of the file."""
from __future__ import annotations

import hashlib
import json
import math
import re
import threading
from pathlib import Path
from typing import Any, Optional

import numpy as np
from pydantic import BaseModel, Field, ValidationError

from . import ffmpeg as ff
from . import loudness
from .errors import LumiereError, NotFound
from .foley import wavio
from .foley.forge import bank, kit as foley_kit, knobs, sfx
from .util import atomic_write

SR = 48000
DEFAULT_KIT = "launch"
TARGET_LUFS = -14.0
TRUE_PEAK_DB = -1.0
MUSIC_BED_LUFS = -20.0       # the music is set here before the effects are laid over it and the whole mix is mastered
STOP_DOWN_DB = 8.0           # the music steps down this far before a big hit (the kit asks for 6-12 dB over 0.3-0.5 s)
STOP_DOWN_S = 0.4
MAX_EVENTS = 400
ALIASES = {
    "tap": "land.light", "toque": "land.light", "touch": "land.light", "dedo": "land.light",
    "golpe": "tock", "knock": "tock", "toc": "tock", "madera": "tock",
    "impacto": "hit", "punch": "hit", "corte": "hit",
    "caida": "drop", "caída": "drop", "bajo": "drop",
    "whoosh": "whoosh.short", "swoosh": "whoosh.short", "barrido": "whoosh.short", "whoosh_corto": "whoosh.short",
    "whoosh_largo": "whoosh.long", "big_whoosh": "whoosh.long", "barrido_largo": "whoosh.long",
    "clic": "click", "clack": "click", "pulsar": "click",
    "confirmar": "click.confirm", "confirm": "click.confirm", "ding": "click.confirm", "aviso": "click.confirm",
    "tecla": "key", "tecleo": "key", "type": "key", "espacio": "key.space", "space": "key.space", "intro": "key.return", "enter": "key.return",
    "campana": "bell", "nota": "bell", "texto": "text", "palabra": "text",
    "tictac": "tick", "tic": "tick", "tick_crisp": "tick.crisp",
    "aterriza": "land", "thump": "land", "popin": "land", "aparece": "land",
    "burbuja": "pop", "globo": "pop",
    "chasquido": "snap", "foto": "snap", "disparo": "snap", "shutter": "snap",
    "subida": "riser", "crescendo": "swell", "oleada": "swell",
}
_DECLARED = re.compile(r"<script\b[^>]*\bid\s*=\s*[\"']lumiere-sound[\"'][^>]*>(.*?)</script>", re.S | re.I)
_LOCK = threading.RLock()


# ------------------------------------------------------------------------------------------------ the declaration
class SoundEvent(BaseModel):
    t: float = Field(..., ge=0, allow_inf_nan=False, description="Seconds into the composition where the picture event happens.")
    role: str = Field(..., min_length=1, max_length=40, description="A kit role or an alias (tap, golpe, clic, whoosh...). See motion_sound_roles.")
    gain_db: float = Field(0, ge=-30, le=12, allow_inf_nan=False)
    take: Optional[int] = Field(None, ge=0, description="Which take of the role (0-based); omitted = taken in turn, so a run of the same role varies.")
    offset_ms: Optional[float] = Field(None, ge=-500, le=500, allow_inf_nan=False, description="Replaces the role's own offset between picture event and sync point.")
    pitch: Optional[str | float] = Field(None, description="Pitched roles (bell, text): a note name (C5) or Hz.")
    label: str = Field("", max_length=80)


class SoundMusic(BaseModel):
    media: Optional[str] = Field(None, max_length=40, description="A media id of the library (an audio file or a song received from the song module).")
    path: Optional[str] = Field(None, max_length=2000)
    gain_db: float = Field(0, ge=-24, le=12, allow_inf_nan=False, description="Against the usual bed level.")
    start_s: float = Field(0, ge=0, allow_inf_nan=False, description="Where in the music the composition starts.")
    loop: bool = False
    fade_in_s: float = Field(0.02, ge=0, le=10, allow_inf_nan=False)
    fade_out_s: float = Field(0.4, ge=0, le=10, allow_inf_nan=False)
    stop_down: bool = Field(True, description="The music steps down before a hit, drop, long whoosh or swell and comes back on it.")


class SoundSpec(BaseModel):
    kit: str = DEFAULT_KIT
    events: list[SoundEvent] = Field(default_factory=list, max_length=MAX_EVENTS)
    music: Optional[SoundMusic] = None
    lufs: float = Field(TARGET_LUFS, ge=-30, le=-8, allow_inf_nan=False)
    true_peak_db: float = Field(TRUE_PEAK_DB, ge=-6, le=-0.1, allow_inf_nan=False)
    audio: bool = Field(True, description="False keeps the declaration but exports the video without sound.")


def role_names(kit_name: str = DEFAULT_KIT) -> list[str]:
    spec = foley_kit.preset(kit_name)
    return [r for r, s in spec["roles"].items() if s]


def _pitch_value(pitch: Any) -> Any:
    """A note name stays a name; a number (also as text) is Hz."""
    if isinstance(pitch, str) and pitch.strip().replace(".", "", 1).isdigit():
        return float(pitch)
    return pitch


def canonical_role(name: str, kit_name: str = DEFAULT_KIT) -> str:
    spec = foley_kit.preset(kit_name)
    roles = spec["roles"]
    key = str(name).strip().lower().replace(" ", "_")
    candidates = [key, key.replace("_", "."), ALIASES.get(key, ""), ALIASES.get(key.replace(".", "_"), "")]
    for candidate in candidates:
        if candidate in roles:
            if not roles[candidate]:
                why = (spec.get("off") or {}).get(candidate, "this kit does not sound it")
                raise LumiereError(f"Role {candidate!r} is off in the {kit_name} kit: {why}.", code="sound_role_off")
            return candidate
    raise LumiereError(f"Unknown sound role {name!r}. Roles: {', '.join(role_names(kit_name))}. "
                       f"Aliases: {', '.join(sorted(ALIASES))}.", code="sound_role")


def declared_in_html(html: str) -> Optional[dict]:
    """The sound a composition declares inside its HTML, or None."""
    found = _DECLARED.search(html or "")
    if not found:
        return None
    try:
        data = json.loads(found.group(1))
    except ValueError as error:
        raise LumiereError(f"The lumiere-sound block of the HTML is not valid JSON: {error}", code="sound_spec") from None
    if not isinstance(data, dict):
        raise LumiereError("The lumiere-sound block must be a JSON object.", code="sound_spec")
    return data


def normalise(raw: Any, duration: Optional[float] = None) -> Optional[dict]:
    """A canonical sound declaration (roles resolved, events sorted) or None when there is nothing to sound."""
    if raw is None:
        return None
    if isinstance(raw, SoundSpec):
        raw = raw.model_dump()
    if not isinstance(raw, dict):
        raise LumiereError("sound must be an object {kit, events, music...}.", code="sound_spec")
    if not raw:
        return None
    try:
        spec = SoundSpec.model_validate(raw)
    except ValidationError as error:
        first = error.errors()[0]
        raise LumiereError(f"Invalid sound declaration at {'.'.join(str(p) for p in first['loc'])}: {first['msg']}", code="sound_spec") from None
    if spec.kit not in foley_kit.presets():
        raise LumiereError(f"Unknown kit {spec.kit!r}. Kits: {', '.join(foley_kit.presets())}.", code="sound_kit")
    events = []
    for event in spec.events:
        if duration is not None and event.t > duration + 1e-6:
            raise LumiereError(f"The event at {event.t:g} s ({event.role}) is after the end of the composition ({duration:g} s).", code="sound_event_time")
        item = event.model_dump(exclude_none=True)
        if not item.get("label"):
            item.pop("label", None)
        if item.get("gain_db") == 0:
            item.pop("gain_db")
        item["role"] = canonical_role(event.role, spec.kit)
        if event.pitch is not None:
            try:
                knobs.label(_pitch_value(event.pitch))
            except Exception:  # noqa: BLE001 - the forge says why it is not a pitch
                raise LumiereError(f"{event.pitch!r} is not a pitch: use a note name (C5) or Hz.", code="sound_pitch") from None
        events.append(item)
    events.sort(key=lambda e: (e["t"], e["role"]))
    out: dict[str, Any] = {"kit": spec.kit, "events": events, "lufs": spec.lufs, "true_peak_db": spec.true_peak_db}
    if spec.music:
        if not (spec.music.media or spec.music.path):
            raise LumiereError("music needs a media id or a path.", code="sound_music")
        out["music"] = spec.music.model_dump(exclude_none=True)
    if not spec.audio:
        out["audio"] = False
    return out if (events or out.get("music")) else None


# ------------------------------------------------------------------------------------------------ the take library
class Library:
    """The takes of one kit rendered on demand into data/creative/sound/<kit>-<digest>/, with the same kit.lock.json
    (onset_s / peak_s / end_s per take) the kit writes. Only the roles a composition uses are rendered; a take is
    rendered once and kept."""

    def __init__(self, data_dir: Path, kit_name: str = DEFAULT_KIT):
        if kit_name not in foley_kit.presets():
            raise LumiereError(f"Unknown kit {kit_name!r}. Kits: {', '.join(foley_kit.presets())}.", code="sound_kit")
        self.kit_name = kit_name
        self.spec = foley_kit.load(foley_kit.preset(kit_name))
        self.sr = int(self.spec.get("sr", SR))
        self.out = Path(data_dir).resolve() / "creative" / "sound" / f"{kit_name}-{foley_kit._digest(self.spec)[:10]}"

    # -- what the kit offers
    def roles(self) -> list[dict]:
        aliases: dict[str, list[str]] = {}
        for alias, role in ALIASES.items():
            aliases.setdefault(role, []).append(alias)
        rows = []
        for role, spec in self.spec["roles"].items():
            if not spec:
                rows.append({"role": role, "sounded": False, "why_off": (self.spec.get("off") or {}).get(role, "off in this kit")})
                continue
            rows.append({"role": role, "sounded": True, "sound": spec["sound"], "sync_point": spec.get("align", foley_kit.ALIGN.get(role, "onset")),
                         "offset_ms": float(spec.get("offset_ms") or 0), "bed_db": spec.get("bed_db"), "level_db": float(spec.get("level_db") or 0),
                         "takes": int(spec.get("takes", 1)) * (len(spec["pitches"]) if spec.get("pitches") else 1),
                         "pitches": spec.get("pitches"), "min_gap_ms": spec.get("min_gap_ms"), "aliases": sorted(aliases.get(role, [])),
                         "note": spec.get("note")})
        return rows

    # -- the lock
    def _skeleton(self) -> dict:
        from .foley import __version__
        lock = {"kit": foley_kit.SCHEMA, "earshot": __version__, "bank": bank.VERSION, "sr": self.sr, "name": self.spec.get("name"), "roles": {}}
        for role, spec in self.spec["roles"].items():
            lock["roles"][role] = None if not spec else {
                "sound": spec["sound"], "align": spec.get("align", foley_kit.ALIGN.get(role, "onset")),
                "level_db": float(spec.get("level_db") or 0.0), "max_s": spec.get("max_s"),
                **{m: float(spec[m]) for m in foley_kit.MIX_KEYS if spec.get(m) is not None}, "takes": []}
        if self.spec.get("mix") is not None:
            lock["mix"] = self.spec["mix"]
        return lock

    def lock(self) -> dict:
        path = self.out / foley_kit.LOCK
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return self._skeleton()

    @staticmethod
    def _sha1(path: Path) -> str:
        return hashlib.sha1(path.read_bytes()).hexdigest()

    def ensure(self, roles: set[str], ctx: Any = None) -> dict:
        """Render the missing takes of these roles and return the lock."""
        with _LOCK:
            self.out.mkdir(parents=True, exist_ok=True)
            lock = self.lock()
            for role, entry in self._skeleton()["roles"].items():  # a lock from an older bank keeps its takes, new roles appear
                lock["roles"].setdefault(role, entry)
            changed = False
            by_role: dict[str, list[dict]] = {}
            for take in foley_kit.plan(self.spec):
                if take["role"] in roles:
                    by_role.setdefault(take["role"], []).append(take)
            for role, planned in by_role.items():
                role_lock = lock["roles"][role]
                known = {t.get("file"): t for t in role_lock["takes"]}
                fresh = []
                for take in planned:
                    if ctx is not None:
                        ctx.check()
                    key = foley_kit.take_key(take, self.sr)
                    path = self.out / take["file"]
                    record = known.get(take["file"])
                    if record and record.get("key") == key and path.is_file() and self._sha1(path) == record.get("sha1"):
                        fresh.append(record)
                        continue
                    y, check, meta, used = foley_kit.render_take(take)
                    measured = bank.measure(y)
                    sfx.write(str(path), y, self.sr)
                    record = {"file": take["file"], "seed": used, "pitch": take["pitch"],
                              **{k: measured[k] for k in ("onset_s", "peak_s", "end_s", "dur_s", "peak_db", "lufs_m_max")},
                              "sha1": self._sha1(path), "key": key, "checks": [w["code"] for w in check["warnings"]]}
                    if used != take["seed"]:
                        record["planned_seed"] = take["seed"]
                    if "bed_db" in role_lock:
                        record["l50_lufs"] = foley_kit.loud_lufs(y, self.sr)
                    if take.get("width") is not None:
                        record["corr"] = meta["width"]["measured"]
                    fresh.append(record)
                    changed = True
                if [t.get("file") for t in fresh] != [t.get("file") for t in role_lock["takes"]]:
                    changed = True
                role_lock["takes"] = fresh
            if changed or not (self.out / foley_kit.LOCK).is_file():
                atomic_write(self.out / foley_kit.LOCK, json.dumps(lock, indent=1) + "\n")
            return lock

    def audio(self, take: dict) -> np.ndarray:
        y, _ = wavio.read(str(self.out / take["file"]), always_2d=True, dtype="float64")
        return loudness.stereo(y)


# ------------------------------------------------------------------------------------------------ the mix
def _pick_take(role: str, role_lock: dict, event: dict, ordinal: int) -> dict:
    takes = role_lock["takes"]
    if event.get("pitch") is not None:
        want = knobs.label(_pitch_value(event["pitch"]))
        takes = [t for t in takes if t.get("pitch") is not None and knobs.label(t["pitch"]) == want]
        if not takes:
            raise LumiereError(f"The {role} role has no take at pitch {event['pitch']}. Pitches: "
                               f"{', '.join(sorted({knobs.label(t['pitch']) for t in role_lock['takes'] if t.get('pitch') is not None}))}.", code="sound_pitch")
        if event.get("take") is None:
            return takes[ordinal % len(takes)]
    if event.get("take") is not None:
        if event["take"] >= len(takes):
            raise LumiereError(f"The {role} role has {len(takes)} takes; take {event['take']} does not exist.", code="sound_take")
        return takes[event["take"]]
    return takes[ordinal % len(takes)]


def stop_down_gain(n: int, times: list[float], depth_db: float = STOP_DOWN_DB, length_s: float = STOP_DOWN_S,
                   attack_s: float = 0.06, release_s: float = 0.12, sr: int = SR) -> np.ndarray:
    """Linear gain for the music: one step down shortly before each big hit, back up on it."""
    reduction = np.zeros(n)
    for t in times:
        first = max(0.0, t - length_s)
        a, b = int(first * sr), min(n, int((t + release_s) * sr) + 1)
        if b <= a:
            continue
        x = np.arange(a, b) / sr
        curve = np.interp(x, [first, first + attack_s, t, t + release_s], [0.0, depth_db, depth_db, 0.0])
        reduction[a:b] = np.maximum(reduction[a:b], curve)
    return 10.0 ** (-reduction / 20.0)


def decode(tools: ff.Tools, path: Path, start_s: float = 0.0, length_s: Optional[float] = None, handle: Any = None) -> np.ndarray:
    """(n, 2) float64 at 48 kHz of the first audio stream of a file."""
    args = ["-ss", f"{start_s:.6f}"] if start_s else []
    args += ["-i", str(path), "-map", "0:a:0", "-vn"]
    if length_s is not None:
        args += ["-t", f"{length_s:.6f}"]
    args += ["-ac", "2", "-ar", str(SR), "-f", "f32le", "-acodec", "pcm_f32le", "pipe:1"]
    out, _ = ff.capture(tools, args, binary=True, handle=handle)
    if not out:
        raise LumiereError(f"{path.name} has no sound to decode.", code="sound_music")
    return np.frombuffer(out, np.float32).reshape(-1, 2).astype(np.float64)


def build_mix(library: Library, sound: dict, duration_s: float, music: Optional[np.ndarray] = None, ctx: Any = None) -> dict:
    """The soundtrack of one composition: {'mix' (n, 2) float64 before mastering, 'effects' (n, 2), 'placements', 'notes', 'music'}.

    sound: a normalised declaration; music: samples (n, 2) at 48 kHz starting at the composition's start, or None."""
    n = int(round(duration_s * SR))
    events = sound.get("events", [])
    lock = library.ensure({e["role"] for e in events}, ctx)
    effects = np.zeros((n, 2))
    placements: list[dict] = []
    notes: list[str] = []
    counters: dict[str, int] = {}
    bed_source = None
    music_info = None
    if music is not None:
        m = music[:n]
        level = loudness.integrated_lufs(m) if m.size else loudness.SILENT_LUFS
        if level > -70:
            m = m * loudness.amp(MUSIC_BED_LUFS + float(sound.get("music", {}).get("gain_db", 0.0)) - level)
        music_info = {"source_lufs": round(level, 2), "bed_lufs": MUSIC_BED_LUFS + float(sound.get("music", {}).get("gain_db", 0.0))}
        bed_source = m
    stop_times: list[float] = []
    stop_roles = set((lock.get("mix") or {}).get("stop_down", {}).get("before") or [])
    last_by_role: dict[str, float] = {}
    for event in events:
        if ctx is not None:
            ctx.check()
        role = event["role"]
        role_lock = lock["roles"][role]
        ordinal = counters.get(role, 0)
        counters[role] = ordinal + 1
        take = _pick_take(role, role_lock, event, ordinal)
        align = role_lock.get("align") or "onset"
        offset_ms = float(event["offset_ms"]) if event.get("offset_ms") is not None else float(role_lock.get("offset_ms") or 0.0)
        sync_s = event["t"] + offset_ms / 1000.0
        start_s = sync_s - float(take[f"{align}_s"])
        bed = None
        if bed_source is not None:
            try:
                bed = foley_kit.bed_level(bed_source, event["t"])
            except foley_kit.KitError:
                bed = None
        gain_db = foley_kit.gain_db(take, role_lock, bed) + float(event.get("gain_db", 0.0))
        y = library.audio(take) * loudness.amp(gain_db)
        first = int(round(start_s * SR))
        a, b = max(0, first), min(n, first + y.shape[0])
        if b > a:
            piece = y[a - first:b - first]
            if first + y.shape[0] > n:  # cut by the end of the composition: a short fade so it does not click
                fade = min(piece.shape[0], int(0.01 * SR))
                piece = piece.copy()
                piece[-fade:] *= np.linspace(1.0, 0.0, fade)[:, None]
            effects[a:b] += piece
        else:
            notes.append(f"The {role} at {event['t']:g} s falls outside the composition and is not heard.")
        gap = (float(role_lock.get("min_gap_ms") or 0.0)) / 1000.0
        if role in last_by_role and gap and event["t"] - last_by_role[role] < gap - 1e-9:
            notes.append(f"Two {role} cues {event['t'] - last_by_role[role]:.3f} s apart (the kit keeps them {gap:.3f} s apart in a run); kept on the picture.")
        last_by_role[role] = event["t"]
        if role in stop_roles:
            stop_times.append(event["t"])
        placements.append({"t": event["t"], "role": role, "take": take["file"], "sync_point": align, "offset_ms": offset_ms,
                           "sync_s": round(sync_s, 4), "start_s": round(start_s, 4), "gain_db": round(gain_db, 2)})
    mix = effects.copy()
    if bed_source is not None:
        spec = sound.get("music", {})
        bed = bed_source.copy()
        if spec.get("stop_down", True) and stop_times:
            stop = (lock.get("mix") or {}).get("stop_down", {})
            depth = float(np.mean(stop.get("depth_db") or [STOP_DOWN_DB, STOP_DOWN_DB]))
            length = float(np.mean(stop.get("length_s") or [STOP_DOWN_S, STOP_DOWN_S]))
            bed *= stop_down_gain(bed.shape[0], stop_times, depth, length)[:, None]
        fade_in, fade_out = int(spec.get("fade_in_s", 0.02) * SR), int(spec.get("fade_out_s", 0.4) * SR)
        if fade_in:
            k = min(fade_in, bed.shape[0])
            bed[:k] *= np.linspace(0.0, 1.0, k)[:, None]
        if fade_out:
            k = min(fade_out, bed.shape[0])
            bed[-k:] *= np.linspace(1.0, 0.0, k)[:, None]
        mix[:bed.shape[0]] += bed
        music_info = {**(music_info or {}), "stop_down": [round(t, 3) for t in stop_times] if spec.get("stop_down", True) else []}
    return {"mix": mix, "effects": effects, "placements": placements, "notes": notes, "music": music_info, "samples": n}


def measure_sync(audio: np.ndarray, placements: list[dict], sr: int = SR, before: float = 0.12, after: float = 0.4) -> list[dict]:
    """Where the sync point of each effect really is in a rendered track: {t, role, expected_s, measured_s, error_ms}.
    An effect closer than `after` seconds to another one is skipped (the points would blend)."""
    x = loudness.stereo(audio).mean(axis=1)
    rows = []
    syncs = [p["sync_s"] for p in placements]
    for i, p in enumerate(placements):
        e = p["sync_s"]
        if any(j != i and e - after < s < e + after for j, s in enumerate(syncs)):
            rows.append({"t": p["t"], "role": p["role"], "expected_s": e, "measured_s": None, "error_ms": None, "skipped": "overlaps another effect"})
            continue
        w0 = max(0.0, e - before)
        window = x[int(w0 * sr):int((e + after) * sr)]
        if window.size < sr // 100 or float(np.abs(window).max()) < 1e-6:
            rows.append({"t": p["t"], "role": p["role"], "expected_s": e, "measured_s": None, "error_ms": None, "skipped": "silent"})
            continue
        found = bank.times(window, sr)
        measured = w0 + found[f"{p['sync_point']}_s"]
        rows.append({"t": p["t"], "role": p["role"], "expected_s": e, "measured_s": round(measured, 4), "error_ms": round((measured - e) * 1000.0, 2)})
    return rows


def resolve_music(svc: Any, music: Optional[dict]) -> Optional[Path]:
    if not music:
        return None
    from . import media as media_store
    if music.get("media"):
        info = media_store.get(svc, music["media"])
        if not info["has_audio"]:
            raise LumiereError(f"{info['name']} has no sound to use as music.", code="sound_music")
        path = Path(info["path"])
    else:
        path = Path(music["path"]).expanduser()
        media_store._check_root(svc, path)
    if not path.is_file():
        raise NotFound(f"The music file is missing: {path}")
    return path


def build_soundtrack(svc: Any, sound: dict, duration_s: float, destination: Path, ctx: Any = None) -> dict:
    """Mix and master the declared sound for a composition of duration_s seconds into destination (a 24-bit WAV).
    Returns the report {'path', 'lufs', 'true_peak_db', 'placements', 'sync_check', ...}."""
    library = Library(svc.config.data_dir, sound.get("kit", DEFAULT_KIT))
    music = None
    path = resolve_music(svc, sound.get("music"))
    if path is not None:
        spec = sound["music"]
        want = duration_s + 0.1
        music = decode(svc.tools(), path, float(spec.get("start_s", 0.0)), None if spec.get("loop") else want, ctx.handle() if ctx else None)
        if spec.get("loop") and music.shape[0]:
            music = np.tile(music, (int(math.ceil(want * SR / music.shape[0])), 1))
    built = build_mix(library, sound, duration_s, music, ctx)
    ceiling = float(sound.get("true_peak_db", TRUE_PEAK_DB)) - float(sound.get("_headroom", 0.0))
    mastered, info = loudness.master(built["mix"], float(sound.get("lufs", TARGET_LUFS)), ceiling)
    if ctx is not None:
        ctx.check()
    wavio.write(str(destination), mastered, SR, subtype="PCM_24")
    check = measure_sync(built["effects"], built["placements"])
    notes = list(built["notes"])
    if not info["reached"] and not info.get("silent"):
        notes.append(f"The mix reaches {info['lufs']} LUFS, not {info['target_lufs']}: the effects are too sparse to get there under {info['ceiling_db']} dBTP.")
    if info.get("silent"):
        notes.append("The mix is silent.")
    return {"path": str(destination), "kit": library.kit_name, "duration_s": round(built["samples"] / SR, 4), "events": len(built["placements"]),
            "lufs": info["lufs"], "true_peak_db": info["true_peak_db"], "gain_db": info["gain_db"], "limiter_db": info["limiter_db"],
            "target_lufs": info["target_lufs"], "ceiling_db": info["ceiling_db"], "reached": info["reached"], "music": built["music"],
            "placements": built["placements"], "sync_check": check,
            "max_sync_error_ms": max([abs(r["error_ms"]) for r in check if r["error_ms"] is not None] or [0.0]), "notes": notes}
