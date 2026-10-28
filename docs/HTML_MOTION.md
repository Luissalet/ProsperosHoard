# HTML animation (Montaje)

An animation is one ordinary HTML file that exposes `window.seek(t)`. The page must draw the **complete frame** for the
time `t` (seconds) and nothing else may depend on the wall clock. Prospero opens it in a headless browser, asks for every
frame of the timeline, encodes them with ffmpeg and returns a verified H.264 MP4. Compositions are stored in
`<data>/editing/creative/html/<id>/<revision>/`; every save makes a new revision and the old ones stay.

| Where | What |
| --- | --- |
| UI | Montaje, home page, **Animación HTML**: source editor, frame review, several moments at once, sound, export, add to the timeline |
| REST | `/editing/api/html-motion/...` (composition, `revisions/{n}/frame`, `.../render`, `.../sound`, ...) |
| MCP | `html_motion_starters`, `html_motion_starter_create`, `html_motion_save`, `html_motion_list`, `html_motion_get`, `html_motion_frame`, `html_motion_storyboard`, `html_motion_render`, `html_motion_restore`, and the sound tools below |

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
