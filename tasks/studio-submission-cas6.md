# Prospero929: retain pooled and global HTTP clients on failed shutdown

Independent CAS5 review found two real internal closures that still hid errors: Backend.close cleared its pooled/retired owners before aclose and swallowed failures; the owned GlobalSync delegated to the SDK close that swallowed its inner client's aclose. Both original installed witnesses and the CHANGES vote remain preserved.

Backend.close now records unique actual client objects, removes each only after successful closure, and keeps pool/retired Link references until every client and its owning loop have finished. GlobalSync has a product-owned strict close: retain inner/loop/thread after an error, close the client/executor on that loop, join/close the loop, then clear ownership. No SDK file changes. Editing's async lifecycle also closes its parent Link client after the checkpointed synchronous services close.

New real-resource regressions cover current pooled ComfyClient, current httpx inner, retired pooled client and retired inner after Backend.reload. Each fails aclose twice without network access: both OFF errors retain app/loop/thread and reject ON; the third OFF calls the real closure and clears all owners. These are actual client/Link objects, not substitutes for their production closure.

27 focused source regressions pass;13 existing shutdown/render-pool tests pass separately, including the native app's no-thread-left shutdown test. The new wheel is installed into an external target and all eight shutdown/control regressions pass with Python-I and product imports exclusively from TARGET.

Final Windows x64 wheel:37,919,789bytes; SHA2564d5455748a367a6bce1955e14f9c400aedac8a4a0e7601bffc9e33bebbf152b9. Private logs:prospero-cas6-client-tests.log,prospero-cas6-existing-backend-tests.log,prospero-cas6-installed-retry.log. Target:shutdown-fixed-installed/prospero-cas6. UI/assets/native worker bytes and cold catalog are unchanged from CAS5's fully installed verifier/CUA, and those are separate evidence rather than repeated claims of every format/provider.

No model/render/GPU, personal profile, integration, real migration934 or publication is part of this delivery. Root requests a fresh review at exact CAS6; no earlier vote is reused.
