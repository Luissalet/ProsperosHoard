# Installation details in tracked files

Use relative paths or placeholders such as `<workspace>` in documentation and
fictional accounts and hosts in test fixtures. Keep raw test output in the
local QA directory, outside the checkout.

Run `python -m pytest tests/test_installation_privacy.py` from a Git worktree.
The check scans tracked text and reports the file, line and kind of each
match. On Windows it uses the current account. It reads the private DNS
suffix from local Tailscale status when available. To check a specific private
network in CI or without Tailscale, provide its suffix through the
`HOARD_TEST_PRIVATE_DNS_SUFFIX` environment variable.
