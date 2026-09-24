# Characters that stay the same

A character in Prospero's Hoard is a `characters` row (name, look prompt,
negative, palette, voice, canonical and reference images) plus a **kit**
(`characters.kit_json`, `prosperos_hoard/charkit.py`) with everything that
keeps it recognisable from shot to shot.

## Three ways to keep a character consistent

| Route | How | When |
| --- | --- | --- |
| Look prompt | `@Name` inlines the look description into the prompt | Always on; weakest |
| Reference edit | `consistent=true`: Qwen-Image 2.1 / Flux Kontext edit of the canonical image | No adapter yet; faithful face and costume, but drawn toward the canonical's pose and framing |
| Adapter (LoRA) | A LoRA trained on the character, loaded with its trigger word | Best identity with free poses; needs training once per architecture |

Adapters and the reference edit combine: a `consistent=true` render also
loads the adapter. `prefer_adapter=true` (productions use it for lead shots
unless `spec.lead_route="reference"`) renders txt2img + adapter instead,
when every mentioned character has one.

## The kit

```
trigger        rare word for the LoRA ("farolchr"), editable
use_adapters   inject enabled adapters automatically (default true)
adapters[]     {id, arch, lora_name, strength, trigger, enabled, installed, source, trained?}
dataset[]      {asset_id, caption, include, view?, source}
sheet          {asset_ids, contact_sheet_id, views, engine}
identity       {threshold}  (0-10, default 6.5)
good_seeds[]   seeds worth reusing
history[]      what changed and when (last 100)
library        {id, version} once saved to the library
```

One adapter per architecture is enabled at a time; older ones stay, disabled,
to go back to. Architectures: `qwen_image` (qwen21_* templates), `flux1`
(flux_schnell_txt2img, flux_kontext_edit), `sdxl`, `sd15`, `wan22_5b`
(wan22_ti2v, so production clips of the lead use a video adapter too) and
`z_image`. A custom workflow can declare `"arch"` in its params file.

Injection (`comfy_driver.inject_loras`) chains one `LoraLoaderModelOnly` per
adapter after every model loader and rewires the loader's MODEL consumers.
The LoRAs are part of the recipe's params, so "Reuse recipe" and "Vary seed"
load the same ones. An adapter whose file ComfyUI does not list is skipped
with a note (`adapter_notes`); with two or more characters in one frame each
adapter is scaled to 0.8 and at most three are loaded.

## Model sheet

`studio_character_sheet` redraws the canonical once per view with the edit
engine (Qwen-Image 2.1 or Flux Kontext; SDXL cannot keep identity and is
refused): front, three_quarter, profile, back, closeup, happy, angry, scared,
surprised, action, sitting, night (8 by default). Each image is tagged
`char:<id>`, `sheet`, `view:<name>`, goes into the dataset with a caption
from its view, and a labelled contact sheet is saved. If the character
already has an adapter for the edit engine, the sheet uses it.

## Dataset

Sources: `canonical`, `references`, `sheet`, `takes` (with `min_identity`
to skip drifted ones; rejected takes are never used). Captions follow one
rule: the trigger word stands for everything permanent (face, hair, body,
colours, signature outfit and props); the caption describes what changes
(pose, framing, angle, expression, background, light). `caption` asks the
vision model for exactly that; without one, captions are derived from the
render prompts with the @mention and the look removed.

The report blocks training with fewer than 8 images or captions without the
trigger, and warns about images under 512 px, blurry ones (Laplacian
variance), near duplicates (dHash distance <= 4) and missing basic views.

## Training

`prosperos_hoard/trainers.py` builds the command for the configured trainer:

| kind | What it runs | Notes |
| --- | --- | --- |
| `ai_toolkit` | `python run.py config.yaml` in the trainer folder | writes the job config (LoRA rank, steps, lr, 8-bit AdamW, quantised base, low VRAM, samples with the trigger) |
| `musubi` | `python -m musubi_tuner.<arch>_train_network ...` | writes the TOML dataset config |
| `custom` | your `command` list | placeholders `{dataset_dir} {output_dir} {name} {trigger} {steps} {rank} {lr} {resolution} {base_model} {arch}` |
| `fake` | `prosperos_hoard.devtools.fake_trainer` | demo and tests: progress lines and a tiny valid .safetensors |

The plan sizes steps to the dataset (about 100 per image, 500-3000), rank 16
(32 for SD/SDXL), 512 px by default - a LoRA trained at 512 applies at any
generation resolution and costs far less - and estimates VRAM and minutes on
a 16 GB card. The job exports the dataset (`NNN.png` + `NNN.txt`), picks the
freest GPU (or `training.gpu`) with enough memory - otherwise it waits in
`waiting_gpu` - sets `CUDA_VISIBLE_DEVICES`, streams the log
(`data/training/<run_id>/train.log`, tail via `action="log"`), and on
success copies the LoRA to `data/adapters/` and to
`<lora_dir>/prospero/<trigger>_<arch>_<run>.safetensors`, then attaches it.
Cancel kills the whole trainer process tree.

The base model for each architecture comes from `training.base_models`
(a local folder or a hub id the trainer can download); the plan says what is
missing before anything starts.

## Takes and identity

`studio_character_takes` lists every generated image or clip of the
character (tagged for it or whose recipe matched its @mention), grouped into
takes of the same shot (same template, view and prompt): take N of M, seed,
adapters used. `score` rates identity 0-10 against the canonical and up to
two references: with the vision model through Hoard Link (identity only -
pose, framing, light and background are ignored), otherwise a rough colour
signature of the subject area, reported as `rough`. Scores are cached per
asset and invalidated when the reference set changes. Actions: `canonical`,
`reference`, `unreference`, `dataset`, `reject`, `unreject`.

## Packs and the library

A `.hoardchar` is a zip: `character.json` (character + kit, images referred
to by pack path), `images/`, `sheet/`, `dataset/`, `adapters/` (optional,
can be large) and `preview.png`. Import validates member paths and the
format version, renames on a name clash, imports images as assets and
installs adapters into `lora_dir` (or keeps them in `data/adapters/` and
says so). An adapter exported without weights is trusted by name and used
only if ComfyUI already has the file.

The library (`data/library/characters/<lib_id>/`) keeps `v1.hoardchar`,
`v2.hoardchar`... with a `meta.json` that records what changed between
versions (look, adapters, dataset size). `use` casts any version into a
project (the "Casting" project when none is given); a recipe run accepts a
library lead, `cast={"lead": "lib_..."}` or `{"lead": {"library": "lib_...",
"version": 2}}`.
