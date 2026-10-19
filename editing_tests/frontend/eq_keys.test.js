import test from "node:test";
import assert from "node:assert/strict";
import { activeEqKeyProps, applyEqKeys, filtersWithEqKeys } from "../../client/src/editor/eqKeys.js";

const bright = [{ t: 0, v: -0.5 }, { t: 1000, v: 0.5 }];
const sat = [{ t: 0, v: 0 }, { t: 2000, v: 3 }];

test("activeEqKeyProps ignores keys when every eq filter is disabled", () => {
  const clip = {
    filters: [{ type: "eq", enabled: false, params: { brightness: 0.2 } }],
    keyframes: { brightness: bright },
  };
  assert.deepEqual(activeEqKeyProps(clip), []);
});

test("applyEqKeys drives brightness at mid-point like kfRender", () => {
  const clip = { filters: [{ type: "eq", params: { brightness: 0, contrast: 1, saturation: 1, gamma: 1 } }], keyframes: { brightness: bright } };
  const { params, keyed } = applyEqKeys(clip, 500, clip.filters[0].params);
  assert.deepEqual(keyed, ["brightness"]);
  assert.ok(Math.abs(params.brightness - 0) < 1e-6);
});

test("filtersWithEqKeys synthesises a default eq when keys exist without an eq filter", () => {
  const clip = { filters: [{ type: "warm", params: { amount: 0.5 } }], keyframes: { saturation: sat } };
  const out = filtersWithEqKeys(clip, 0);
  assert.equal(out[0].type, "eq");
  assert.equal(out[0].params.saturation, 0);
  assert.equal(out[1].type, "warm");
});

test("filtersWithEqKeys applies keys only to the first enabled eq", () => {
  const clip = {
    filters: [
      { type: "eq", enabled: false, params: { brightness: 0.9, contrast: 1, saturation: 1, gamma: 1 } },
      { type: "eq", params: { brightness: 0.1, contrast: 1.2, saturation: 1.1, gamma: 1 } },
    ],
    keyframes: { brightness: bright },
  };
  const out = filtersWithEqKeys(clip, 1000);
  assert.equal(out[0].params.brightness, 0.9);
  assert.ok(Math.abs(out[1].params.brightness - 0.5) < 1e-6);
  assert.equal(out[1].params.contrast, 1.2);
});
