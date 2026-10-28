# Timeline as text and cut proposals (Montaje)

An assistant edits a project the way it edits a source file: it reads the whole timeline as plain text, changes lines, and the
change is proved before anything is saved. Nothing here replaces `timeline_edit`; it is the compact way to make many small
changes (volumes, trims, labels, titles, moving or deleting clips) and to look at the whole edit at once.

| Where | What |
| --- | --- |
| UI | Montaje, bottom tabs, **Montaje como texto**: the text, Check, Apply (one undo step), and below it **Cut proposals** |
| REST | `GET /editing/api/projects/{id}/text`, `POST .../text/check`, `POST .../text/apply`, `GET/POST .../cut-proposals`, `POST .../cut-proposals/accept`, `POST .../cut-proposals/reject` |
| MCP | `timeline_text`, `timeline_check`, `timeline_apply_text`, `cut_proposals`, and `dry_run` + `expected_revision` on `timeline_edit` |

## The text

```
# lumiere-timeline-text/v1. Times are whole milliseconds. ...
timeline: prj_c7b4tejc "Interview"
revision: 3
canvas: 1920x1080 30fps background=#000000 length_mode=main
duration: 15000

track trk_vjjrkdzp | kind=video | role=main | name="Principal" | muted=no | hidden=no | locked=no | volume=0
  clp_8wb4rcxy media med_cbtzdzkp "speech" | start=0 | end=15000 | src=0-15000 | speed=1 | volume=-6 | mute=no | fade=0/0 | afade=0/0 | label="Intro"   # 0:00.000 to 0:15.000
```

Times are whole milliseconds; strings are JSON-quoted; the `# ...` timecode at the end of a line is ignored; the header is read
only; `end=` is derived (change `src`, `speed`, `length` or `start`). What can be changed in text: per clip `start`, `src=in-out`, `speed`,
`volume`, `mute`, fades, `label` and, on titles, `text` and `length`; moving a line under another track moves the clip; deleting a clip
line deletes the clip; per track its name, role, mute, hidden, locked and volume; deleting a marker line deletes the marker.
Adding clips, effects, keyframes or changing the media a clip plays stays with `timeline_edit`.

## Editing

- `timeline_text` returns the text and its `revision` (`track` narrows it; `truncated` says when it was cut at `max_chars`).
- `timeline_apply_text` takes `replacements: [{old, new}]` and the `expected_revision` that was read. Each `old` must match exactly
  once (an ambiguous one is refused with how many times it matched), the replacements apply all or nothing, a different revision is a
  `version_conflict`, and `dry_run` reports without saving. Before saving, the operations are run on a copy and the copy's text must
  be exactly the text asked for; an edit that would introduce an error (a clip asking for more media than exists) or a clip past the end
  is refused. One undo step, `ripple` is off unless asked.
- `timeline_check` is the same with no save: whether it applies to the revision, the operations, what changes, the new duration, issues.
- `timeline_edit` also takes `dry_run` (reports `would_be_rev` and the changes) and `expected_revision`.

## Cut proposals

`cut_proposals` finds filler words (a vocabulary per language plus drawn-out hesitations such as "eeeh", "ejm", "ehmm") and
immediate repeats, and pauses longer than `min_pause_ms` between words. Nothing is cut: the result is a **separate document**
(`cp_xxxxxxxx`, in the project) of proposals `{id, kind, words, text, confidence, reason, at}` that name **word ids from the
transcript, never times**. Confidence for a filler starts at 0.9 (a listed word or hesitation) or 0.72 (an immediate repeat) and is scaled by the transcriber's
own word probability; for a pause it grows with its length (0.55 up to 0.95) and drops by 0.15 when it follows the end of a sentence,
because that pause is often natural.

- `get` (optionally `min_confidence`, `kind`), `reject` (`ids` or `max_confidence`), `accept` (`ids`, or every open one above
  `min_confidence`), `list`. Accepting is **one edit, one undo step** (`dry_run` reports the time that would go); undoing it reopens
  the accepted proposals.
- The document **expires** when the transcript of any of its media changes (a new transcription): `get` says so and `accept` is
  refused with `proposals_expired`; make them again with `create`.
- Proposals need a transcript (`media_analyze` kind `transcript`). Words that are no longer on the timeline are skipped.

What the proof measured (private server, a 15 s Spanish recording with fillers and pauses, transcribed locally): the proposals found
"eeeh", "ejm", the repeated "que" and two pauses of 2.1 and 2.3 s; accepting four of them cut 3.49 s in one step and an undo
restored the 15 s; re-transcribing with another model made the document expire. A transcriber can miss fillers: the smaller model
dropped the two hesitations, so what is proposed depends on what was transcribed.
