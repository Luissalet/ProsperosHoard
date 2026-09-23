# Example production: "DON'T LOOK BACK" by FAROL

[Español](no-mires-atras.es.md) · [Back to the README](../../README.md#production-example)

A two-minute horror-rap single with an original character, made end to end
on one local machine by
[`scripts/productions/no_mires_atras.py`](../../scripts/productions/no_mires_atras.py).
It exists in two versions that share every picture and clip: **DON'T LOOK
BACK** in English (`--lang en`, the one shown here) and the first take,
**NO MIRES ATRÁS**, in Spanish (the default). The English version was made
with `--reuse-from` the Spanish run, so only its song, designs and cut were
made again.
The script drives Prospero **only through its MCP adapter**, the same path
Faustus uses. This page holds everything needed to understand or reproduce
it: the character bible, every prompt and negative, the seeds, the model and
sampler settings, the real timings, and the problems the run found (all
fixed in the app).

![Album cover: the lantern head close-up with the title in Prospero's typographic layer](../media/farol/cover.jpg)

| | |
| --- | --- |
| Hardware | ComfyUI 0.37 on **one 16 GB card** (an RTX 5060 Ti); Prospero, ffmpeg and the designer on the CPU |
| Image model | Qwen-Image 2.1 (int8): the reference sheet, the stills and the photocards; the edit template carries FAROL's reference |
| Video model | Wan 2.2 TI2V 5B: seven 5 s clips at 1280×704, 24 fps |
| Music model | ACE-Step 1.5 turbo: two 120 s takes per version |
| Lyric timing | word timestamps from faster-whisper (`small`, run outside Prospero), aligned to the written lyrics and imported as an LRC |
| Wall-clock | reference sheet 4.7 min · song 41 s · 48 stills 57 min · 7 clips ≈ 70 min on an idle card · 5 photocards ≈ 10 min · album art 5 s · two timelines with preview and 1080p final renders 6.5 min |
| Picked by eye | the canonical reference (sheet 4 of 4) and the best of three variants for each of the 12 shots |

## 1. The bible

**FAROL**: an urban night creature. Very tall and thin (2.3 m), made of wet
black paper and wire. Its head is an old oval paper street lantern (washi,
amber) with rips, a candle flame visible inside and a crooked smile painted
in faded red. Long fingers like bent umbrella ribs, a torn, too-long dark
raincoat. It is never shown mid-stride: always still, always a little
closer.

- Palette: sodium orange `#F28C28`, wet asphalt `#1B1D22`, fog grey `#8A9099`, faded smile red `#A33A2E`.
- Mood: quiet dread, cinematic, 35 mm, shallow depth of field, rain, fog, practical light only.
- Setting: a night-time, empty, Spanish-style street under sodium lamps, with wet asphalt and no readable signs or brands.
- Contrast: an idol-style photocard set, where the same creature appears in a glossy pastel studio shoot.

## 2. Character: reference sheet → canonical reference

Four seeds (1001…1004) of a 16:9 turnaround sheet through `qwen21_txt2img`.
The fourth read best (the candle is visible, the crooked red smile is there
and the fingers look like umbrella ribs), and its left third (the front view)
became FAROL's canonical reference: `studio_cast update canonical_asset_id +
canonical_crop=left_third`. Every later image of FAROL is an edit from that
crop (`consistent=true`, which routes to `qwen21_edit` with the reference as
`image_1`).

Look (every FAROL prompt starts from it):

```text
FAROL, a very tall and thin urban night creature about 2.3 meters tall, made of wet black paper and wire,
its head an old oval paper street lantern (washi, amber) with rips, a candle flame visible inside, a crooked
smile painted in faded red on the paper, long fingers like bent umbrella ribs, a torn too-long dark raincoat,
never shown mid-stride, always still, always a little closer; palette sodium orange, wet asphalt dark grey,
fog grey, faded smile red; quiet dread, cinematic, 35mm, shallow depth of field, rain, fog, practical light only
```

Sheet: the look, then `character turnaround reference sheet, three full-body
poses side by side on a neutral grey seamless studio backdrop: front view,
three-quarter view, back view, identical design and proportions in every
pose, even studio lighting, reference photography`.

Negative for the shots without FAROL (a txt2img has no reference to hold the
look): `cartoon, cute, bright daylight, sunny, comic style, text, watermark,
extra limbs, deformed hands`.

## 3. Song

`studio_compose` with ACE-Step 1.5 turbo, seeds 2001 and 2002; the first take
is the single in both languages.

- Tags: `dark trap, horror rap, eerie music box melody, detuned piano, heavy 808, half-time 140 bpm, whispered ad-libs, male rap vocals, english, minor key, cinematic, tape hiss, rain ambience` (`spanish` in place of `english` for NO MIRES ATRÁS).
- bpm 140, key F# minor, 4/4, language `en` (or `es`), 120 s.
- The lyrics are in the script (`SONG_LYRICS_EN`, `SONG_LYRICS`). Both tell the same story with the same sections (`[Intro]`, `[Verse 1]`, `[Pre-Chorus]`, `[Chorus]`, `[Verse 2]`, `[Bridge]`, `[Chorus]`, `[Outro]`), so one storyboard fits both. The hook: *Don't look back, don't look back, / the light behind you ain't the city's, that's a fact, / every streetlamp, one more step, / when it cuts out… it's already at your back.*

Timing: Prospero's own `studio_time_lyrics` estimates each line from the
section tags and the song's bars. For the final cut, the take was
transcribed with faster-whisper (`small`, word timestamps), each written
line was matched to the words (difflib), and the result was saved as an LRC
and imported (`--only timeline --lrc-path <file>`). 42 of the 48 English
lines matched a sung word; the other six (whispers, ad-libs) were
interpolated. The first chorus starts at 0:40.1, the bridge at 1:17.0 and
the last line at 1:42.0. One lesson from the aligner: give whisper only the
title as its prompt. Whisper treats the prompt as text it has already heard,
so prompting it with the opening lines made it skip them.

## 4. Stills: 12 shots

Each shot gets three 16:9 variants at 1344×768 (seeds `3000 + 10·n`, then +1,
+2) plus one 4:5 post (seed `3000 + 10·n + 1`). When FAROL is in frame, the
prompt is `@FAROL <shot>, <night look>` with `consistent=true` (Qwen edit
from the canonical reference). When it is not, the prompt is `<shot>,
<night look>` through txt2img, with the negative above.

Night look, appended to every shot:

```text
cinematic 35mm film still, night, sodium-vapour street lamps, wet asphalt, fog, light rain, shallow depth
of field, practical light only, deep shadows, subtle film grain, no text, no readable signs, no logos
```

![The best still of each of the 12 shots](../media/farol/stills.jpg)

| # | FAROL | Shot prompt |
| --- | --- | --- |
| 1 | yes | standing perfectly still under a far flickering sodium lamp at the end of an empty Spanish-style street at 2:15 a.m., seen small in the distance, fog |
| 2 | yes | seen over the shoulder of someone looking back down an empty wet street, standing under a closer sodium lamp, still, a little closer than before |
| 3 | yes | extreme close-up of the paper lantern head: the crooked painted smile, the candle flame inside, raindrops beading on the wet paper |
| 4 | no | close-up of a phone held in a trembling hand at night, the screen's cold glow on a wet frightened face, rainy street blurred behind, the screen itself blank |
| 5 | yes | reflected in the fogged glass of a night bus shelter, standing right beside the viewer's reflection, while the real bench beside the viewer is empty |
| 6 | yes | only its long paper fingers curling over a stairwell rail two floors up, seen from below, dim stairwell light |
| 7 | yes | seated perfectly still among empty plastic chairs in a laundromat at 3 a.m., washing machines spinning, flickering fluorescent tubes |
| 8 | yes | an amber lantern glow in an elevator mirror, just behind the viewer's shoulder, the lantern head barely visible |
| 9 | no | close-up of a wet doormat outside a flat door at night, a trail of wet footprints that are not human, long and thin like bent umbrella ribs, leading to the door |
| 10 | yes | seen from inside a dark flat through a rain-streaked window: standing on the empty street below, lantern lit, looking up at the window |
| 11 | yes | a quick fragmented insert: flickering sodium lamps, the crooked painted smile, long paper fingers and the candle flame, extreme close framing |
| 12 | no | a dark bedroom a second after the light switch was turned off, a hand still on the switch, the room slowly filling with a sodium-orange glow from the window |

Qwen edit settings: 25 steps, cfg 1, euler / simple, denoise 1,
`resolution` 1024, `custom_size` on with the explicit 1344×768 canvas.
About 70 s per image.

## 5. Clips: Wan 2.2 TI2V from the best stills

Shots 1, 2, 3, 5, 7, 10 and 12, each 121 frames at 24 fps (5.04 s), 1280×704.
Settings: 20 steps, cfg 5, shift 8, uni_pc / simple, seed `5000 + n`.
Each clip took about 9.5 min on the idle card.

<p><img src="../media/farol/clip-over-shoulder.gif" width="49%" alt="Clip 2: over the shoulder, FAROL still under the closer lamp, slow push-in">
<img src="../media/farol/clip-lantern.gif" width="49%" alt="Clip 3: the candle flame flickering inside the lantern, raindrops on the paper"></p>

| # | Motion prompt |
| --- | --- |
| 1 | light rain falling, the far lamp flickering, fog drifting; the tall figure under the lamp stands perfectly still and does not walk |
| 2 | light rain falling, a subtle slow push-in; the figure under the lamp stays perfectly still |
| 3 | the candle flame flickering gently inside the lantern, raindrops sliding down the paper |
| 5 | rain streaking down the glass, a slow push-in; the reflected figure stays perfectly still |
| 7 | a washing machine spinning, faint fluorescent flicker; the seated figure stays perfectly still |
| 10 | rain on the window glass, the street lamp flickering; the figure on the street stays perfectly still, looking up |
| 12 | the room slowly brightening into sodium orange, a very slow push-in |

**Stillness needs its own negative.** Wan's stock negative prompt lists
"static" and "motionless frame" among the things to avoid, so on the first
try FAROL walked towards the camera. For the shots where FAROL is visible
(1, 2, 5, 7, 10), the negative is the stock one without its stillness terms,
plus walking (`走路，迈步, walking, stepping, striding, moving figure, turning
around`). Everything else keeps the stock negative. The walking take of
shot 1 is still in the project if you want an "it moved" insert.

## 6. Photocards: the idol contrast

Five looks at 2:3 (seeds 4001…4005), each `@FAROL <look>, full-length
portrait, the whole figure in frame with clear empty space above the lantern
head` with `consistent=true`. The framing is left out for the photo-booth
strip. `studio_photocard_set` then lays out the fronts, the backs (each with
a sweet-creepy note) and a contact sheet. About 2 min per photo.

![The photocard set: five fronts and their backs](../media/farol/photocards.jpg)

| Role | Look (abridged; the full prompts are `IDOL_LOOKS` in the script) | Back |
| --- | --- | --- |
| Visual | pastel pink seamless backdrop, holding a bouquet of wilted roses, soft beauty lighting, magazine retouching, the candle flame glowing warmly | «thanks for coming» |
| Main Rapper | sitting on a wooden stool in a cream knit sweater, warm cream backdrop, the flame glowing softly | «wrap up, it's cold out» |
| Center | a peace sign with one long paper finger, photo-booth strip, four small frames, bright flash, baby blue curtain | «click!» |
| Lead Vocal | by a rainy window strung with warm fairy lights, soft bokeh, gentle smile painted on the lantern | «I can see you from here» |
| Maknae | holding a small hand-written note that says "sorry" with both paper hands, lilac backdrop | «sorry for following you» |

## 7. Album art

Prospero's own typographic layer (exact text, bundled fonts), in the night
variants, with the accent `#F28C28`:

- Cover (3000×3000): still 3, the lantern close-up. Title «DON'T LOOK BACK», artist «FAROL», «single».
- Tracklist back: the same still blurred, with «01 DON'T LOOK BACK 2:00» and the credits.
- Teaser poster: still 1, the establishing shot. «FAROL», tagline «Don't look back», line «Always a little closer».
- Lyric card: still 11, the chorus insert, with the start of the hook.

The Spanish version prints the same designs in Spanish: «NO MIRES ATRÁS», «Siempre un poco más cerca» and the backs «gracias por venir», and so on.

<p><img src="../media/farol/poster.jpg" width="32%" alt="Teaser poster: FAROL small under the lamp, the name in large type">
<img src="../media/farol/lyric-card.jpg" width="32%" alt="Lyric card: the four lines of the hook over the dark insert">
<img src="../media/farol/back.jpg" width="32%" alt="Tracklist back"></p>

## 8. The cut

`studio_timeline action=auto` for 9:16 and 16:9, fed with the 12 best stills
and the 7 clips:

- **Cutting:** a new shot on every sung line and at every section start. The intro, bridge and outro hold each shot for two bars, the verses change once a bar (or once a line, whichever comes first), and the chorus cuts every half bar. Each section draws from its own shot pool (`STORYBOARD` in the script); for example, the pre-chorus is the bus-shelter clip and then the over-the-shoulder still. That makes about 130 cuts in two minutes (131 in English, 132 in Spanish).
- **Finishing:** `sodium_night` grade, grain 0.3, vignette, a white flash plus a colour-split glitch on the chorus's strong downbeats, and horror karaoke captions (a condensed uppercase face, per-line jitter and progressive highlighting).
- **Renders:** a preview first, then the final: 1080×1920 and 1920×1080 H.264 with the song as AAC (CRF 18, about 150 MB per 2 min final).

![Frames of the 9:16 cut, from the intro whisper to the last line](../media/farol/cut-9x16.jpg)

## 9. What the run found (fixed in the app)

- **Qwen edits came out as noise.** The edit template fed a pure empty latent
  to the sampler at denoise 0.6, which the model renders as texture. Qwen
  edits now sample at denoise 1 unless you ask for a strength.
- **Videos timed out on a shared card.** A language model spilled onto the
  same GPU and one clip took 43 minutes, so the fixed 900 s wait failed it.
  Now the wait is 1 h for video, 30 min for audio and 20 min for images, and
  `PROSPERO_COMFY_TIMEOUT_S` overrides it.
- **A job waited behind its own renderer.** The VRAM gate read the freest
  card through nvidia-smi and counted ComfyUI's cached models as used. Now it
  reads the card ComfyUI really runs on (`/system_stats`), where cached models
  count as free.
- **A red stripe ran down every frame.** On ffmpeg 8, a round trip through
  planar RGB (`gbrp`) blacks out the right-most 8 columns of a 1080-wide
  frame, and the grade then tinted them red. The RGB-only grade filters now
  run on packed RGB, and the glitch uses `chromashift`, which stays in YUV.
- **The final grew to 820 MB.** Once the grade stopped going through planar
  RGB, the grain landed on the chroma planes too. That coloured speckle is
  nearly incompressible, and the 2 min 1080p final came out eight times
  bigger at the same CRF. Grain is luma-only now, the way film grain looks,
  and the final is down to about 150 MB.
- **Photocard heads were cut off.** A tall subject filled its 2:3 photo and
  the card cropped at a fixed point. Card fronts now use a subject-aware crop,
  and the idol prompts ask for headroom.
- **FAROL walked.** See the clips section above.

## Reproduce it

Start ComfyUI on a card with 16 GB or more, with Qwen-Image 2.1, Wan 2.2
TI2V 5B and ACE-Step 1.5 installed. Then start Prospero and, from the repo
root:

```powershell
# the whole production, resumable (each still, clip, card and render is checkpointed)
.venv\Scripts\python.exe scripts\productions\no_mires_atras.py --backend real --quality final
# the English version on top of it: reuses the pictures, makes the song, designs and cut again
.venv\Scripts\python.exe scripts\productions\no_mires_atras.py --backend real --quality final --lang en --reuse-from data\productions\no_mires_atras
# after aligning or re-timing the lyrics: redo only the cut with your LRC
.venv\Scripts\python.exe scripts\productions\no_mires_atras.py --backend real --quality final --lang en --only timeline --lrc-path C:\Users\<you>\Music\dont_look_back.lrc
```

Seeds are fixed, but the same seeds only give the same pictures on the same
models, ComfyUI version and card. Each asset's lineage (`studio_lineage`)
has its exact recipe.
