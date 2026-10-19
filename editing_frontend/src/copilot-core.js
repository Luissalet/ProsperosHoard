// Pure helpers behind window.hoardCopilot: no DOM or React at import time, so they run under plain `node --test`.

export const CONTEXT_REQUEST = "hoard:copilot-context-request";
export const REFRESH_EVENT = "lumiere:copilot-updated";

const NO_OBJECT = new Set(["", "new", "undefined", "null"]);

/** True when a route segment names no stored object ("new", empty, placeholders). */
export function isNoObjectId(id) {
  return id === undefined || id === null || NO_OBJECT.has(String(id).trim().toLowerCase());
}

/** Same grammar as the app's hash router: `#/<page>/<id>?query`. */
export function parseRoute(hash) {
  const [path] = String(hash || "").replace(/^#\/?/, "").split("?");
  const parts = path.split("/");
  let id = parts[1] || null;
  if (id) {
    try { id = decodeURIComponent(id); } catch { /* keep the raw segment */ }
  }
  return { page: parts[0] || "home", id: isNoObjectId(id) ? null : id };
}

const same = (a, b) => JSON.stringify(a) === JSON.stringify(b);

/** Keys of `current` whose value differs from `initial` (deep compare); a key missing from `current` is not reported. */
export function changedFields(initial, current) {
  const base = initial || {};
  return Object.fromEntries(Object.entries(current || {}).filter(([key, value]) => !same(value, base[key])));
}

/** Snapshot a form hands to the copilot. `objectId` is omitted for create forms. */
export function formSnapshot(kind, { objectId, initial, current } = {}) {
  const fields = changedFields(initial, current);
  return { kind, ...(isNoObjectId(objectId) ? {} : { object_id: String(objectId) }), dirty: Object.keys(fields).length > 0, fields };
}

/** Ask every mounted form (they answer synchronously) and keep the most relevant answer: the last dirty one, else the last. */
export function collectEditor(dispatch) {
  const answers = [];
  const selected = [];
  try {
    dispatch({
      provide: (snapshot) => { if (snapshot && typeof snapshot === "object" && snapshot.kind) answers.push(snapshot); },
      provideSelection: (ids) => { if (Array.isArray(ids)) selected.push(...ids.map(String)); },
    });
  } catch { /* a broken form must not break the turn */ }
  const dirty = answers.filter((a) => a.dirty);
  const editor = dirty.length ? dirty[dirty.length - 1] : answers[answers.length - 1];
  return { editor, selected: [...new Set(selected)] };
}

const clipTitle = (clip, media) => {
  if (clip.type === "text") return clip.text ? `“${String(clip.text).slice(0, 80)}”` : clip.label || clip.id;
  const m = clip.media && media ? media[clip.media] : null;
  return clip.label || m?.name || clip.id;
};

/** Selected clips that really exist in the project document. */
export function selectedClipObjects(view, ids, url) {
  const wanted = new Set(ids || []);
  const out = [];
  for (const track of view?.doc?.tracks || []) {
    for (const clip of track.clips || []) {
      if (wanted.has(clip.id)) out.push({ kind: "clip", object_id: clip.id, title: clipTitle(clip, view.media), ...(url ? { url } : {}) });
    }
  }
  return out;
}

/**
 * Build `{ project?, active_objects?, editor? }` for the current view. Every part is optional: a failing read omits
 * that part, an id that does not resolve (or "new") means "no object", and the promise never rejects.
 *  - api: the app's client API (`api.project(id)`)
 *  - ask: `(detail) => void` that dispatches the context request to the mounted forms
 */
export async function buildContext({ hash, api, ask }) {
  const out = {};
  let route = { page: "home", id: null };
  try { route = parseRoute(hash); } catch { /* default route */ }

  let editor;
  let selected = [];
  try {
    const collected = collectEditor(ask);
    editor = collected.editor;
    selected = collected.selected;
  } catch { /* omit the editor part */ }

  // Always say which objects are open (possibly none): an empty list overrides the launcher's guesses from the URL,
  // which would otherwise report an id that does not exist.
  out.active_objects = [];
  if (route.page === "p" && route.id) {
    try {
      const view = await api.project(route.id);
      if (view && view.id) {
        const url = `#/p/${encodeURIComponent(view.id)}`;
        out.project = { external_project_id: String(view.id), title: view.name || String(view.id) };
        out.active_objects = [
          { kind: "project", object_id: String(view.id), title: view.name || String(view.id), url },
          ...selectedClipObjects(view, selected, url),
        ];
      }
    } catch { /* unresolved id or backend down: no object */ }
  }

  if (editor) out.editor = editor;
  return out;
}
