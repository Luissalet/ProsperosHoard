/** Brightness / saturation keyframes for the first enabled eq — shared by WebGL (scene) and CSS preview (layers). */
import { FX_DEFAULTS } from "./fxspec.js";
import { clamp, kfRender } from "./gl/mathx.js";

export const EQ_KEY_RANGE = { brightness: [-1, 1], saturation: [0, 3] };

/** Active eq key props for this clip (keys ignored when every eq filter is disabled). */
export function activeEqKeyProps(clip) {
  const eqs = (clip.filters || []).filter((f) => f.type === "eq");
  const kf = clip.keyframes || {};
  if (eqs.length && !eqs.some((f) => f.enabled !== false)) return [];
  return Object.keys(EQ_KEY_RANGE).filter((k) => kf[k] && kf[k].length);
}

/** Apply brightness/saturation keys at clip-local `local` ms onto eq params (copy). */
export function applyEqKeys(clip, local, params = {}) {
  const kf = clip.keyframes || {};
  const keyed = activeEqKeyProps(clip);
  const p = { ...FX_DEFAULTS.eq, ...params };
  for (const k of keyed) {
    p[k] = clamp(kfRender(kf[k], local, p[k]), EQ_KEY_RANGE[k][0], EQ_KEY_RANGE[k][1]);
  }
  return { params: p, keyed };
}

/**
 * Filter list for CSS preview with the same eq-key rules as scene.effectList:
 * keys drive the first enabled eq; with keys and no eq, a default eq is synthesised; all-eq-off → keys inert.
 */
export function filtersWithEqKeys(clip, local) {
  const filters = clip.filters || [];
  const { keyed } = applyEqKeys(clip, local, {});
  if (!keyed.length) return filters;
  const eqs = filters.filter((f) => f.type === "eq");
  if (!eqs.length) {
    const { params } = applyEqKeys(clip, local, FX_DEFAULTS.eq);
    return [{ type: "eq", enabled: true, params }, ...filters];
  }
  let done = false;
  return filters.map((f) => {
    if (f.enabled === false || f.type !== "eq" || done) return f;
    done = true;
    const { params } = applyEqKeys(clip, local, f.params || {});
    return { ...f, params };
  });
}
