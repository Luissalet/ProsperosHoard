import test from "node:test";
import assert from "node:assert/strict";
import { changedFields, buildContext, formSnapshot } from "./src/copilot-core.js";
import { mergeSettingsDraft, settingsFields } from "./src/settings-draft.js";

test("agent refresh and partial Save retain independent settings edits", () => {
  const original = settingsFields({ transcript_language: "es", default_export: "h264" });
  const draft = { ...original, transcript_language: "qa395-es" };
  const server = { ...original, default_export: "gif" };
  const refreshed = mergeSettingsDraft(original, draft, server);
  assert.equal(refreshed.transcript_language, "qa395-es");
  assert.equal(refreshed.default_export, "gif");
  assert.deepEqual(changedFields(settingsFields(server), refreshed), { transcript_language: "qa395-es" });
});

test("typing or clearing a field during Save remains pending against its response", () => {
  const submitted = settingsFields({ transcript_language: "es", export_folder: "before" });
  const current = { ...submitted, transcript_language: "", export_folder: "typed while saving" };
  const response = { ...submitted, default_export: "gif" };
  const merged = mergeSettingsDraft(submitted, current, response);
  assert.deepEqual(changedFields(settingsFields(response), merged), {
    transcript_language: "", export_folder: "typed while saving",
  });
  assert.equal(merged.default_export, "gif");
});

test("settings pending fields reach context whole with no fabricated project", async () => {
  const original = settingsFields({ transcript_language: "es" });
  const draft = { ...original, transcript_language: "qa395-es", export_folder: "q".repeat(5000) + "tail395" };
  const context = await buildContext({ hash: "#/settings", api: { project: () => { throw new Error("Unexpected project read"); } },
    ask: ({ provide }) => provide(formSnapshot("settings-form", { initial: original, current: draft })),
  });
  assert.deepEqual(context.active_objects, []);
  assert.equal(context.project, undefined);
  assert.equal(context.editor.fields.transcript_language, "qa395-es");
  assert.equal(context.editor.fields.export_folder, draft.export_folder);
});
