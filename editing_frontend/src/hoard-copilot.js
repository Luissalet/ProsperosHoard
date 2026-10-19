// Hooks the shared Faustus copilot launcher into this app: it merges context() into every turn and calls
// onTurnEnd() when an agent turn finishes. Domain knowledge only; the panel itself is injected by the server.
import { api } from "./api.js";
import { CONTEXT_REQUEST, REFRESH_EVENT, buildContext } from "./copilot-core.js";
import { captureViewFields } from "./copilot-view.js";

const ask = (detail) => window.dispatchEvent(new CustomEvent(CONTEXT_REQUEST, { detail }));

window.hoardCopilot = {
  async context() {
    try {
      const formValues = captureViewFields(window.document);
      const ctx = await buildContext({ hash: window.location.hash, api, ask });
      ctx.view = { ...(ctx.view || {}), form_values: formValues };
      // Keep the form snapshot in the view alongside the other native UI values.
      if (ctx.editor) {
        ctx.view = { ...(ctx.view || {}), editor: ctx.editor };
        delete ctx.editor;
      }
      return ctx;
    } catch {
      return {};
    }
  },
  // The agent may have changed projects, media, transcripts or renders: let the mounted views reload in place.
  onTurnEnd() {
    window.dispatchEvent(new Event(REFRESH_EVENT));
  },
};
