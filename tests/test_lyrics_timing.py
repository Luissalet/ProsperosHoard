"""First-pass karaoke timing from the lyrics' own [Section] tags and the
song's bar grid (`audio.time_lyrics`), and the timed section markers the
auto-cut then follows."""

from __future__ import annotations

from prosperos_hoard import audio, timeline

BAR = 4 * 60 / 140  # a 4/4 bar at 140 bpm


def _analysis(duration=120.0, bpm=140.0, sections=None):
    beat = 60 / bpm
    beats = [round(i * beat, 3) for i in range(int(duration / beat))]
    return {"duration_s": duration, "tempo_bpm": bpm, "beat_times": beats, "downbeats": beats[::4],
            "sections": sections or [{"label": "section A", "start_s": 0.0, "end_s": duration, "energy": "mid"}]}


LYRICS = """[Intro]
(shh...)
Cuenta las farolas.

[Verse 1]
""" + "\n".join(f"verso uno linea {i}" for i in range(12)) + """

[Chorus]
""" + "\n".join(f"estribillo {i}" for i in range(8)) + """

[Outro]
Una, dos, tres.
"""


def test_every_line_lands_on_a_beat_in_order_inside_its_section():
    an = _analysis()
    out = audio.time_lyrics(LYRICS, an)
    beats = set(an["beat_times"])
    times = [ln["time_s"] for ln in out["lines"]]
    assert times == sorted(times) and len(set(times)) == len(times)
    assert all(t in beats for t in times)
    by_label = {s["label"]: s for s in out["sections"]}
    for ln in out["lines"]:
        sec = by_label[ln["section"]]
        assert sec["start_s"] <= ln["time_s"] < sec["end_s"]
    assert out["sections"][0]["start_s"] == 0.0 and out["sections"][-1]["end_s"] == 120.0


def test_rap_verse_moves_faster_than_the_hook():
    out = audio.time_lyrics(LYRICS, _analysis())
    verse = [ln["time_s"] for ln in out["lines"] if ln["section"] == "Verse 1"]
    chorus = [ln["time_s"] for ln in out["lines"] if ln["section"] == "Chorus"]
    verse_gap = (verse[-1] - verse[0]) / (len(verse) - 1)
    chorus_gap = (chorus[-1] - chorus[0]) / (len(chorus) - 1)
    assert 0.8 * BAR <= verse_gap <= 1.3 * BAR
    assert chorus_gap >= 1.6 * verse_gap
    assert out["lines"][0]["time_s"] >= BAR  # an instrumental lead-in before the intro's first line


def test_section_starts_snap_to_the_analysis_boundaries():
    # the analysis heard a change 1.5 bars after where the line counts put the chorus
    plain = audio.time_lyrics(LYRICS, _analysis())
    chorus_at = next(s for s in plain["sections"] if s["label"] == "Chorus")["start_s"]
    heard = chorus_at + 1.5 * BAR
    sections = [{"label": "section A", "start_s": 0.0, "end_s": heard, "energy": "mid"},
                {"label": "section B", "start_s": heard, "end_s": 120.0, "energy": "high"}]
    snapped = audio.time_lyrics(LYRICS, _analysis(sections=sections))
    new_at = next(s for s in snapped["sections"] if s["label"] == "Chorus")["start_s"]
    assert abs(new_at - heard) < BAR / 2


def test_lrc_round_trip_keeps_markers_out_of_the_captions():
    an = _analysis()
    out = audio.time_lyrics(LYRICS, an)
    sung, sections = audio.lrc_sections(audio.parse_lrc(out["lrc"]), 120.0)
    assert len(sung) == len(out["lines"]) and not any(ln["text"].startswith("[") for ln in sung)
    assert [s["label"] for s in sections] == ["Intro", "Verse 1", "Chorus", "Outro"]
    assert [s["energy"] for s in sections] == ["low", "mid", "high", "low"]


def test_section_kinds():
    assert audio.section_kind("Pre-Chorus") == "pre"
    assert audio.section_kind("Chorus 2") == "chorus"
    assert audio.section_kind("Estribillo") == "chorus"
    assert audio.section_kind("Verse 2") == "verse"
    assert audio.section_kind("Something") == "other"
