"""Titles and captions as one ASS subtitle file (rendered by libass inside the render), plus SRT / VTT exports.

Caption lines come from the word-level transcripts of the media on the captioned tracks, mapped through every clip to
timeline time, so cuts, speed changes and text-based edits move the captions with the picture.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable, Optional

from ..timeline import Captions, Clip, Project, TextStyle
from ..hoard_link.media import subs

WordsFor = Callable[[str], Optional[list[dict[str, Any]]]]  # media id -> [{t0, t1, text, id?}] in source ms
# Ready-made caption cues (translations): (start ms, stop ms, lines). The first line is drawn normally, further lines smaller and highlighted.
CueLines = list[tuple[int, int, list[str]]]


@dataclass
class Word:
    t0: int
    t1: int
    text: str
    clip: str
    speaker: str = ""   # display name of who says it (speaker-separated transcripts), "" = unknown / single voice
    color: str = ""     # #RRGGBB of that speaker


def _ass_color(hex6: str, alpha: int = 0) -> str:
    h = hex6.lstrip("#")
    r, g, b = h[0:2], h[2:4], h[4:6]
    return f"&H{alpha:02X}{b}{g}{r}".upper()


def _box_color(value: Optional[str]) -> tuple[str, int]:
    if not value:
        return "#000000", 0x80
    h = value.lstrip("#")
    alpha = 255 - int(h[6:8], 16) if len(h) == 8 else 0x40
    return "#" + h[:6], alpha


def _clean(text: str) -> str:
    return subs.ass_escape(text)


def _ts(ms: float) -> str:
    return subs.ass_time(ms / 1000)


# ---------------------------------------------------------------- words on the timeline

def timeline_words(project: Project, words_for: WordsFor, *, track_ids: Optional[list[str]] = None) -> list[Word]:
    tracks = track_ids or project.captions.tracks
    if not tracks:
        main = project.main_track()
        tracks = [main.id] if main else []
    tracks = list(project.with_multicam_sound(set(tracks)))  # a multicam group's words come from its master sound
    end = project.duration
    out: list[Word] = []
    for t in project.tracks:
        if t.id not in tracks or t.muted:
            continue
        for c in t.clips:
            if c.type != "media" or c.mute or c.reverse or not c.media:
                continue
            words = words_for(c.media) or []
            for w in words:
                t0, t1 = int(w["t0"]), int(w["t1"])
                mid = (t0 + t1) / 2
                if mid < c.src_in or mid >= c.src_out:
                    continue
                a = c.timeline_at(max(t0, c.src_in))
                b = c.timeline_at(min(t1, c.src_out))
                a, b = int(round(max(a, c.start))), int(round(min(b, c.end, end)))
                text = str(w.get("text") or "").strip()
                if b - a < 20 or not text or a >= end:
                    continue
                out.append(Word(a, b, text, c.id, str(w.get("speaker_name") or w.get("speaker") or ""), str(w.get("speaker_color") or "")))
    out.sort(key=lambda w: w.t0)
    return out


def group_lines(words: list[Word], cap: Captions) -> list[list[Word]]:
    lines: list[list[Word]] = []
    cur: list[Word] = []
    chars = 0
    for w in words:
        gap = w.t0 - cur[-1].t1 if cur else 0
        ends_sentence = bool(cur) and cur[-1].text[-1:] in ".?!…"
        new_len = chars + len(w.text) + (1 if cur else 0)
        if cur and (len(cur) >= cap.max_words or new_len > cap.max_chars or gap > 700 or (ends_sentence and len(cur) >= 2)
                    or cur[-1].clip != w.clip and gap > 200 or (cap.speaker_labels != "off" and cur[-1].speaker != w.speaker)):
            lines.append(cur)
            cur, chars = [], 0
            new_len = len(w.text)
        cur.append(w)
        chars = new_len
    if cur:
        lines.append(cur)
    return lines


def _line_text(line: list[Word], upper: bool) -> list[str]:
    return [(_clean(w.text).upper() if upper else _clean(w.text)) for w in line]


def _multi_speaker(words: list[Word]) -> bool:
    return len({w.speaker for w in words if w.speaker}) > 1


def _speaker_prefix(line: list[Word], cap: Captions, multi: bool, upper: bool) -> str:
    """"Ana: " in front of a line of that speaker (only when the captions show names and several people speak)."""
    if cap.speaker_labels in ("prefix", "both") and multi and line[0].speaker:
        name = _clean(line[0].speaker)
        return (name.upper() if upper else name) + ": "
    return ""


def _speaker_tag(line: list[Word], cap: Captions) -> str:
    """ASS override that paints the line in its speaker's colour ("" when colours are off or the speaker has none)."""
    if cap.speaker_labels in ("color", "both") and line[0].color:
        h = line[0].color.lstrip("#")
        return f"{{\\1c&H{h[4:6]}{h[2:4]}{h[0:2]}&}}"
    return ""


# ---------------------------------------------------------------- ASS

def _caption_geometry(project: Project) -> dict[str, int]:
    W, H = project.canvas.width, project.canvas.height
    cap = project.captions
    portrait = H > W
    base = {"clean": 0.045, "bold": 0.058, "karaoke": 0.055, "pop": 0.062, "boxed": 0.045, "minimal": 0.034}[cap.style]
    size = cap.size or int(round(min(H, W * 1.78) * base * (1.0 if portrait else 1.05)))
    if cap.position == "top":
        align, margin_v = 8, int(H * 0.08)
    elif cap.position == "middle":
        align, margin_v = 5, 0
    elif cap.position == "bottom":
        align, margin_v = 2, int(H * 0.06)
    else:
        align, margin_v = 2, int(H * (0.22 if portrait else 0.11))
    return {"size": size, "align": align, "margin_v": margin_v, "margin_h": int(W * 0.06)}


def _style_line(name: str, font: str, size: int, primary: str, secondary: str, outline: str, back: str, bold: bool, italic: bool,
                border_style: int, outline_w: float, shadow: float, align: int, ml: int, mr: int, mv: int) -> str:
    return (f"Style: {name},{font},{size},{primary},{secondary},{outline},{back},{-1 if bold else 0},{-1 if italic else 0},0,0,100,100,0,0,"
            f"{border_style},{outline_w:.1f},{shadow:.1f},{align},{ml},{mr},{mv},1")


def build_ass(project: Project, words_for: WordsFor, cues: Optional[CueLines] = None) -> tuple[str, dict[str, int]]:
    """The ASS document for the whole timeline and counts {captions, titles}. Times are timeline ms. With ``cues`` (a
    translation) the captions are those cues instead of the words heard: one static event per cue, so the timing is
    exactly the one the cues carry."""
    W, H = project.canvas.width, project.canvas.height
    styles: list[str] = []
    events: list[str] = []
    end = project.duration
    counts = {"captions": 0, "titles": 0}

    cap = project.captions
    if cap.enabled:
        g = _caption_geometry(project)
        white, hl, out = _ass_color(cap.color), _ass_color(cap.highlight), _ass_color(cap.outline)
        if cap.style == "boxed":
            styles.append(_style_line("Cap", cap.font, g["size"], white, white, _ass_color("#000000", 0x60), _ass_color("#000000", 0x60), True, False,
                                      3, max(6, g["size"] * 0.18), 0, g["align"], g["margin_h"], g["margin_h"], g["margin_v"]))
        elif cap.style == "minimal":
            styles.append(_style_line("Cap", cap.font, g["size"], white, white, out, _ass_color("#000000", 0x70), False, False, 1, 1.5, 2,
                                      g["align"], g["margin_h"], g["margin_h"], g["margin_v"]))
        elif cap.style == "karaoke":
            styles.append(_style_line("Cap", cap.font, g["size"], hl, white, out, _ass_color("#000000", 0x80), True, False, 1,
                                      max(3, g["size"] * 0.07), 2, g["align"], g["margin_h"], g["margin_h"], g["margin_v"]))
        else:
            outline_w = max(2, g["size"] * (0.09 if cap.style in ("bold", "pop") else 0.05))
            styles.append(_style_line("Cap", cap.font, g["size"], white, white, out, _ass_color("#000000", 0x80), cap.style != "clean", False, 1,
                                      outline_w, 2 if cap.style != "clean" else 1, g["align"], g["margin_h"], g["margin_h"], g["margin_v"]))
        all_words = timeline_words(project, words_for) if cues is None else []
        lines = group_lines(all_words, cap) if cues is None else []
        multi = _multi_speaker(all_words)
        upper = cap.uppercase or cap.style in ("bold", "pop")
        for a, b, parts in (cues or []):
            a, b = max(0, int(a)), min(int(b), end)
            if b - a < 60 or not parts:
                continue
            second = white if cap.style == "karaoke" else hl
            shown = [(_clean(x).upper() if upper else _clean(x)) for x in parts if x.strip()]
            if not shown:
                continue
            text = shown[0] + "".join(f"\\N{{\\fs{max(8, int(g['size'] * 0.78))}\\c{second}}}{x}" for x in shown[1:])
            events.append(f"Dialogue: 0,{_ts(a)},{_ts(b)},Cap,,0,0,0,,{{\\fad(60,60)}}{text}")
            counts["captions"] += 1
        for i, line in enumerate(lines):
            start = line[0].t0
            next_start = lines[i + 1][0].t0 if i + 1 < len(lines) else end
            stop = min(max(line[-1].t1 + 250, line[-1].t1), next_start, end)
            if stop - start < 60:
                continue
            words = _line_text(line, upper)
            tag = _speaker_tag(line, cap)
            prefix = _speaker_prefix(line, cap, multi, upper)
            if cap.style == "karaoke":
                parts = []
                for j, w in enumerate(line):
                    nxt = line[j + 1].t0 if j + 1 < len(line) else w.t1
                    parts.append(f"{{\\kf{max(1, round((nxt - w.t0) / 10))}}}{words[j]}")
                events.append(f"Dialogue: 0,{_ts(start)},{_ts(stop)},Cap,,0,0,0,,{tag}{prefix}{' '.join(parts)}")
                counts["captions"] += 1
            elif cap.style == "pop":
                for j, w in enumerate(line):
                    a = w.t0 if j else start
                    b = line[j + 1].t0 if j + 1 < len(line) else stop
                    if b - a < 20:
                        continue
                    text = " ".join(
                        (f"{{\\c{hl}\\fscx112\\fscy112\\t(0,90,\\fscx100\\fscy100)}}{t}{{\\r}}{tag}" if k == j else t) for k, t in enumerate(words))
                    events.append(f"Dialogue: 0,{_ts(a)},{_ts(b)},Cap,,0,0,0,,{tag}{prefix}{text}")
                counts["captions"] += 1
            else:
                events.append(f"Dialogue: 0,{_ts(start)},{_ts(stop)},Cap,,0,0,0,,{{\\fad(60,60)}}{tag}{prefix}{' '.join(words)}")
                counts["captions"] += 1

    n = 0
    for t in project.tracks:
        if t.kind != "text" or t.hidden:
            continue
        for c in t.clips:
            if c.type != "text" or c.start >= end:
                continue
            n += 1
            name = f"T{n}"
            st = c.style or TextStyle()
            styles.append(_text_style(name, st))
            events += _text_events(c, st, name, W, H, min(c.end, end))
            counts["titles"] += 1

    header = ["[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}", "WrapStyle: 0", "ScaledBorderAndShadow: yes",
              "YCbCr Matrix: TV.709", "", "[V4+ Styles]",
              "Format: Name, Fontname, Fontsize, PrimaryColour, SecondaryColour, OutlineColour, BackColour, Bold, Italic, Underline, StrikeOut, "
              "ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, MarginR, MarginV, Encoding",
              *(styles or [_style_line("Default", "Arial", 48, "&H00FFFFFF", "&H00FFFFFF", "&H00000000", "&H80000000", False, False, 1, 2, 1, 2, 20, 20, 20)]),
              "", "[Events]", "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text", *events, ""]
    return "\n".join(header), counts


def _text_style(name: str, st: TextStyle) -> str:
    if st.box:
        box, alpha = _box_color(st.box)
        return _style_line(name, st.font, st.size, _ass_color(st.color), _ass_color(st.color), _ass_color(box, alpha), _ass_color(box, alpha),
                           st.bold, st.italic, 3, max(4, st.size * 0.2), 0, 5, 0, 0, 0)
    return _style_line(name, st.font, st.size, _ass_color(st.color), _ass_color(st.color), _ass_color(st.outline), _ass_color("#000000", 0x80),
                       st.bold, st.italic, 1, st.outline_width, st.shadow, 5, 0, 0, 0)


def _text_events(c: Clip, st: TextStyle, style: str, W: int, H: int, stop: int) -> list[str]:
    row = {"top": 8, "middle": 5, "bottom": 2}[st.position]
    col = {"left": 1, "center": 2, "right": 3}[st.align]
    an = {8: 6, 5: 3, 2: 0}[row] + col
    x = {"left": st.margin, "center": W / 2, "right": W - st.margin}[st.align] + c.transform.x * W
    y = {"top": st.margin, "middle": H / 2, "bottom": H - st.margin}[st.position] + c.transform.y * H
    x, y = int(round(x)), int(round(y))
    alpha = int(round((1 - c.transform.opacity) * 255))
    base = f"\\an{an}\\pos({x},{y})" + (f"\\alpha&H{alpha:02X}&" if alpha else "")
    rot = f"\\frz{-c.transform.rotation:.1f}" if c.transform.rotation else ""
    scale = f"\\fscx{c.transform.scale * 100:.0f}\\fscy{c.transform.scale * 100:.0f}" if abs(c.transform.scale - 1) > 1e-3 else ""
    fin, fout = (c.fade_in or 0), (c.fade_out or 0)
    text = _clean(c.text)
    start, length = c.start, stop - c.start
    if length < 40:
        return []
    anim = st.animation
    if anim == "typewriter":
        chars = list(c.text.replace("\r", ""))
        reveal = min(len(chars) * 45, int(length * 0.5))
        step = reveal / max(1, len(chars))
        events = []
        for i in range(1, len(chars) + 1):
            a = start + int((i - 1) * step)
            b = start + int(i * step) if i < len(chars) else stop
            if b <= a:
                continue
            shown = _clean("".join(chars[:i]))
            events.append(f"Dialogue: 0,{_ts(a)},{_ts(b)},{style},,0,0,0,,{{{base}{rot}{scale}}}{shown}")
        return events
    if anim == "pop":
        tags = f"\\fad({max(fin, 60)},{max(fout, 120)})\\fscx40\\fscy40\\t(0,180,\\fscx{c.transform.scale * 100:.0f}\\fscy{c.transform.scale * 100:.0f})"
        return [f"Dialogue: 0,{_ts(start)},{_ts(stop)},{style},,0,0,0,,{{{base}{rot}{tags}}}{text}"]
    if anim == "slide_up":
        dy = int(H * 0.04)
        move = f"\\an{an}\\move({x},{y + dy},{x},{y},0,320)" + (f"\\alpha&H{alpha:02X}&" if alpha else "")
        return [f"Dialogue: 0,{_ts(start)},{_ts(stop)},{style},,0,0,0,,{{{move}{rot}{scale}\\fad({max(fin, 200)},{max(fout, 200)})}}{text}"]
    fade = f"\\fad({max(fin, 250)},{max(fout, 250)})" if anim == "fade" else (f"\\fad({fin},{fout})" if fin or fout else "")
    return [f"Dialogue: 0,{_ts(start)},{_ts(stop)},{style},,0,0,0,,{{{base}{rot}{scale}{fade}}}{text}"]


# ---------------------------------------------------------------- SRT / VTT

def _srt_ts(ms: int, sep: str = ",") -> str:
    return (subs.srt_time if sep == "," else subs.vtt_time)(ms / 1000)


def raw_cues(project: Project, words_for: WordsFor) -> list[tuple[int, int, str]]:
    """The caption cues as heard (no upper-casing, no speaker labels): what a translation answers one by one."""
    lines = group_lines(timeline_words(project, words_for), project.captions)
    end = project.duration
    cues = []
    for i, line in enumerate(lines):
        start = line[0].t0
        nxt = lines[i + 1][0].t0 if i + 1 < len(lines) else end
        stop = min(line[-1].t1 + 250, nxt, end)
        cues.append((start, stop, " ".join(w.text for w in line)))
    return cues


def caption_cues(project: Project, words_for: WordsFor) -> list[tuple[int, int, str, str]]:
    """(start, stop, text, speaker colour or '') with the caption settings applied: case and speaker names or colours."""
    cap = project.captions
    all_words = timeline_words(project, words_for)
    lines = group_lines(all_words, cap)
    multi = _multi_speaker(all_words)
    end = project.duration
    cues = []
    for i, line in enumerate(lines):
        start = line[0].t0
        nxt = lines[i + 1][0].t0 if i + 1 < len(lines) else end
        stop = min(line[-1].t1 + 250, nxt, end)
        text = " ".join(w.text for w in line)
        text = text.upper() if cap.uppercase else text
        if cap.speaker_labels in ("prefix", "both") and multi and line[0].speaker:
            text = f"{line[0].speaker}: {text}"
        cues.append((start, stop, text, line[0].color if cap.speaker_labels in ("color", "both") else ""))
    return cues


def _cue_texts(project: Project, words_for: WordsFor, cues: Optional[CueLines]) -> list[tuple[int, int, str, str]]:
    if cues is None:
        return caption_cues(project, words_for)
    up = project.captions.uppercase
    return [(a, b, "\n".join((x.upper() if up else x) for x in parts), "") for a, b, parts in cues]


def build_srt(project: Project, words_for: WordsFor, cues: Optional[CueLines] = None) -> str:
    out = []
    for i, (a, b, text, color) in enumerate(_cue_texts(project, words_for, cues), start=1):
        if color:
            text = f'<font color="{color}">{text}</font>'
        out.append(f"{i}\n{_srt_ts(a)} --> {_srt_ts(b)}\n{text}\n")
    return "\n".join(out)


def build_vtt(project: Project, words_for: WordsFor, cues: Optional[CueLines] = None) -> str:
    return subs.to_vtt(subs.Cue(a / 1000, b / 1000, text) for a, b, text, _ in _cue_texts(project, words_for, cues))
