# Dedicated ComfyUI memory admission

The default admission check still compares the workflow's estimated VRAM
requirement with the existing free-memory estimate. That estimate includes
active PyTorch reservation but does not include every reusable DynamicVRAM
allocation. After a completed local QA render, observed telemetry yielded
5,836 MiB under this calculation; manual cache release yielded 15,116 MiB,
while active PyTorch reservation changed only from 10.49 to 9.5 MiB. This is
evidence of the estimate's limits, not a measured prediction for every model.

ComfyUI reports its primary device first. The estimate now uses that primary
accelerator instead of the freest device in the response. CPU entries are
ignored; a malformed primary is not replaced with a later GPU. Existing demo
and unknown-telemetry/nvidia-smi fallback behavior remains compatible; the
fallback does not confer permission for the managed-memory override.

For a **dedicated endpoint**, explicitly opt in in `backend.json`:

```json
{"comfy": {"url": "http://127.0.0.1:8189", "manage_memory": true}}
```

Only boolean true enables it. This declaration means ComfyUI may manage and
offload its own warm memory as part of the requested render. It is not consent
to unload models from any other service or to change LLM resident-only policy.
No `/free`, unload or interrupt request is made by this admission path.

When estimated free memory is insufficient, the override permits the render
only if the selected ComfyUI client's URL exactly matches the explicitly
configured URL, the primary device has valid integer telemetry and total VRAM
at least equal to the estimate, and that same client's `/queue` reports both
running and pending queues empty. Configured URL and opt-in are rechecked after
the probe. Another render-pool endpoint does not inherit the main endpoint's
opt-in. Demo mode never uses the override. Unknown/malformed telemetry, a
failed queue probe, busy queues, an undersized primary or changed configuration
cannot enable it. Legacy default handling of unknown telemetry is unchanged.

The method returns a separate admission decision; it does not report total
VRAM as free or invent a quantity of cached memory. Empty endpoint queues do
not prove the GPU is free of other processes. Total capacity at least equal
to the estimate does not guarantee success or prevent OOM. The probes provide
no exclusive lease, and another application may submit after the check;
configuration/probe/submission is not an atomic cross-application operation.
Only configure this policy for a dedicated endpoint whose resource use you
control. Errors remain normal job errors with existing submission receipts.

Tests use synthetic HTTP transport, the observed numeric snapshot, primary vs
secondary GPU capacity, malformed/CPU telemetry, busy/unknown/error queues,
strict opt-in, pinned URL/configuration changes, demo, render-pool isolation,
legacy test doubles and absence of mutation requests. This document does not
claim universal GPU success. Coordinated real QA on 2026-09-30 found that
the opt-in alone admitted a second Qwen-Image 2.1 int8 instruction edit, but
ComfyUI 0.37 / aimdo 0.5.5 then failed with an OOM. Admission is not an OOM fix.

For that dedicated local renderer, starting ComfyUI with
`--disable-smart-memory` (DynamicVRAM still enabled) allowed two consecutive
1024-square instruction edits, blue fedora then anime, in 98.03 and 81.96
seconds through the chat tool/adapter, HTTP jobs and owned gallery. Both had
accepted prompt receipts and finished outputs, visually inspected. No `/free`
request or manual cache release occurred between them. Before the second,
free telemetry was 15,844,507,644 bytes: this successful sequence did not need
the low-free override. The earlier OOM sequence exercised that override, while
synthetic tests verify its admission constraints. The launch flag controls
ComfyUI's own offload policy; it is not enabled on existing user services by
this patch. Other workflows, shared GPUs and versions need separate validation.
Evidence is in `D:/LocalAI/tmp/prospero-live-edit-20260930/`.

Sources: [original project](https://github.com/Luissalet/ProsperosHoard),
the installed ComfyUI `comfy/cli_args.py` and `comfy/model_management.py`
(`DISABLE_SMART_MEMORY` guards the DynamicVRAM keep-loaded branch).

Final validation: `.venv/Scripts/python.exe -m pytest -q`: 454 tests passed in
220.38 seconds, with three dependency deprecation warnings. The targeted new
memory/demo suite passed 30 tests. Inside `frontend`, `npm run build` passed
TypeScript checking and the Vite production build. Real consecutive-edit GPU
QA remains separately coordinated; these checks do not substitute for it.
