import test from "node:test";
import assert from "node:assert/strict";
import { contactSheetRequest } from "../../client/src/editor/contactSheet.js";
import { api } from "../../client/src/api.js";

test("contact sheet overview sends bounded settings and leaves frame selection to the renderer", () => {
  assert.deepEqual(contactSheetRequest("overview", "22", "96"), {
    mode: "overview", count: 16, width: 128,
  });
});

test("cut view delegates cut-side sampling to the renderer and forwards the frame budget", () => {
  assert.deepEqual(contactSheetRequest("boundaries", 3.3, 704.2), {
    mode: "boundaries", count: 3, width: 640,
  });
});

test("adaptive view is sent through the normal contact sheet request", () => {
  assert.deepEqual(contactSheetRequest("adaptive", "7", "256"), {
    mode: "adaptive", count: 7, width: 256,
  });
});

test("API client posts settings and preserves real sheet, manifest, and frame metadata", async () => {
  const oldWindow = globalThis.window;
  const oldFetch = globalThis.fetch;
  const fixture = {
    url: "/api/frames/sheet.jpg", png_url: "/api/frames/sheet.png",
    receipt_url: "/api/frames/sheet.json", html_url: "/api/frames/sheet.html",
    frames: [{ t_ms: 1024, time: "0:01.024", frame: 30, actual_time_ms: 1024, layers: [{ media_name: "A.mp4" }] }],
  };
  let request;
  globalThis.window = { location: { origin: "http://127.0.0.1" } };
  globalThis.fetch = async (url, init) => {
    request = { url: String(url), init };
    return { ok: true, text: async () => JSON.stringify(fixture) };
  };
  try {
    const result = await api.projectContactSheet("project / 1", { mode: "adaptive", count: 8, width: 256 });
    assert.equal(request.url, "http://127.0.0.1/api/projects/project%20%2F%201/contact-sheet");
    assert.equal(request.init.method, "POST");
    assert.deepEqual(JSON.parse(request.init.body), { mode: "adaptive", count: 8, width: 256 });
    assert.equal(result.png_url, fixture.png_url);
    assert.equal(result.receipt_url, fixture.receipt_url);
    assert.equal(result.html_url, fixture.html_url);
    assert.equal(result.frames[0].time, "0:01.024");
    assert.equal(result.frames[0].layers[0].media_name, "A.mp4");
  } finally {
    globalThis.window = oldWindow;
    globalThis.fetch = oldFetch;
  }
});
