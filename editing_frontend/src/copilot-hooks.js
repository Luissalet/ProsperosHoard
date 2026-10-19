import { useEffect, useRef } from "react";
import { CONTEXT_REQUEST, REFRESH_EVENT, formSnapshot } from "./copilot-core.js";

/** Run `fn` (an existing reload function) when an agent turn ends. Nothing is remounted. */
export function useCopilotRefresh(fn) {
  const ref = useRef(fn);
  ref.current = fn;
  useEffect(() => {
    const run = () => { try { ref.current?.(); } catch { /* a reload must not break the others */ } };
    window.addEventListener(REFRESH_EVENT, run);
    return () => window.removeEventListener(REFRESH_EVENT, run);
  }, []);
}

/**
 * Let the copilot see a form's unsaved changes. `initial` is what the form started from (or its empty defaults for a
 * create form: pass no `objectId` then) and `current` its live values.
 */
export function useCopilotForm(kind, { objectId, initial, current }) {
  const state = useRef({ kind, objectId, initial, current });
  state.current = { kind, objectId, initial, current };
  useEffect(() => {
    const answer = (event) => {
      const s = state.current;
      event.detail?.provide?.(formSnapshot(s.kind, s));
    };
    window.addEventListener(CONTEXT_REQUEST, answer);
    return () => window.removeEventListener(CONTEXT_REQUEST, answer);
  }, []);
}
