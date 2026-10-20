# Text timing

## Overview

Task #1057 adds a shared **Operate** editor inside the existing song track and script desk. It inherits Prospero's plum surfaces, magenta actions, shared modal and labelled fields. This local record leaves root DESIGN.md and the existing Studio comp gates unchanged.

The task is sequential: listen, mark each line's start, mark the final end, correct individual values, then explicitly save and link.

## Colors

Shared accent and accent-soft variables identify marking actions, keyboard focus and the current line. Warning pills distinguish Untimed, Start missing, End missing and Review. Errors use the shared alert treatment.

## Typography

The editor inherits the current Source Sans 3 working font, serif modal heading and mono playback clock. Line numbers and numeric inputs use tabular numerals. Root DESIGN.md's older body-font description is not refreshed here.

## Layout

The wide shared modal contains instructions, a sticky wrapping audio player, wrapping actions and a divided line list. Desktop rows place text beside two 88px timing columns. At 600px and below, text spans the row and the two timing fields sit beneath it; audio occupies its own row. The shared modal keeps its footer outside the scrolling body.

## Components

Listen and mark arms keyboard marking and focuses the editor. Space marks only while armed, ignores repeated keydown events and leaves native controls their normal keyboard behavior. Each sequential mark starts the next line and closes the preceding one; an extra mark ends the final line.

Selecting a line pauses playback, seeks to its known start and prepares that line for remarking. Start and end remain directly editable in seconds. Playback offers 0.5×, 0.75× and 1× speed plus a three-second rewind.

A line with either bound missing displays a warning, never the completion check. The footer counts rows with both bounds. Save permits wholly untimed rows but rejects partial bounds, invalid ranges and overlaps, naming the first affected line. Working status and errors use status and alert roles.

Draft rows are retained in sessionStorage by production context and audio ID, and restored only when the line-text signature matches. Successful save removes that timing draft. Opening the editor captures its audio ID and rows; save uses that captured source.

SongTrack exposes Sync lyrics and optional word alignment. Alignment requests a preview, checks returned line text and count, and updates the local draft before explicit saving. Its manual save links the captured lyrics and song. ScriptDesk exposes Time while listening without automatic alignment; saving retains segment metadata and sends the captured source-song ID.

## Do's and Don'ts

- Do retain explicit labels, unfinished-line warnings and the separate save-and-link action.
- Do preserve the captured text/audio context and existing segment metadata.
- Don't interpret a complete pair of fields as validated timing; validation runs on save.
- Don't describe session recovery as durable storage or alignment preview as a saved link.

Evidence: D:/LocalAI/research-inbox/prospero-lyrics1057/timing-desktop.png and timing-mobile.png show the dark editor at desktop and mobile widths, including the corrected End missing warning. The parent reports a scoped ship verdict for that correction. These captures do not establish light-theme coverage, model quality or closure of root comp gates. This documentation pass inspected source and captures without launching apps, models or tests.
