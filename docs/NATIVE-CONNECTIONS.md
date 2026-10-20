# Native MCP and family connections

The absorbed modules (editing) retain their original MCP tool names and schemas inside this owner. Discovery reads the shipped static catalogue and does not open a profile, import a native engine, start a server or enable a module.

Run the installed owner Python with `-m prosperos_hoard.compat_mcp --module <module>`. Configure `PROSPERO_URL` to the existing owner URL (default `http://127.0.0.1:8815`) and `PROSPERO_TOKEN_FILE` to its current token file. Keep the existing MCP connection ID and its disabled state. Native token files remain preserved in the migrated profile; this adapter uses the owner's current token and never rotates either identity.

A tool call makes one request to the already running owner. Start the owner and enable only the requested module explicitly; an OFF module remains OFF and returns its actual error. An uncertain write is reported as `outcome_unknown` and is not repeated. Native object IDs, arguments, errors and image content remain intact.

The same connection is available to the Hub under `/compat/<module>`:

- `GET /api/agent/tools`: cold native catalogue.
- `POST /api/agent/call`: native tool name, original arguments and family context.
- `GET /api/health`: owner service descriptor without starting the module.

A retired Hub identity points at the owner's `/compat/<module>` URL and existing token file, with no launch command. Its ID and manual-stop flag remain unchanged. `x-family.standalone` and `x-family.absorbed_by.standalone` must both be false, and `absorbed_by.id` names this owner. Editor and native REST paths remain under `/<module>/`; compatibility discovery uses the cold `/compat/` surface.

`HOARD_AGENT_ID` and `HOARD_AGENT_SESSION` retain the native `x-agent-id` and `x-agent-session` headers. Optional request metadata `hoard/agent`, `hoard/session`, `hoard/reason` and `hoard/caller` carries per-call context. The implementation supports MCP 1.9 and later 1.x without importing the retired checkout.
