import React, { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { useApp } from "../App.jsx";
import { Icon } from "../components/ui.jsx";
import { useEd } from "./EditorContext.js";
import TimelineClip, { visibleRange } from "./TimelineClip.jsx";
import { clamp, clipDur, clipEnd, fmtMs, fmtRuler, frameMs, hasRamp, rulerStep, srcAt, trackRows } from "./time.js";

const HDR_W = 176;
const ROW_H = { video: 52, audio: 44, text: 34 };
const RULER_H = 26;
const MARK_H = 16;
const TOP_H = RULER_H + MARK_H;
const MIN_PPS = 1;
const MAX_PPS = 1600;
const SNAP_PX = 8;
const MIN_CLIP = 80;

const MARKER_COLORS = { note: "#F5B700", beat: "#c77dff", scene: "#3BA4F5", highlight: "var(--ok)", chapter: "#ff8a5c" };

// Trimming a clip with a speed curve: the edge moves along the curve, so the new source point is read through it.
function rampTrimCalc(clip, side, d, mediaDur, ripple) {
  const dur = clipDur(clip);
  if (side === "l" && !ripple) d = Math.max(d, -clip.start);
  const key = (side === "r") !== !!clip.reverse ? "src_out" : "src_in";
  let src = srcAt(clip, side === "r" ? clipEnd(clip) + d : clip.start + d);
  src = clamp(src, 0, mediaDur || Infinity);
  src = key === "src_out" ? Math.max(src, clip.src_in + 1) : Math.min(src, clip.src_out - 1);
  const props = { [key]: Math.round(src) };
  const ndur = clipDur({ ...clip, ...props });
  if (ndur < MIN_CLIP) return { d: 0, props: {}, start: clip.start, dur };
  const nd = side === "r" ? ndur - dur : dur - ndur;
  return { d: nd, props, start: side === "l" ? clip.start + nd : clip.start, dur: ndur };
}

function trimCalc(clip, side, d, mediaDur, isLen, ripple) {
  const s = clip.speed || 1;
  const dur = clipDur(clip);
  if (!isLen && hasRamp(clip)) return rampTrimCalc(clip, side, d, mediaDur, ripple);
  if (isLen) {
    if (side === "r") d = Math.max(d, MIN_CLIP - dur);
    else { d = Math.min(d, dur - MIN_CLIP); if (!ripple) d = Math.max(d, -clip.start); }
    const length = Math.round(side === "r" ? dur + d : dur - d);
    return { d, length, start: side === "l" ? clip.start + d : clip.start, dur: length, props: { length } };
  }
  let lo = -Infinity;
  let hi = Infinity;
  if (!clip.reverse) {
    if (side === "r") { lo = (clip.src_in + MIN_CLIP * s - clip.src_out) / s; hi = mediaDur ? (mediaDur - clip.src_out) / s : Infinity; }
    else { lo = -clip.src_in / s; hi = (clip.src_out - clip.src_in) / s - MIN_CLIP; }
  } else if (side === "r") {
    hi = clip.src_in / s; lo = (clip.src_in - clip.src_out + MIN_CLIP * s) / s;
  } else {
    lo = mediaDur ? (clip.src_out - mediaDur) / s : -Infinity; hi = (clip.src_out - clip.src_in - MIN_CLIP * s) / s;
  }
  if (side === "l" && !ripple) lo = Math.max(lo, -clip.start);
  d = clamp(d, lo, hi);
  const props = {};
  if (!clip.reverse) {
    if (side === "r") props.src_out = Math.round(clip.src_out + d * s); else props.src_in = Math.round(clip.src_in + d * s);
  } else if (side === "r") props.src_in = Math.round(clip.src_in - d * s);
  else props.src_out = Math.round(clip.src_out - d * s);
  const ndur = side === "r" ? dur + d : dur - d;
  return { d, props, start: side === "l" ? clip.start + d : clip.start, dur: ndur };
}

function gesture(onMove, onUp) {
  const up = (e) => {
    window.removeEventListener("pointermove", onMove);
    window.removeEventListener("pointerup", up);
    window.removeEventListener("pointercancel", up);
    onUp(e);
  };
  window.addEventListener("pointermove", onMove);
  window.addEventListener("pointerup", up);
  window.addEventListener("pointercancel", up);
}

export default function Timeline() {
  const { t, notify } = useApp();
  const ed = useEd();
  const { doc, view, pb, selection, setSelection, edit, actions, marks, setMarks } = ed;
  const scrollRef = useRef(null);
  const contentRef = useRef(null);
  const playheadRef = useRef(null);
  const headRef = useRef(null);
  const laneEls = useRef(new Map());
  const [pps, setPps] = useState(80);
  const [viewport, setViewport] = useState({ left: 0, w: 900, h: 300 });
  const [drag, setDrag] = useState(null);
  const [tip, setTip] = useState(null);
  const [guide, setGuide] = useState(null);
  const [rubber, setRubber] = useState(null);
  const [dropLane, setDropLane] = useState(null);
  const [snapOn, setSnapOn] = useState(true);
  const [editingTrack, setEditingTrack] = useState(null);
  const pendingScroll = useRef(null);
  const fitted = useRef(false);
  const ppm = pps / 1000;
  const rows = useMemo(() => trackRows(doc), [doc]);
  const duration = ed.duration;

  const live = useRef({});
  live.current = { doc, ppm, selection, snapOn, marks, viewport, pps };

  // ------------------------------------------------------------ geometry
  const rowTop = useMemo(() => {
    const map = {};
    let y = TOP_H;
    for (const r of rows) { map[r.id] = y; y += ROW_H[r.kind]; }
    map.__end = y;
    return map;
  }, [rows]);
  const contentEnd = useMemo(() => Math.max(duration, ...doc.tracks.flatMap((tr) => tr.clips.map(clipEnd)), 0), [doc, duration]);
  const laneW = Math.max(viewport.w - HDR_W, Math.ceil(contentEnd * ppm) + 360);

  // ------------------------------------------------------------ scroll / zoom
  useEffect(() => {
    const el = scrollRef.current;
    let raf = 0;
    const update = () => {
      raf = 0;
      setViewport((v) => (v.left === el.scrollLeft && v.w === el.clientWidth && v.h === el.clientHeight ? v : { left: el.scrollLeft, w: el.clientWidth, h: el.clientHeight }));
    };
    const onScroll = () => { if (!raf) raf = requestAnimationFrame(update); };
    el.addEventListener("scroll", onScroll, { passive: true });
    const ro = new ResizeObserver(onScroll);
    ro.observe(el);
    update();
    return () => { el.removeEventListener("scroll", onScroll); ro.disconnect(); cancelAnimationFrame(raf); };
  }, []);

  // Zoom requests are coalesced to one render per frame (wheel events and the slider can fire faster than that).
  const zoomReq = useRef(null);
  const zoomRaf = useRef(0);
  const zoomNow = useCallback(() => {
    zoomRaf.current = 0;
    const req = zoomReq.current;
    zoomReq.current = null;
    const el = scrollRef.current;
    if (!req || !el) return;
    const cur = live.current.ppm;
    const target = clamp(req.next, MIN_PPS, MAX_PPS);
    const cx = req.anchorX === undefined ? Math.min(el.clientWidth / 2, Math.max(HDR_W, el.clientWidth / 2)) : Math.max(HDR_W, req.anchorX);
    const tAnchor = (el.scrollLeft + cx - HDR_W) / cur;
    pendingScroll.current = Math.max(0, HDR_W + (tAnchor * target) / 1000 - cx);
    setPps(target);
  }, []);
  const zoomTo = useCallback((next, anchorX) => {
    zoomReq.current = { next, anchorX };
    if (!zoomRaf.current) zoomRaf.current = requestAnimationFrame(zoomNow);
  }, [zoomNow]);
  useEffect(() => () => cancelAnimationFrame(zoomRaf.current), []);

  useLayoutEffect(() => {
    if (pendingScroll.current !== null && scrollRef.current) {
      scrollRef.current.scrollLeft = pendingScroll.current;
      pendingScroll.current = null;
    }
  }, [pps]);

  const fit = useCallback(() => {
    const el = scrollRef.current;
    const total = Math.max(500, live.current.doc ? Math.max(ed.duration, 1) : 1000);
    const target = clamp(((el.clientWidth - HDR_W - 60) / total) * 1000, MIN_PPS, MAX_PPS);
    pendingScroll.current = 0;
    setPps(target);
    el.scrollLeft = 0;
  }, [ed.duration]);

  useEffect(() => {
    if (!fitted.current && duration > 0 && viewport.w > 300) { fitted.current = true; fit(); }
  }, [duration, viewport.w, fit]);

  useEffect(() => {
    ed.tlApi.current = { zoomIn: () => zoomTo((zoomReq.current ? zoomReq.current.next : live.current.pps) * 1.4), zoomOut: () => zoomTo((zoomReq.current ? zoomReq.current.next : live.current.pps) / 1.4), fit };
  }, [ed.tlApi, zoomTo, fit]);

  useEffect(() => {
    const el = scrollRef.current;
    const onWheel = (e) => {
      if (e.ctrlKey || e.metaKey) {
        e.preventDefault();
        const rect = el.getBoundingClientRect();
        zoomTo((zoomReq.current ? zoomReq.current.next : live.current.pps) * (e.deltaY < 0 ? 1.18 : 1 / 1.18), e.clientX - rect.left);
      } else if (e.shiftKey || Math.abs(e.deltaX) > Math.abs(e.deltaY)) {
        e.preventDefault();
        el.scrollLeft += e.shiftKey && !e.deltaX ? e.deltaY : e.deltaX;
      } else if (el.scrollHeight <= el.clientHeight + 1) {
        e.preventDefault();
        el.scrollLeft += e.deltaY;
      }
    };
    el.addEventListener("wheel", onWheel, { passive: false });
    return () => el.removeEventListener("wheel", onWheel);
  }, [zoomTo]);

  // ------------------------------------------------------------ playhead
  useEffect(() => {
    const place = (time, playing) => {
      const x = (time * live.current.ppm);
      if (playheadRef.current) playheadRef.current.style.transform = `translateX(${HDR_W + x}px)`;
      if (headRef.current) headRef.current.style.transform = `translateX(${x}px)`;
      const el = scrollRef.current;
      if (playing && el) {
        const px = HDR_W + x;
        if (px > el.scrollLeft + el.clientWidth - 40 || px < el.scrollLeft + HDR_W) el.scrollLeft = Math.max(0, px - HDR_W - 80);
      }
    };
    place(pb.t, false);
    return pb.subscribe(place);
  }, [pb, ppm]);

  // ------------------------------------------------------------ snapping
  const snapTargets = useCallback((excludeIds) => {
    const { doc: d, marks: mk } = live.current;
    const pts = [0, pb.t];
    for (const tr of d.tracks) for (const c of tr.clips) {
      if (excludeIds.has(c.id)) continue;
      pts.push(c.start, clipEnd(c));
    }
    for (const m of d.markers) pts.push(m.t);
    if (mk.in !== null) pts.push(mk.in);
    if (mk.out !== null) pts.push(mk.out);
    return pts;
  }, [pb]);

  const snapDelta = useCallback((edges, targets) => {
    if (!live.current.snapOn) return { delta: 0, at: null };
    const thr = SNAP_PX / live.current.ppm;
    let best = null;
    let at = null;
    for (const e of edges) {
      for (const p of targets) {
        const diff = p - e;
        if (Math.abs(diff) <= thr && (best === null || Math.abs(diff) < Math.abs(best))) { best = diff; at = p; }
      }
    }
    return { delta: best || 0, at };
  }, []);

  const timeAtClientX = useCallback((clientX) => {
    const rect = contentRef.current.getBoundingClientRect();
    return (clientX - rect.left - HDR_W) / live.current.ppm;
  }, []);

  // ------------------------------------------------------------ ruler: seek + scrub
  const onRulerDown = (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    pb.pause();
    const f = frameMs(doc.canvas.fps);
    const seekTo = (cx) => pb.seek(Math.round(Math.max(0, timeAtClientX(cx)) / f) * f);
    seekTo(e.clientX);
    gesture((ev) => seekTo(ev.clientX), () => {});
  };

  // ------------------------------------------------------------ clip gestures
  const laneAt = useCallback((clientY) => {
    for (const [id, el] of laneEls.current) {
      const r = el.getBoundingClientRect();
      if (clientY >= r.top && clientY < r.bottom) return id;
    }
    return null;
  }, []);

  const mediaOf = (clip) => (clip.media ? view.media[clip.media] : null);

  const onClipDown = useCallback((e, clip, track) => {
    if (e.button !== 0 || e.target.dataset.edge) return;
    e.stopPropagation();
    const { selection: sel, doc: d } = live.current;
    const additive = e.shiftKey || e.ctrlKey || e.metaKey;
    let ids = sel.ids;
    const was = ids.includes(clip.id);
    if (additive) {
      ids = was ? ids.filter((x) => x !== clip.id) : [...ids, clip.id];
      setSelection({ ids, track: null });
      return;
    }
    if (!was) { ids = [clip.id]; setSelection({ ids, track: null }); }
    if (track.locked) return;
    const clipMedia = clip.media ? view.media[clip.media] : null;
    if (e.altKey && clip.type !== "text" && clipMedia && clipMedia.kind !== "image") {
      // Slip: the block stays, the source material slides under it.
      const startX = e.clientX;
      const sp = hasRamp(clip) ? (clip.src_out - clip.src_in) / Math.max(1, clipDur(clip)) : clip.speed || 1;  // a curve slips at its mean speed
      const mdur = clipMedia.duration_ms || 0;
      let started = false;
      let lastD = 0;
      const clampD = (dMs) => {
        const lo = -clip.src_in / sp;
        const hi = mdur ? (mdur - clip.src_out) / sp : Infinity;
        return Math.round(clamp(dMs, lo, hi));
      };
      const slipMove = (ev) => {
        const dx = ev.clientX - startX;
        if (!started && Math.abs(dx) < 3) return;
        started = true;
        lastD = clampD(-dx / live.current.ppm);
        const patch = { src_in: Math.round(clip.src_in + lastD * sp), src_out: Math.round(clip.src_out + lastD * sp) };
        setDrag({ kind: "slip", id: clip.id, patch, d: lastD });
        setTip({ x: ev.clientX, y: ev.clientY, text: t("slip_tip", { a: fmtMs(patch.src_in), b: fmtMs(patch.src_out) }) });
      };
      gesture(slipMove, () => {
        setTip(null);
        if (!started || Math.abs(lastD) < 1) { setDrag(null); return; }
        edit([{ op: "slip", clip: clip.id, delta: lastD }], t("lbl_slip")).finally(() => setDrag(null));
      });
      return;
    }
    const movingIds = new Set(ids);
    const movers = [];
    for (const tr of d.tracks) for (const c of tr.clips) if (movingIds.has(c.id) && !tr.locked) movers.push({ clip: c, track: tr });
    const startX = e.clientX;
    const startY = e.clientY;
    let started = false;
    let last = null;
    const targets = snapTargets(movingIds);
    const single = movers.length === 1;
    const move = (ev) => {
      const dx = ev.clientX - startX;
      if (!started && Math.hypot(dx, ev.clientY - startY) < 4) return;
      started = true;
      let dMs = dx / live.current.ppm;
      const minStart = Math.min(...movers.map((m) => m.clip.start));
      dMs = Math.max(dMs, -minStart);
      const edges = movers.flatMap((m) => [m.clip.start + dMs, clipEnd(m.clip) + dMs]);
      const s = snapDelta(edges, targets);
      dMs = Math.max(-minStart, dMs + s.delta);
      setGuide(s.at);
      let trackId = null;
      if (single) {
        const hit = laneAt(ev.clientY);
        const dest = hit && d.tracks.find((tr) => tr.id === hit);
        const m0 = movers[0];
        if (dest && !dest.locked && dest.id !== m0.track.id && (m0.clip.type === "text" ? dest.kind === "text" : dest.kind === m0.track.kind)) trackId = dest.id;
      }
      last = { kind: "move", ids: [...movingIds], primary: clip.id, dMs, trackId };
      setDrag(last);
    };
    gesture(move, () => {
      setGuide(null);
      if (!started || !last) return;
      const { dMs, trackId } = last;
      if (Math.abs(dMs) < 1 && !trackId) { setDrag(null); return; }
      const ordered = [...movers].sort((a, b) => (dMs > 0 ? b.clip.start - a.clip.start : a.clip.start - b.clip.start));
      const ops = ordered.map((m) => ({ op: "move", clip: m.clip.id, start: Math.round(m.clip.start + dMs), ...(single && trackId ? { track: trackId } : {}) }));
      setDrag({ ...last, committing: true });
      edit(ops, ordered.length > 1 ? t("lbl_move_n", { n: ordered.length }) : t("lbl_move")).finally(() => setDrag(null));
    });
  }, [edit, setSelection, snapTargets, snapDelta, laneAt, t]);

  const startRoll = (e, left, right, track) => {
    setSelection({ ids: [right.id], track: null });
    const lm = view.media[left.media];
    const rm = view.media[right.media];
    const ls = left.speed || 1;
    const rs = right.speed || 1;
    let lo = -(clipDur(left) - MIN_CLIP);
    let hi = clipDur(right) - MIN_CLIP;
    if (lm && lm.kind !== "image" && lm.duration_ms) hi = Math.min(hi, (lm.duration_ms - left.src_out) / ls);
    if (rm && rm.kind !== "image") lo = Math.max(lo, -right.src_in / rs);
    const targets = snapTargets(new Set([left.id, right.id]));
    const startX = e.clientX;
    const cut = right.start;
    let started = false;
    let lastD = 0;
    const move = (ev) => {
      const dx = ev.clientX - startX;
      if (!started && Math.abs(dx) < 3) return;
      started = true;
      let d0 = dx / live.current.ppm;
      const s = snapDelta([cut + d0], targets);
      d0 = clamp(d0 + s.delta, lo, hi);
      setGuide(s.at);
      lastD = Math.round(d0);
      setDrag({ kind: "roll", left: left.id, right: right.id, d: lastD, cut, ls, rs, lm, rm });
      setTip({ x: ev.clientX, y: ev.clientY, text: `${lastD >= 0 ? "+" : "−"}${fmtMs(Math.abs(lastD))}` });
    };
    gesture(move, () => {
      setGuide(null);
      setTip(null);
      if (!started || Math.abs(lastD) < 1) { setDrag(null); return; }
      edit([{ op: "roll", clip: right.id, delta: lastD }], t("lbl_roll")).finally(() => setDrag(null));
    });
  };

  const onEdgeDown = useCallback((e, clip, track, side) => {
    if (e.button !== 0) return;
    e.stopPropagation();
    e.preventDefault();
    if (track.locked) return;
    if (e.ctrlKey || e.metaKey) {
      // Roll: the cut between two touching clips moves; both change, nothing else shifts.
      const sorted = [...track.clips].sort((a, b) => a.start - b.start);
      const i = sorted.findIndex((c) => c.id === clip.id);
      const left = side === "l" ? sorted[i - 1] : clip;
      const right = side === "l" ? clip : sorted[i + 1];
      if (left && right && Math.abs(clipEnd(left) - right.start) <= 1 && left.type !== "text" && right.type !== "text") {
        startRoll(e, left, right, track);
        return;
      }
    }
    const { selection: sel } = live.current;
    if (!(sel.ids.length === 1 && sel.ids[0] === clip.id)) setSelection({ ids: [clip.id], track: null });
    const media = clip.media ? view.media[clip.media] : null;
    const isLen = clip.type === "text" || media?.kind === "image";
    const targets = snapTargets(new Set([clip.id]));
    const startX = e.clientX;
    let started = false;
    let last = null;
    const move = (ev) => {
      const dx = ev.clientX - startX;
      if (!started && Math.abs(dx) < 3) return;
      started = true;
      const ripple = !ev.altKey && clip.type !== "text";
      let d0 = dx / live.current.ppm;
      const edge = side === "l" ? clip.start : clipEnd(clip);
      const s = snapDelta([edge + d0], targets);
      d0 += s.delta;
      const calc = trimCalc(clip, side, d0, media?.duration_ms || 0, isLen, ripple);
      setGuide(Math.abs(calc.d - d0) < 1 ? s.at : null);
      last = { kind: "trim", id: clip.id, side, calc, ripple, trackId: track.id };
      setDrag(last);
    };
    gesture(move, () => {
      setGuide(null);
      if (!started || !last) { setDrag(null); return; }
      const { calc, ripple } = last;
      if (Math.abs(calc.d) < 1) { setDrag(null); return; }
      let ops;
      if (side === "r") ops = [{ op: "trim", clip: clip.id, ...calc.props, ripple }];
      else if (ripple) ops = [{ op: "trim", clip: clip.id, ...calc.props, ripple: true }];
      else {
        const mv = { op: "move", clip: clip.id, start: Math.max(0, Math.round(calc.start)) };
        const tr = { op: "trim", clip: clip.id, ...calc.props, ripple: false };
        ops = calc.d < 0 ? [mv, tr] : [tr, mv];
      }
      setDrag({ ...last, committing: true });
      edit(ops, t("lbl_trim")).finally(() => setDrag(null));
    });
  }, [edit, setSelection, snapTargets, snapDelta, view.media, t]); // eslint-disable-line react-hooks/exhaustive-deps

  // ------------------------------------------------------------ rubber band on empty lane space
  const onLaneDown = (e) => {
    if (e.button !== 0 || e.target !== e.currentTarget) return;
    const origin = contentRef.current.getBoundingClientRect();
    const sx = e.clientX - origin.left;
    const sy = e.clientY - origin.top;
    const additive = e.shiftKey || e.ctrlKey || e.metaKey;
    const base = additive ? live.current.selection.ids : [];
    let moved = false;
    gesture((ev) => {
      const cx = ev.clientX - origin.left;
      const cy = ev.clientY - origin.top;
      if (!moved && Math.hypot(cx - sx, cy - sy) < 4) return;
      moved = true;
      const x0 = Math.min(sx, cx);
      const x1 = Math.max(sx, cx);
      const y0 = Math.min(sy, cy);
      const y1 = Math.max(sy, cy);
      setRubber({ x: x0, y: y0, w: x1 - x0, h: y1 - y0 });
      const ids = [...base];
      for (const r of rows) {
        const top = rowTop[r.id];
        if (top + ROW_H[r.kind] < y0 || top > y1) continue;
        for (const c of r.clips) {
          const cx0 = HDR_W + c.start * live.current.ppm;
          const cx1 = HDR_W + clipEnd(c) * live.current.ppm;
          if (cx1 >= x0 && cx0 <= x1 && !ids.includes(c.id)) ids.push(c.id);
        }
      }
      setSelection({ ids, track: null });
    }, (ev) => {
      setRubber(null);
      if (!moved) {
        if (!additive) setSelection({ ids: [], track: null });
        const f = frameMs(live.current.doc.canvas.fps);
        pb.seek(Math.round(Math.max(0, timeAtClientX(ev.clientX)) / f) * f);
      }
    });
  };

  // ------------------------------------------------------------ media dropped from the library
  const onLaneDrop = (e, track) => {
    setDropLane(null);
    const id = e.dataTransfer.getData("application/x-lumiere-media");
    if (!id) return;
    e.preventDefault();
    const rect = e.currentTarget.getBoundingClientRect();
    let at = Math.max(0, (e.clientX - rect.left) / live.current.ppm);
    const s = snapDelta([at], snapTargets(new Set()));
    at += s.delta;
    actions.addMedia(id, { track: track.id, at, mode: "overwrite" });
  };

  // ------------------------------------------------------------ what is drawn
  const clipIndex = useMemo(() => {
    const idx = new Map();
    for (const tr of doc.tracks) for (const c of tr.clips) idx.set(c.id, { c, tr });
    return idx;
  }, [doc]);

  const ghosts = useMemo(() => {
    const map = new Map();
    if (!drag) return map;
    if (drag.kind === "move") {
      for (const id of drag.ids) {
        const found = clipIndex.get(id);
        if (!found) continue;
        map.set(id, { start: found.c.start + drag.dMs, dur: clipDur(found.c), trackId: id === drag.primary && drag.trackId ? drag.trackId : found.tr.id, dragging: true });
      }
    } else if (drag.kind === "slip") {
      const found = clipIndex.get(drag.id);
      if (found) map.set(drag.id, { start: found.c.start, dur: clipDur(found.c), trackId: found.tr.id, dragging: true, patch: drag.patch });
    } else if (drag.kind === "roll") {
      const l = clipIndex.get(drag.left);
      const r = clipIndex.get(drag.right);
      if (l && r) {
        map.set(l.c.id, { start: l.c.start, dur: clipDur(l.c) + drag.d, trackId: l.tr.id, dragging: true, patch: { src_out: Math.round(l.c.src_out + drag.d * drag.ls) } });
        map.set(r.c.id, { start: r.c.start + drag.d, dur: clipDur(r.c) - drag.d, trackId: r.tr.id, dragging: true, patch: { src_in: Math.round(r.c.src_in + drag.d * drag.rs) } });
      }
    } else {
      const tr = doc.tracks.find((x) => x.id === drag.trackId);
      const c = tr?.clips.find((x) => x.id === drag.id);
      if (c) {
        map.set(c.id, { start: drag.calc.start, dur: drag.calc.dur, trackId: tr.id, dragging: true, trimSide: drag.side });
        if (drag.ripple && drag.side === "r") {
          const end = clipEnd(c);
          for (const o of tr.clips) if (o.id !== c.id && o.start >= end - 1) map.set(o.id, { start: o.start + drag.calc.d, dur: clipDur(o), trackId: tr.id });
        }
      }
    }
    return map;
  }, [drag, doc, clipIndex]);

  const byTrack = useMemo(() => {
    const map = new Map(doc.tracks.map((tr) => [tr.id, []]));
    for (const tr of doc.tracks) for (const c of tr.clips) {
      const g = ghosts.get(c.id);
      (map.get(g?.trackId || tr.id) || map.get(tr.id)).push({ clip: c, track: tr, g });
    }
    return map;
  }, [doc, ghosts]);

  // ruler ticks
  const step = rulerStep(ppm, 96);
  const minor = step >= 1000 ? step / 5 : step / 2;
  const firstTick = Math.max(0, Math.floor(((viewport.left - HDR_W) / ppm - step) / step) * step);
  const lastTick = ((viewport.left + viewport.w - HDR_W) / ppm) + step;
  const labels = [];
  for (let ms = firstTick; ms <= lastTick; ms += step) labels.push(ms);

  const selSet = useMemo(() => new Set(selection.ids), [selection.ids]);
  const totalH = rowTop.__end;
  const nameOf = (c, media) => c.label || media?.name || "";

  return (
    <div style={{ display: "flex", flexDirection: "column", flex: 1, minHeight: 0 }}>
      <div className="tl-head">
        <button type="button" className="btn btn-sm" onClick={() => actions.split()} title={`${t("split")} (S)`}><Icon name="scissors" size={14} />{t("split")}</button>
        <button type="button" className="btn btn-sm" disabled={!selection.ids.length} onClick={() => actions.remove(true)} title={`${t("delete")} (Supr)`}><Icon name="trash" size={14} />{t("delete")}</button>
        <button type="button" className="btn btn-sm" disabled={!selection.ids.length} onClick={() => actions.duplicate()} title="Ctrl+D"><Icon name="copy" size={14} />{t("duplicate")}</button>
        <button type="button" className="btn btn-sm" onClick={() => actions.closeGaps()} title={t("close_gaps_help")}><Icon name="closegap" size={14} />{t("close_gaps")}</button>
        <button type="button" className="btn btn-sm" onClick={() => actions.addMarker()} title={`${t("marker_add")} (M)`}><Icon name="marker" size={14} />{t("marker")}</button>
        <span style={{ width: 1, height: 18, background: "var(--line-2)", margin: "0 4px" }} />
        <button type="button" className={`btn btn-sm ${snapOn ? "btn-on" : ""}`} aria-pressed={snapOn} onClick={() => setSnapOn((v) => !v)} title={t("snap_help")}><Icon name="magnet" size={14} />{t("snap")}</button>
        <button type="button" className="btn btn-sm" onClick={() => setMarks((m) => ({ ...m, in: Math.round(pb.t) }))} title={`${t("mark_in")} (I)`}><Icon name="inout" size={14} />I</button>
        <button type="button" className="btn btn-sm" onClick={() => setMarks((m) => ({ ...m, out: Math.round(pb.t) }))} title={`${t("mark_out")} (O)`}><Icon name="inout" size={14} style={{ transform: "scaleX(-1)" }} />O</button>
        {marks.in !== null || marks.out !== null ? <button type="button" className="btn btn-sm btn-ghost" onClick={() => setMarks({ in: null, out: null })} title={t("marks_clear")}><Icon name="x" size={12} />{t("marks_clear")}</button> : null}
        <div style={{ flex: 1 }} />
        <button type="button" className="btn btn-sm btn-ghost btn-icon" onClick={() => zoomTo(pps / 1.4)} title={`${t("zoom_out")} (-)`} aria-label={t("zoom_out")}><Icon name="zoomout" size={15} /></button>
        <input type="range" aria-label="Zoom" style={{ width: 120 }} min={Math.log(MIN_PPS)} max={Math.log(MAX_PPS)} step={0.01} value={Math.log(pps)} onChange={(e) => zoomTo(Math.exp(parseFloat(e.target.value)))} />
        <button type="button" className="btn btn-sm btn-ghost btn-icon" onClick={() => zoomTo(pps * 1.4)} title={`${t("zoom_in")} (+)`} aria-label={t("zoom_in")}><Icon name="zoomin" size={15} /></button>
        <button type="button" className="btn btn-sm" onClick={fit} title={t("fit_help")}><Icon name="fit" size={14} />{t("fit")}</button>
      </div>
      <div className="tl-scroll" ref={scrollRef} data-testid="timeline">
        <div className="tl-content" ref={contentRef} style={{ width: HDR_W + laneW, height: totalH + 40, minWidth: "100%" }}>
          {/* ruler + markers */}
          <div style={{ position: "sticky", top: 0, zIndex: 12, height: TOP_H, display: "flex" }}>
            <div className="tl-corner" style={{ width: HDR_W, height: TOP_H, display: "flex", alignItems: "center", gap: 4, padding: "0 6px" }}>
              <span className="muted" style={{ fontSize: 11 }}>{t("add_track")}</span>
              <button type="button" className="btn btn-sm btn-ghost btn-icon" title={t("track_video")} aria-label={t("track_video")} onClick={() => edit([{ op: "track_add", kind: "video" }], t("lbl_track_add"))}><Icon name="film" size={14} /></button>
              <button type="button" className="btn btn-sm btn-ghost btn-icon" title={t("track_audio")} aria-label={t("track_audio")} onClick={() => edit([{ op: "track_add", kind: "audio" }], t("lbl_track_add"))}><Icon name="music" size={14} /></button>
              <button type="button" className="btn btn-sm btn-ghost btn-icon" title={t("track_text")} aria-label={t("track_text")} onClick={() => edit([{ op: "track_add", kind: "text" }], t("lbl_track_add"))}><Icon name="type" size={14} /></button>
            </div>
            <div style={{ position: "relative", width: laneW, flex: "none" }}>
              <div
                className="tl-ruler"
                data-testid="ruler"
                onPointerDown={onRulerDown}
                style={{ position: "relative", width: laneW, height: RULER_H, backgroundImage: `repeating-linear-gradient(90deg, #ffffff22 0 1px, transparent 1px ${step * ppm}px), repeating-linear-gradient(90deg, #ffffff12 0 1px, transparent 1px ${minor * ppm}px)`, backgroundSize: `${step * ppm}px 12px, ${minor * ppm}px 6px`, backgroundRepeat: "repeat-x", backgroundPosition: "0 100%" }}
              >
                {marks.in !== null || marks.out !== null ? (
                  <div className="tl-range" style={{ left: (marks.in ?? 0) * ppm, width: Math.max(2, ((marks.out ?? contentEnd) - (marks.in ?? 0)) * ppm) }} />
                ) : null}
                {labels.map((ms) => (
                  <span key={ms} className="num" style={{ position: "absolute", left: ms * ppm + 4, top: 3, fontSize: 10.5, color: "var(--muted)", pointerEvents: "none" }}>{fmtRuler(ms, step)}</span>
                ))}
              </div>
              <div style={{ position: "relative", height: MARK_H, width: laneW, background: "var(--sunken)", borderBottom: "1px solid var(--line)" }}>
                {doc.markers.map((m) => (
                  <div
                    key={m.id}
                    className="tl-marker"
                    style={{ left: m.t * ppm }}
                    title={`${m.label || t(`marker_kind_${m.kind}`)} · ${t("marker_hint")}`}
                    onClick={() => pb.seek(m.t)}
                    onContextMenu={(e) => { e.preventDefault(); edit([{ op: "marker_delete", id: m.id }], t("lbl_marker_del")); }}
                  >
                    <i style={{ background: m.color || MARKER_COLORS[m.kind] }} />
                    {m.label ? <span>{m.label}</span> : null}
                  </div>
                ))}
              </div>
              <div ref={headRef} className="tl-playhead" style={{ left: 0, height: TOP_H, zIndex: 13 }}><i /></div>
            </div>
          </div>

          {/* tracks */}
          {rows.map((track) => {
            const h = ROW_H[track.kind];
            const items = byTrack.get(track.id) || [];
            const isMain = track.kind === "video" && track.role === "main";
            return (
              <div className="tl-row" key={track.id} style={{ height: h }} data-track={track.id}>
                <div className="tl-hdr" style={{ width: HDR_W, height: h }} aria-selected={selection.track === track.id} onClick={() => setSelection({ ids: [], track: track.id })}>
                  <Icon name={track.kind === "video" ? "film" : track.kind === "audio" ? "music" : "type"} size={14} style={{ color: "var(--muted)" }} />
                  {editingTrack === track.id ? (
                    <input className="field" style={{ height: 22, flex: 1, minWidth: 0 }} autoFocus defaultValue={track.name} onClick={(e) => e.stopPropagation()} onBlur={(e) => { setEditingTrack(null); if (e.target.value !== track.name) edit([{ op: "track_set", track: track.id, props: { name: e.target.value } }], t("lbl_track")); }} onKeyDown={(e) => { if (e.key === "Enter") e.target.blur(); if (e.key === "Escape") setEditingTrack(null); }} />
                  ) : (
                    <span className="ellipsis" style={{ flex: 1, minWidth: 0, fontSize: 12 }} onDoubleClick={() => setEditingTrack(track.id)} title={`${track.name} (${t("rename_hint")})`}>{track.name || t(`track_${track.kind}`)}{isMain ? <span className="dim"> ·</span> : null}</span>
                  )}
                  {track.kind !== "text" ? <button type="button" className={`btn btn-ghost btn-icon btn-sm ${track.muted ? "btn-on" : ""}`} style={{ width: 22, height: 22 }} aria-pressed={track.muted} title={track.muted ? t("unmute") : t("mute")} aria-label={t("mute")} onClick={(e) => { e.stopPropagation(); edit([{ op: "track_set", track: track.id, props: { muted: !track.muted } }], t("lbl_track")); }}><Icon name={track.muted ? "mute" : "volume"} size={13} /></button> : null}
                  {track.kind !== "audio" ? <button type="button" className={`btn btn-ghost btn-icon btn-sm ${track.hidden ? "btn-on" : ""}`} style={{ width: 22, height: 22 }} aria-pressed={track.hidden} title={track.hidden ? t("show") : t("hide")} aria-label={t("hide")} onClick={(e) => { e.stopPropagation(); edit([{ op: "track_set", track: track.id, props: { hidden: !track.hidden } }], t("lbl_track")); }}><Icon name={track.hidden ? "eyeoff" : "eye"} size={13} /></button> : null}
                  <button type="button" className={`btn btn-ghost btn-icon btn-sm ${track.locked ? "btn-on" : ""}`} style={{ width: 22, height: 22 }} aria-pressed={track.locked} title={track.locked ? t("unlock") : t("lock")} aria-label={t("lock")} onClick={(e) => { e.stopPropagation(); edit([{ op: "track_set", track: track.id, props: { locked: !track.locked } }], t("lbl_track")); }}><Icon name={track.locked ? "lock" : "unlock"} size={13} /></button>
                </div>
                <div
                  className={`tl-lane${track.locked ? " locked" : ""}${dropLane === track.id ? " drop" : ""}`}
                  data-lane={track.id}
                  ref={(el) => { if (el) laneEls.current.set(track.id, el); else laneEls.current.delete(track.id); }}
                  style={{ width: laneW, height: h, backgroundSize: `${Math.max(20, step * ppm)}px 100%` }}
                  onPointerDown={onLaneDown}
                  onDragOver={(e) => { if ([...e.dataTransfer.types].includes("application/x-lumiere-media")) { e.preventDefault(); setDropLane(track.id); } }}
                  onDragLeave={() => setDropLane((d) => (d === track.id ? null : d))}
                  onDrop={(e) => onLaneDrop(e, track)}
                >
                  {items.map(({ clip, track: own, g }) => {
                    const start = g ? g.start : clip.start;
                    const dur = g ? g.dur : clipDur(clip);
                    const x = start * ppm;
                    const w = dur * ppm;
                    if (x + w < viewport.left - 400 || x > viewport.left + viewport.w + 400) return null;
                    const [vl, vr] = visibleRange(x, w, viewport.left - HDR_W, viewport.w);
                    const media = clip.media ? view.media[clip.media] : null;
                    const ti = clip.transition_in;
                    const shown = g?.patch ? { ...clip, ...g.patch } : clip;
                    return (
                      <TimelineClip
                        key={clip.id}
                        clip={shown}
                        track={own}
                        media={media}
                        info={clip.media ? ed.mediaInfo[clip.media] : null}
                        x={x}
                        w={w}
                        h={h - 4}
                        ppm={ppm}
                        selected={selSet.has(clip.id)}
                        dragging={!!g?.dragging}
                        visL={vl}
                        visR={vr}
                        onBody={onClipDown}
                        onEdge={onEdgeDown}
                        label={nameOf(clip, media)}
                        hasTransition={!!ti}
                        transName={ti ? t(`tr_${ti.type}`) : ""}
                      />
                    );
                  })}
                </div>
              </div>
            );
          })}
          {rows.length === 0 ? <div className="muted" style={{ position: "absolute", top: TOP_H + 20, left: HDR_W + 20 }}>{t("timeline_empty")}</div> : null}
          <div ref={playheadRef} className="tl-playhead" style={{ left: 0 }} data-testid="playhead" />
          {guide !== null ? <div className="tl-guide" style={{ left: HDR_W + guide * ppm }} /> : null}
          {tip ? <div className="tl-tip" style={{ left: tip.x + 14, top: tip.y - 30 }}>{tip.text}</div> : null}
          {rubber ? <div className="tl-rubber" style={{ left: rubber.x, top: rubber.y, width: rubber.w, height: rubber.h }} /> : null}
        </div>
      </div>
    </div>
  );
}
