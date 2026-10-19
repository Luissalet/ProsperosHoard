# Task929 — second source submission

Parent CAS1 `d412923306e46d605b354dd55da0239071e19c3e` remains unchanged. This delivery addresses the coordinator's independent CHANGES1 report, not a self-review. Read and fix submission_revision2 before independent tests/vote. The original full report and acceptance boundaries remain in `studio-submission.md`.

## P1: cancellation must finish native cleanup

The consumer and transition cancellation handlers now join the same owned task through a small local cancellation helper. A shielded AnyIO scope suppresses level-triggered outer cancellation during the join, and every repeated asyncio cancellation is absorbed until the shielded gather completes. Caller cancellation is then propagated. No second cancel is injected into the native task's `finally`, and the module retains ownership of the request until it really finishes. The helper is an owned adaptation of the Writer b818 cancellation correction, with provenance in its source and no dependency on another repository/worktree.

The causal regression retains the actual native `finally` on an event: cancel consumer, wait until cleanup entered, cancel again, request OFF, observe phase `stopping`, retain the request, verify caller/OFF/owner close are pending and another module remains responsive, then release cleanup. Exactly one close observes cleanup complete and OFF is finally published. Separate held ON/OFF transition tests inject cancellation twice. An AnyIO CancelScope test verifies the same ownership rule.

## P2: native text and snapshots must not be rewritten

Scalar `scoped` now handles only an explicitly selected link. JSON uses a separate traversal with a finite allowlist of derived HTTP URL fields and the native media `urls` structure (`play`, `file`, `poster`, `sprite`, `waveform`). Opaque native documents, snapshots, manifests, parameters, settings, metadata and provenance remain exact; other string values, titles, prompts, file paths and IDs are never rewritten. HTML still scopes URL attributes only. Scope is idempotent for already prefixed URLs.

The mixed JSON regression covers relative/absolute owner links, prefixed links, native media download structures, non-link values beginning `/api/` or `/assets/`, and opaque nested document/snapshot/manifest/parameter URLs. It exercises the actual ASGI response transformation as well as the pure traversal.

## Evidence and limits

- `cas2-regressions.txt`: **19 passed, 2.55s**, new causal tests plus existing shell/native store transfer/reopen and seven inert global consumer tests. No listener, model, GUI, GPU or user data.
- `cas2-mcp-log.txt` and `studio-mcp-verification.json`: actual stdio MCP/private ASGI/native synthetic stores passes cold168 discovery, real native project transfer, legacy internal export, OFF/read/XML range206 and reopening the same editor ID. This is a rerun after the corrections; media preparation remains inert.
- No native/frontend source changed; the existing binary/build hashes from CAS1 are retained and no Rust/model process was started. No integration, push or real-data transition.
- CUA, common-control English, prefixed standalone preview-helper QA, real creative-engine opt-ins, optional models/hardware, real identity/data continuity and Faustus938 deployment remain pending as recorded in CAS1. These corrections do not certify those acceptance steps or turn earlier broad failed runs into passing final-suite runs.
