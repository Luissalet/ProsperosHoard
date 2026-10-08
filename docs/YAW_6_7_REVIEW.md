# YAW 6.7: independent video finishing

[Español](YAW_6_7_REVIEW.es.md)

Reviewed on 2026-10-06 from the user-provided ZIP. The single JSON is
`Hunyuan YAW Workflow by JSN-v67-5090-fix.json`, SHA-256
`62b06d0470658cac9a115d5d7f44bfbaf4f23f70e534e4e87d3335d723dbe816`.
It contains 426 UI nodes and 567 links. Notes and paths in the export were
read as source material, not executed or imported into studio data.

## What this version actually contains

| Stage | Evidence in the supplied graph | Prospero decision |
| --- | --- | --- |
| Frame interpolation | `FILM VFI`, node 86, `film_net_fp32.pt`, multiplier 2; output node 1180 at 48 fps | Add independent finishing of an existing asset. First implementation uses local CPU motion-compensated FFmpeg, **not FILM** |
| Preview pause | `Preview Chooser`, node 181, `Always pause` | Prospero already has reviewable animatics, draft clips and promotion; this custom node's interactive pause is not integrated |
| Sound generation | Two `MMAudioSampler` nodes, one standalone branch | Valuable future capability: sound effects conditioned on the video. Existing song/TTS tools do not provide this |
| Extension | Node 1436 selects source frames, node 1419 conditions I2V, node 1427 merges old/new frames | General video continuation is distinct from existing S2V audio extension and chained production shots |
| TeaCache | Four `TeaCacheHunyuanVideoSampler_FOK` nodes | Model-specific acceleration; not transferable to Wan 2.2 by copying this Hunyuan sampler |
| Prompts/LoRAs | Prompt saving, wildcards, two random-choice LoRA branches with trigger substitution | No temporal prompt interpolation was found; the word “interpolation” refers to frames |

The installed ComfyUI catalog was read without queueing a render or
restarting a service. It did not list FILM VFI, MMAudioSampler, the YAW
TeaCache sampler or VideoHelperSuite's load/combine nodes. Dry conversion
with the real catalog failed at missing `MathExpression|pysssss` (node 97).
GetNode/SetNode also need frontend variable resolution that Prospero's
converter does not currently implement. Raw UI node count alone does **not**
prove the 400-node API limit is exceeded: muted/decorative nodes disappear.

## Available operation

`studio_interpolate(asset_id, fps=48, wait_s=0)` queues a cancellable CPU job
on an existing clip, without calling ComfyUI or a language model. Poll
`studio_job`. It records source and target fps, method and parent asset;
the original remains. Each call makes a new take. HTTP equivalents:
`POST /api/agent/studio_interpolate` and
`POST /api/assets/{asset_id}/interpolate`.

The method is [FFmpeg minterpolate](https://ffmpeg.org/ffmpeg-filters.html#minterpolate),
with motion compensation and scene-cut detection. It keeps the clip's size
and pacing, converts its first audio track to AAC while preserving its
offset relative to the video, and trims the filter's
look-ahead padding to the actual source video duration. Target fps must be
above the source rate (finite, 1–120); at least three source frames are
needed. Difficult motion can produce interpolation artefacts; this is not
a neural FILM-quality claim. There is no new library or model dependency.

## References and remaining work

- [Original listing](https://civitai.com/models/1134115?modelVersionId=1517699).
- [FILM ComfyUI adapter](https://github.com/Fannovel16/ComfyUI-Frame-Interpolation),
  recorded node version `c336f7184cb1ac1243381e725fea1ad2c0a10c09`.
- [MMAudio adapter](https://github.com/kijai/ComfyUI-MMAudio), recorded
  version `a49a1b8f382687c7dc9d7266a054dc3c6f992ccd`.
- [Hunyuan TeaCache adapter](https://github.com/facok/ComfyUI-TeaCacheHunyuanVideo),
  recorded versions `481ac1c2e987a8160b2ceb97bcfb00e2600957db` / `1.0.0`.

These repository references identify dependencies, not verified licences
for the complete supplied workflow and all model weights. No upstream
code or workflow was bundled. Future FILM/MMAudio adapters need their own
installation, compatibility and real-output evaluation. Intermediate
tensor caching is a separate idea found in newer author projects, not a
confirmed feature of this supplied 6.7 export.
