# HTML animation (Montaje)

An animation is one ordinary HTML file that exposes `window.seek(t)`. The page must draw the **complete frame** for the
time `t` (seconds) and nothing else may depend on the wall clock. Prospero opens it in a headless browser, asks for every
frame of the timeline, encodes them with ffmpeg and returns a verified H.264 MP4. Compositions are stored in
`<data>/editing/creative/html/<id>/<revision>/`; every save makes a new revision and the old ones stay.

| Where | What |
| --- | --- |
| UI | Montaje, home page, **Animación HTML**: source editor, frame review, several moments at once, sound, export, add to the timeline, *Borrar composición* (with a confirm) |
| REST | `/editing/api/html-motion/...` (composition, `revisions/{n}/frame`, `.../render`, `.../sound`, ...; `DELETE /editing/api/html-motion/{id}`) |
| MCP | `html_motion_starters`, `html_motion_starter_create`, `html_motion_save`, `html_motion_list`, `html_motion_get`, `html_motion_frame`, `html_motion_storyboard`, `html_motion_render`, `html_motion_restore`, `html_motion_delete`, and the sound tools below |

`html_motion_delete` removes the composition's folder (`data/editing/creative/html/<id>`): every revision, render,
storyboard, sound preview, treatment, cues and facts. It refuses with `html_motion_busy` while an export of the
composition is queued or running, and with `html_motion_in_use` while a library media still reads one of its renders
(`media_receive` imports a render where it is, without a copy: remove that media first).

## Sound

An animation can carry sound. It is **declared**, not recorded: a list of events `{t, role}` (the moment in the picture and
what kind of sound goes there) and, optionally, a music bed. The effects are generated from code (a bank of 252 procedural
effects, vendored in `prosperos_hoard/editing/foley/`, see [NOTICE](../NOTICE)), so the same declaration always gives the
same audio and nothing has to be downloaded or licensed.

```json
{"events": [{"t": 0.6, "role": "tap"}, {"t": 1.2, "role": "golpe"}, {"t": 2.4, "role": "whoosh"}],
 "music": {"media": "med_xxx", "gain_db": 0}, "lufs": -14, "true_peak_db": -1}
```

A page can also declare it inside the HTML: `<script type="application/json" id="lumiere-sound">{...}</script>`; an explicit
`sound` argument of `html_motion_save` wins, an edit that does not mention sound keeps the sound it had, and `sound: {}` clears it.

**Placement by sync point.** Every effect has a measured *onset* (first audible instant), *peak* (loudest 5 ms) and *end*.
The kit says which one belongs on the picture event and by how much to lead or lag it: in the `launch` kit a tap (`tap`,
`land.light`) leads the picture by 60 ms, a knock (`golpe`, `tock`) by 40 ms, a click by 11 ms and a whoosh peak follows it by
37 ms. `motion_sound_roles` lists the roles with their sound, sync point and offset; roles also answer to Spanish and English
aliases (`tap`, `golpe`, `clic`, `whoosh`, ...). Several events of the same role take different takes in turn; with music, the
bed steps down just before a hit and the effects are set against the bed.

**Mastering.** The mix is brought to the target loudness (default -14 LUFS, BS.1770 gated) with a look-ahead true-peak limiter
under -1 dBTP. The limiter never works harder than 8 dB; a mix of a few sparse effects without music cannot reach -14 LUFS
without crushing them, so it keeps the loudest honest level and says so (`reached: false`, with a note) instead of faking it.
AAC can overshoot the peak a little, so the exported file is measured again and the mix redone with just enough headroom.

| Tool | What it does |
| --- | --- |
| `motion_sound_roles` | Roles of a kit: sound, sync point, offset, takes, aliases |
| `motion_sound_events` | Read or change the events and the music (`set`, `add`, `remove`, `clear`, `clear_music`); a change saves a revision and needs `expected_revision` |
| `motion_sound_preview` | Mix and master the declared sound to a WAV without rendering the picture; returns the loudness reached and the **sync error measured on every effect** |
| `motion_export` (and `html_motion_render`) | Queue the MP4 with H.264 + AAC; `audio=false` for a silent file; `result.sound_report` lists every placement, the loudness delivered and the measured sync |
| `audio_sync_points` | Onset, peak and end of any library sound or file |

**Aligning any sound by its sync point.** Importing audio into the library measures its onset/peak/end (`media_analyze`
kind `sync`, also shown in the media view). `timeline_edit` `add_media` takes `align: "onset" | "peak" | "end"` with `at` (ms):
the clip is placed so that point lands on `at`, and the part that would fall before the start of the timeline is skipped.

**Music** comes from the library (`music: {media: id}`) or a file path. Songs made in the generation module arrive in the
library through `media_import` / `media_receive` like any other audio.

What the proof measured (synthetic animation, six events, real browser): every effect peak within 0.5 ms of its event in the
exported AAC file, delivered -14.1 LUFS with music and a true peak of -1.1 dBTP.


## Direction: style cards, treatment, cues and the checks

A film that has to look like something starts from a **style card** and goes through one flow. Each step leaves a file next to
the composition (`<data>/editing/creative/html/<id>/`), so the next revision, and the next assistant, start from it.

1. **Choose a card.** `motion_styles` (`search` by words, category or collection; `get` with an optional `section`) reads the
   74 imported direction cards (59 from one collection, 15 motion-graphics styles from another; both MIT, the demo films they
   mention are CC BY and are not copied) and the 4 built-in ones. Every card keeps its licence, attribution and source link, and
   its sections are split by what a variant may touch: **safe to change** (colour logic, type, motion quality, sound palette,
   native moves), **changes the character** (materials, camera), **essence** (what the style is and is not, the engine) and the
   **variation space** left free in every film. `studio_style_cards` (generation module) lists, searches and copies cards;
   `create` with `from_card` and `changes={section: text}` makes a variant, refusing an essence change without
   `allow_essence_change` and a character change without `accept_character_change`.
2. **Write the treatment first.** `motion_treatment` (`get`, `set`, `check`, `draft`) stores `treatment.json` *before* any HTML:
   three structures with the reason one was chosen, the shots one by one with **why each is there**, and the **cue map** (what the
   picture does at each moment and what is heard). `check` refuses three structures that are the same, shots out of order or past the end, shots that all give
   the same *why*, and cues past the end or with an unknown sound role. `draft` writes a first treatment from the card; it is marked `draft` and is a starting point.
3. **Make the page.** `motion_from_style` (card, name, size, duration, treatment or `draft_treatment`) creates the
   composition with the card's palette, a caption per shot sized to the reading rule, and a visual hit (flash, rule, bar) on
   every cue. It is a starting scaffold for an assistant to rewrite, not a finished film.
4. **Cues.** `motion_cues` (`get`, `set`, `clear`) owns `cues.json`: `{id, t, visual, sound}`. The sound events of the animation are
   **derived** from it, so picture and sound cannot drift; while cues exist, `motion_sound_events` refuses to change events
   (`cues_own_sound`) but still changes music, kit and loudness. The page may embed the same list in
   `<script id="lumiere-cues" type="application/json">`; `cues.json` is the one that sounds.
5. **Check the picture against the sound.** `motion_cuecheck` renders the real page and measures, per cue, the frame where the
   picture actually changes (mean absolute difference of 192 px greyscale frames, looked for within `window_s` = 0.3 s either side).
   Statuses: `ok` (within one frame and 20 ms), `sound_late`, `sound_early`, `no_hit`, `unclear`. With `scan` (the default up to
   1500 frames) it also lists **visual hits that have no cue**, and offers each cue that found nothing the nearest such hit
   (`suggest_t`, within 2 s). `retime=true` (with `expected_revision`) moves only the cues that are off by a little.
6. **Check that it can be read.** `motion_readcheck` samples the page and measures how long every text stays fully visible
   (opacity at least 0.85, at least 98 % inside the frame) against `letters / 15 s + 1.5 s` (CJK: characters / 4.5 s). A page that
   draws text in a canvas defines `window.TEXTS(t)` returning `[{id, text, x0, y0, x1, y1, opacity?}]`; otherwise the DOM is read.
7. **Informative videos keep `FACTS.md`.** `motion_facts` (`get`, `set`, `check`) stores each claim with its source, and `check`
   lists numbers shown on screen that are not in the file and facts never shown.

Reviewing a film does not need a contact sheet each time: `html_motion_storyboard` `mode` is `sheet` (the moments you list, as
before), `strip` (up to 48 frames over a `span`, small tiles in a grid) or `crop-at` (an enlarged detail around a point `x, y` or a
CSS `selector`, scaled to at most 640 px) for typography, edges and small motion.

In the interface, the **Animación HTML** section has *Crear desde un estilo* (search, the card with its four parts and licence,
name/duration/first caption, create), a **Dirección** panel (treatment, editable cues, check sync, move cues, check reading, facts) and
a review-type selector (sheet, strip, detail).

| Tool | What it does |
| --- | --- |
| `motion_styles` | Search and read direction cards and their sections |
| `motion_treatment` | Read, write, check or draft the treatment of a composition |
| `motion_from_style` | Create a composition from a card and a treatment |
| `motion_cues` | Read or set the cues; the sound events follow them |
| `motion_cuecheck` | Measure each cue against the frame where the picture hits; list hits with no cue; retime |
| `motion_readcheck` | Check that each text stays visible long enough to be read |
| `motion_facts` | Keep FACTS.md and check the numbers shown against it |

What the proof measured (real browser, private server): a film made from an imported card passed `motion_cuecheck` (2/2 cues, offsets
0 and 16.7 ms at 30 fps) and `motion_readcheck`; a cue moved 1.5 s off its hit was reported as `no_hit` with the real hit listed
as unmapped and offered as `suggest_t`. The cards are a starting vocabulary: the scaffold gives a plain page, the quality of the
film still depends on the assistant that writes the HTML and on its treatment.

## Comparison with the HyperFrames design (Ágora #783)

Read on 2026-10-20 from the upstream's documentation (Apache-2.0, revision 0693839b52306cbe35ee508f7b4c4b6f914a4fae); the upstream
was not run and nothing here claims its speed or quality. What Prospero already has and what it does not:

| Capability | Here | Gap |
| --- | --- | --- |
| Deterministic HTML frame by time | `window.seek(t)` contract, headless capture of every frame, verified H.264 MP4, frame review and `html_motion_storyboard` | none |
| Editable source with history | every save is a revision, `html_motion_restore` | none |
| Quality gates before export | anti-slideshow lint, GSAP tween reading, starters | none |
| Sound | declared events `{t, role}` from 252 procedural effects with measured sync offsets, optional music bed with ducking, loudness target | no per-track automation curves or groups: a bed has one gain, events have none |
| Montage hand-off | `html_motion_render` result goes to the timeline / Lumiere | none |

The one real difference is mixing: upstream documents audio mixing, automation and groups. Here the mix is fixed (events
placed by sync point over one bed). Add automation only when a composition needs a volume curve; the declaration format
(`lumiere-sound`) leaves room for a `gain_db` curve on `music` without a second mixer.

## Voice and camera trials of the same study (#783)

- VoxCPM2 is a TTS engine of the registry (`voxcpm`, see [VOICE.md](VOICE.md)); the comparison with XTTS, F5 and Kokoro is the
  `last_run` figures of `voice_engines` on the same text.
- Camera: the Spaces *Variations* node has a `orbit` mode (`orbit_step`, `orbit_elevation`, `orbit_distance`). It is a
  generative edit of one picture, not a 3D camera: the sides it was not shown are invented.