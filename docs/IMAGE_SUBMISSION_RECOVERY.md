# Interrupted image submissions

The image worker commits a ComfyUI submission intent to the job's SQLite row
before posting a workflow. After the response it commits the returned prompt
ID. The receipt contains the client ID, workflow SHA256, phase and physical
endpoint with userinfo, query and fragment removed. A failed intent commit
prevents the POST; a lost response or failed acknowledgement commit leaves the
previous committed intent in place. The additive schema migration preserves
existing jobs.

At startup, `generate_image` and `edit_image` jobs left running are held instead
of rerun, including legacy jobs without a receipt: missing evidence does not
prove that nothing was submitted. Queued/waiting image jobs with a receipt are
also held. Waiting images without a receipt and other job types retain the old
requeue behavior. Terminal jobs and cancellation flags are preserved.

The existing schema and consumers support only queued, waiting_gpu, running,
done, failed and cancelled. The held job therefore uses `failed` with an
explicit `outcome_unknown` message. This is a compatibility representation of
an interrupted, uncertain result, **not evidence that the GPU operation failed**.
Repeated startup recovery does not change that receipt or rerun the operation.

This pilot deliberately does not automatically reconcile ComfyUI history,
resume downloads, delete partial assets or issue another GPU submission. It
records the most recent submission of the job (a render batch is capped at
eight); earlier batch receipts are not a complete recoverable journal yet.
The receipt does not retain provider credentials and its sanitized endpoint
must not be treated as a ready-to-replay authenticated URL.

Coverage applies to these two image job types using the production queue's
`Progress` recorder. Video/audio/production jobs, direct engine calls with a
plain progress function, multi-process queue ownership, global ComfyUI
interruption behavior, per-user access and recovery of every partial batch
output remain separate work. This change does not certify readiness to replace
another application's media journal or authorize cloud providers.

Validation uses real SQLite commits and the local fake ComfyUI HTTP server:
intent visible from a second connection before POST processing, successful
render and acknowledgement, commit failure without a POST, lost response after
accepted POST, acknowledgement-write failure, restart without another POST,
legacy schema migration, repeated recovery, safe waiting requeue, and terminal
job/cancellation preservation. No real model or external service is used.

Commands: `.venv/Scripts/python.exe -m pytest -q` and, inside `frontend`,
`npm run build`. Final complete suite: 428 tests passed in 219.92 seconds;
three existing dependency deprecation warnings. Targeted receipt/queue/store
suite: 31 tests passed. Frontend TypeScript and Vite production build passed.
