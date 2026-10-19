import React, { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { GLCompositor } from "./gl/compositor.js";
import { lutFor } from "./gl/cube.js";
import { even } from "./gl/mathx.js";
import { buildScene, outputSize } from "./gl/scene.js";

const fetchLut = (path) => fetch(`/editing/api/luts?path=${encodeURIComponent(path)}`).then((r) => (r.ok ? r.text() : Promise.reject(new Error("lut"))));

// The accelerated preview: one WebGL canvas that composites every visible layer the way the render does (see gl/scene.js and
// gl/compositor.js). The media elements stay in ClipLayer (sound, clock sync); this only reads their frames.
export default function GLStage({ doc, media, box, pb, registry, onFail }) {
  const canvas = useRef(null);
  const comp = useRef(null);
  const raf = useRef(0);
  const live = useRef({});
  const statsRef = useRef({ draws: 0, ms: 0 });
  const [forced, setForced] = useState(0);
  const dpr = Math.min(2, window.devicePixelRatio || 1);
  const width = forced || Math.min(1920, Math.max(160, even(box.w * dpr)));
  const out = useMemo(() => outputSize(doc.canvas, width), [doc.canvas, width]);
  live.current = { doc, media, out };

  const draw = useCallback(() => {
    raf.current = 0;
    const c = comp.current;
    const L = live.current;
    if (!c || !L.doc) return null;
    const scene = buildScene(L.doc, L.media, pb.t, L.out);
    const sources = new Map();
    for (const it of scene.items) {
      for (const layer of it.kind === "layer" ? [it.layer] : [it.a, it.b]) if (layer) sources.set(layer.key, registry.get(layer.clipId));
    }
    const t0 = performance.now();
    const res = c.render(scene, sources, {
      lut: (path) => {
        const entry = lutFor(path, fetchLut);
        if (entry.state === "loading") entry.waiters.add(() => schedule()); // eslint-disable-line no-use-before-define
        return entry;
      },
    });
    statsRef.current.draws += 1;
    statsRef.current.ms += performance.now() - t0;
    L.last = { scene, res, sources };
    return L.last;
  }, [pb, registry]); // eslint-disable-line react-hooks/exhaustive-deps
  const drawRef = useRef(draw);
  drawRef.current = draw;
  const schedule = useCallback(() => {
    if (!raf.current) raf.current = requestAnimationFrame(() => drawRef.current());
  }, []);

  useEffect(() => {
    let c;
    try {
      c = new GLCompositor(canvas.current);
    } catch (error) {
      onFail?.(error);
      return undefined;
    }
    c.onLost = () => onFail?.(new Error("WebGL context lost"));
    comp.current = c;
    registry.notify = schedule;
    const unsub = pb.subscribe((_t, playing) => {
      if (playing) {
        // inside the playback clock's own frame: draw now so the picture and the sound clock stay together
        cancelAnimationFrame(raf.current);
        raf.current = 0;
        drawRef.current();
      } else schedule();
    });
    schedule();
    return () => {
      unsub();
      cancelAnimationFrame(raf.current);
      raf.current = 0;
      registry.notify = () => {};
      comp.current = null;
      c.dispose();
    };
  }, [pb, registry, schedule, onFail]);

  useLayoutEffect(() => { schedule(); }, [doc, media, out, schedule]);

  // Test hook: lets a script drive the playhead and the output width and read back the canvas (scripts/preview_check.py).
  useEffect(() => {
    const hook = {
      seek: (ms) => pb.seek(ms),
      setWidth: (w) => setForced(w),
      stats: () => ({ ...statsRef.current, videos: document.querySelectorAll("video").length }),
      // Mean time of `n` full redraws including the GPU work (a 1 pixel read forces it to finish): what one frame costs on this machine.
      bench: (n = 10) => {
        const gl = comp.current.gl;
        const t0 = performance.now();
        const px = new Uint8Array(4);
        let last = null;
        for (let i = 0; i < n; i++) { last = drawRef.current(); gl.readPixels(0, 0, 1, 1, gl.RGBA, gl.UNSIGNED_BYTE, px); }
        return { ms: (performance.now() - t0) / n, drawn: last ? last.res.drawn : 0, missing: last ? last.res.missing.length : -1 };
      },
      info: () => ({ width, W: out.W, H: out.H, t: pb.t, renderer: comp.current ? comp.current.gl.getParameter(comp.current.gl.VERSION) : null }),
      ready: () => {
        const last = drawRef.current();
        if (!last) return false;
        for (const it of last.scene.items) {
          for (const layer of it.kind === "layer" ? [it.layer] : [it.a, it.b]) {
            if (!layer) continue;
            const el = registry.get(layer.clipId);
            if (!el) return false;
            if (el.tagName === "VIDEO") {
              if (el.seeking || el.readyState < 2) return false;
              if (Math.abs(el.currentTime - layer.srcMs / 1000) > 0.004) return false;
            } else if (!el.complete) return false;
          }
        }
        return last.res.missing.length === 0;
      },
      snapshot: () => {
        const last = drawRef.current();
        return { png: canvas.current.toDataURL("image/png"), W: out.W, H: out.H, A: last ? last.scene.A : null, missing: last ? last.res.missing : [] };
      },
    };
    window.__lumiereGL = hook;
    return () => { if (window.__lumiereGL === hook) delete window.__lumiereGL; };
  }, [pb, registry, out, width]);

  return <canvas ref={canvas} data-gl-preview="1" style={{ position: "absolute", inset: 0, width: "100%", height: "100%", zIndex: 0, display: "block" }} />;
}
