import test from "node:test";
import assert from "node:assert/strict";
import {
  buildContext, changedFields, collectEditor, formSnapshot, isNoObjectId, parseRoute, selectedClipObjects,
} from "./src/copilot-core.js";

const view = {
  id: "prj_1", name: "Vlog", rev: 4,
  doc: { tracks: [
    { id: "v1", clips: [{ id: "clp_a", type: "media", media: "med_1" }, { id: "clp_b", type: "text", text: "Hola mundo" }] },
    { id: "a1", clips: [{ id: "clp_c", type: "media", media: "med_9", label: "Music" }] },
  ] },
  media: { med_1: { id: "med_1", name: "beach.mp4" } },
};
const apiOf = (impl) => ({ project: async (id) => impl(id) });
const noForms = () => {};

test("parseRoute follows the hash router and treats new/blank ids as no object", () => {
  assert.deepEqual(parseRoute("#/p/prj_1"), { page: "p", id: "prj_1" });
  assert.deepEqual(parseRoute("#/p/prj%201?x=1"), { page: "p", id: "prj 1" });
  assert.deepEqual(parseRoute("#/p/new"), { page: "p", id: null });
  assert.deepEqual(parseRoute("#/p/"), { page: "p", id: null });
  assert.deepEqual(parseRoute(""), { page: "home", id: null });
  assert.deepEqual(parseRoute("#/settings"), { page: "settings", id: null });
  assert.equal(isNoObjectId("NEW"), true);
  assert.equal(isNoObjectId("prj_1"), false);
});

test("changedFields reports only differing keys, deeply, including cleared values", () => {
  assert.deepEqual(changedFields({ a: 1, b: [1, 2], c: "x" }, { a: 1, b: [1, 3], c: "" }), { b: [1, 3], c: "" });
  assert.deepEqual(changedFields(undefined, { a: 1 }), { a: 1 });
  assert.deepEqual(changedFields({ a: 1 }, { a: 1 }), {});
});

test("formSnapshot: edit forms carry object_id, create forms do not", () => {
  const edit = formSnapshot("project-form", { objectId: "prj_1", initial: { name: "A" }, current: { name: "B" } });
  assert.deepEqual(edit, { kind: "project-form", object_id: "prj_1", dirty: true, fields: { name: "B" } });
  const draft = formSnapshot("project-draft", { initial: { name: "", media: [] }, current: { name: "Trip", media: [] } });
  assert.deepEqual(draft, { kind: "project-draft", dirty: true, fields: { name: "Trip" } });
  assert.equal("object_id" in formSnapshot("project-draft", { objectId: "new", initial: {}, current: {} }), false);
  assert.equal(formSnapshot("x", { objectId: "p", initial: { a: 1 }, current: { a: 1 } }).dirty, false);
});

test("collectEditor prefers the last dirty answer and gathers the selection", () => {
  const { editor, selected } = collectEditor(({ provide, provideSelection }) => {
    provide({ kind: "project-form", dirty: false, fields: {} });
    provide({ kind: "export-draft", dirty: true, fields: { preset: "web" } });
    provide({ kind: "other", dirty: false, fields: {} });
    provideSelection(["clp_a", "clp_a", "clp_b"]);
    provide(null);
  });
  assert.equal(editor.kind, "export-draft");
  assert.deepEqual(selected, ["clp_a", "clp_b"]);
  assert.equal(collectEditor(() => { throw new Error("boom"); }).editor, undefined);
});

test("project route: project, active project and selected clips come from the client API", async () => {
  const calls = [];
  const api = apiOf((id) => { calls.push(id); return view; });
  const ctx = await buildContext({ hash: "#/p/prj_1", api, ask: ({ provide, provideSelection }) => {
    provideSelection(["clp_b", "clp_c", "ghost"]);
    provide({ kind: "project-form", object_id: "prj_1", dirty: true, fields: { name: "New" } });
  } });
  assert.deepEqual(calls, ["prj_1"]);
  assert.deepEqual(ctx.project, { external_project_id: "prj_1", title: "Vlog" });
  assert.deepEqual(ctx.active_objects.map((o) => [o.kind, o.object_id, o.title]), [
    ["project", "prj_1", "Vlog"], ["clip", "clp_b", "“Hola mundo”"], ["clip", "clp_c", "Music"],
  ]);
  assert.equal(ctx.active_objects[0].url, "#/p/prj_1");
  assert.deepEqual(ctx.editor, { kind: "project-form", object_id: "prj_1", dirty: true, fields: { name: "New" } });
});

test("a new/invalid/unresolvable project id never calls the API with 'new' and never throws", async () => {
  const calls = [];
  const api = apiOf((id) => { calls.push(id); throw Object.assign(new Error("not found"), { status: 404 }); });
  assert.deepEqual(await buildContext({ hash: "#/p/new", api, ask: noForms }), { active_objects: [] });
  assert.deepEqual(calls, []);
  assert.deepEqual(await buildContext({ hash: "#/p/prj_gone", api, ask: noForms }), { active_objects: [] });
  assert.deepEqual(calls, ["prj_gone"]);
  assert.deepEqual(await buildContext({ hash: "#/p/x", api: apiOf(() => null), ask: noForms }), { active_objects: [] });
});

test("create forms are reported on any route, including with no object", async () => {
  const ctx = await buildContext({ hash: "#/", api: apiOf(() => { throw new Error("unused"); }), ask: ({ provide }) => {
    provide(formSnapshot("project-draft", { initial: { name: "" }, current: { name: "Reel" } }));
  } });
  assert.deepEqual(ctx, { active_objects: [], editor: { kind: "project-draft", dirty: true, fields: { name: "Reel" } } });
});

test("a failing form or a rejecting API leaves context() resolved", async () => {
  const ctx = await buildContext({ hash: "#/p/prj_1", api: { project: () => Promise.reject(new Error("down")) }, ask: () => { throw new Error("form"); } });
  assert.deepEqual(ctx, { active_objects: [] });
  assert.deepEqual(await buildContext({ hash: undefined, api: {}, ask: noForms }), { active_objects: [] });
});

test("selectedClipObjects ignores ids that are not in the document", () => {
  assert.deepEqual(selectedClipObjects(view, ["nope"]), []);
  assert.deepEqual(selectedClipObjects(null, ["clp_a"]), []);
  assert.equal(selectedClipObjects(view, ["clp_a"])[0].title, "beach.mp4");
});

test("window.hoardCopilot: context() never rejects and onTurnEnd fires the refresh event", async () => {
  const target = new EventTarget();
  const hits = [];
  globalThis.window = Object.assign(target, { location: { hash: "#/p/new", origin: "http://x.test" } });
  globalThis.fetch = async () => { throw new Error("offline"); };
  await import("./src/hoard-copilot.js");
  const hc = globalThis.window.hoardCopilot;
  assert.equal(typeof hc.context, "function");
  target.addEventListener("hoard:copilot-context-request", (e) => e.detail.provide({ kind: "project-draft", dirty: true, fields: { name: "N" } }));
  assert.deepEqual(await hc.context(), { active_objects: [], view: { form_values: [], editor: { kind: "project-draft", dirty: true, fields: { name: "N" } } } });
  globalThis.window.location.hash = "#/p/prj_9";            // backend unreachable: api rejects, context still resolves
  assert.deepEqual(await hc.context(), { active_objects: [], view: { form_values: [], editor: { kind: "project-draft", dirty: true, fields: { name: "N" } } } });
  target.addEventListener("lumiere:copilot-updated", () => hits.push(1));
  hc.onTurnEnd();
  assert.equal(hits.length, 1);
});
