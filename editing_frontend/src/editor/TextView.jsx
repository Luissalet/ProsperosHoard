import React, { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { api } from "../api.js";
import { useApp } from "../App.jsx";
import { Icon, Spinner } from "../components/ui.jsx";
import { useEd } from "./EditorContext.js";
import { fmtMs } from "./time.js";

// One paragraph of the transcript. Memoised so a selection change only re-renders the paragraphs it touches.
const Paragraph = React.memo(function Paragraph({ p, words, selA, selB, editing, onSeek, onWordDown, onWordEnter, onEdit, onFix, setSpan, onSpeaker }) {
  const first = words[p.items[0]];
  return (
    <p style={{ margin: "0 0 10px" }} data-speaker={first.speaker_name || undefined}>
      <button type="button" className="btn btn-ghost btn-sm num" style={{ height: 20, padding: "0 6px", marginRight: 6, fontSize: 10.5 }} onClick={() => onSeek(p.start)}>{fmtMs(p.start).replace(/\.\d+$/, "")}</button>
      {first.speaker ? (
        <button type="button" className="chip" data-testid="speaker-tag" onClick={() => onSpeaker(first)} title={first.speaker_name}
          style={{ marginRight: 8, cursor: "pointer", border: `1px solid ${first.speaker_color || "var(--line-2)"}`, color: first.speaker_color || "inherit", fontWeight: 700, height: 20 }}>
          <span style={{ width: 8, height: 8, borderRadius: 4, background: first.speaker_color || "var(--muted)", display: "inline-block" }} />{first.speaker_name}
        </button>
      ) : null}
      {p.items.map((i) => {
        const w = words[i];
        const isSel = i >= selA && i <= selB;
        if (editing === i) {
          return <input key={w.id + i} autoFocus className="field" style={{ width: Math.max(50, w.text.length * 11), height: 24, display: "inline-block", fontSize: 14 }} defaultValue={w.text} onBlur={(e) => onFix(i, e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") e.target.blur(); if (e.key === "Escape") onEdit(null); e.stopPropagation(); }} />;
        }
        return (
          <span
            key={w.id + i}
            ref={(el) => setSpan(i, el)}
            className={`word${isSel ? " sel" : ""}${w.p !== null && w.p !== undefined && w.p < 0.5 ? " low" : ""}`}
            data-word={w.id}
            style={w.speaker_color ? { color: w.speaker_color } : undefined}
            onPointerDown={(e) => onWordDown(e, i)}
            onPointerEnter={() => onWordEnter(i)}
            onDoubleClick={() => onEdit(i)}
          >
            {w.text}{" "}
          </span>
        );
      })}
    </p>
  );
});

// Text-based editing: the transcript of the timeline as running text. Select words and cut them (or keep only them).
export default function TextView() {
  const { t, notify, fail, jobs } = useApp();
  const ed = useEd();
  const { transcript, projectId } = ed;
  const words = transcript?.words || [];
  const [sel, setSel] = useState(null); // {a, b} inclusive word indexes
  const [editing, setEditing] = useState(null);
  const [busy, setBusy] = useState(false);
  const anchor = useRef(null);
  const dragging = useRef(false);
  const spans = useRef([]);
  const activeIdx = useRef(-1);
  const box = useRef(null);
  const wordsRef = useRef(words);
  wordsRef.current = words;

  const range = sel ? [Math.min(sel.a, sel.b), Math.max(sel.a, sel.b)] : null;

  // highlight the word being played
  useEffect(() => {
    const find = (time) => {
      const w = wordsRef.current;
      let lo = 0;
      let hi = w.length - 1;
      let idx = -1;
      while (lo <= hi) {
        const mid = (lo + hi) >> 1;
        if (w[mid].t <= time) { idx = mid; lo = mid + 1; } else hi = mid - 1;
      }
      if (idx >= 0 && time > w[idx].t1 + 400) return -1;
      return idx;
    };
    const onTick = (time, playing) => {
      const idx = find(time);
      if (idx === activeIdx.current) return;
      spans.current[activeIdx.current]?.classList.remove("now");
      activeIdx.current = idx;
      const el = spans.current[idx];
      if (el) {
        el.classList.add("now");
        if (playing && box.current) {
          const r = el.getBoundingClientRect();
          const b = box.current.getBoundingClientRect();
          if (r.top < b.top + 30 || r.bottom > b.bottom - 30) el.scrollIntoView({ block: "center", behavior: "smooth" });
        }
      }
    };
    onTick(ed.pb.t, false);
    return ed.pb.subscribe(onTick);
  }, [ed.pb, words]);

  useEffect(() => {
    const up = () => { dragging.current = false; };
    window.addEventListener("pointerup", up);
    return () => window.removeEventListener("pointerup", up);
  }, []);

  const doCut = useCallback(async (keep) => {
    if (!range) return false;
    const picked = words.slice(range[0], range[1] + 1);
    const byMedia = new Map();
    for (const w of picked) {
      if (!byMedia.has(w.media)) byMedia.set(w.media, []);
      byMedia.get(w.media).push(w.id);
    }
    setBusy(true);
    try {
      let last = null;
      for (const [media, ids] of byMedia) {
        last = await ed.withAnalysis(() => api.textCut(projectId, { media, word_ids: ids, keep }));
      }
      if (last?.view) ed.applyView(last.view);
      setSel(null);
      await ed.reloadTranscript();
      notify(t(keep ? "text_kept" : "text_cut", { n: picked.length }), "ok");
    } catch (e) {
      fail(e);
    } finally {
      setBusy(false);
    }
    return true;
  }, [range, words, ed, projectId, notify, fail, t]);

  useEffect(() => {
    ed.textApi.current = { hasSelection: () => !!range, cut: () => doCut(false) };
    return () => { ed.textApi.current = null; };
  }, [ed.textApi, range, doCut]);

  const transcribe = async () => {
    setBusy(true);
    try {
      const ids = [];
      for (const mid of transcript.missing) {
        const res = await api.mediaAnalyze(mid, ["transcript"]);
        for (const j of res.jobs) if (j.job) ids.push(j.job);
      }
      jobs.poke();
      notify(t("analysis_wait"));
      await Promise.all(ids.map((id) => jobs.watch(id)));
      await ed.reloadTranscript();
    } catch (e) {
      fail(e);
    } finally {
      setBusy(false);
    }
  };


  // ---- speakers: who says what (colour per speaker, rename, reassign, keep / remove one person's parts)
  const speakerList = transcript?.speakers || [];
  const mediaIds = useMemo(() => [...new Set(words.map((w) => w.media))], [words]);
  const [numSpeakers, setNumSpeakers] = useState("");
  const [active, setActive] = useState(null); // {media, id, name, color}
  const [draftName, setDraftName] = useState("");
  const byName = useMemo(() => {
    const m = new Map();
    for (const s of speakerList) if (!m.has(s.name)) m.set(s.name, { ...s, medias: [s.media] }); else m.get(s.name).medias.push(s.media);
    return [...m.values()];
  }, [speakerList]);

  const diarize = async () => {
    setBusy(true);
    try {
      const ids = [];
      for (const mid of mediaIds) {
        const res = await api.mediaAnalyze(mid, ["speakers"], true, numSpeakers ? { num_speakers: Number(numSpeakers) } : {});
        for (const j of res.jobs) if (j.job) ids.push(j.job);
      }
      jobs.poke();
      notify(t("analysis_wait"));
      await Promise.all(ids.map((id) => jobs.watch(id)));
      await ed.reloadTranscript();
      const found = await api.mediaSpeakers(mediaIds[0]);
      notify(t("speakers_done", { n: (found.speakers || []).length }), "ok");
    } catch (e) {
      fail(e);
    } finally {
      setBusy(false);
    }
  };

  const openSpeaker = useCallback((w) => {
    setActive({ media: w.media, id: w.speaker, name: w.speaker_name, color: w.speaker_color });
    setDraftName(w.speaker_name);
  }, []);

  const renameSpeaker = async () => {
    const name = draftName.trim();
    if (!active || !name || name === active.name) return;
    setBusy(true);
    try {
      for (const mid of mediaIds) {
        const own = speakerList.find((s) => s.media === mid && s.name === active.name);
        if (own) await api.speakersEdit(mid, { action: "rename", names: { [own.id]: name } });
      }
      await ed.reloadTranscript();
      setActive({ ...active, name });
      notify(t("speakers_renamed"), "ok");
    } catch (e) { fail(e); } finally { setBusy(false); }
  };

  const assignTo = async (target) => {
    if (!range) return;
    let name = target;
    if (target === "__new") {
      name = (window.prompt(t("speakers_prompt_name")) || "").trim();
      if (!name) return;
    }
    const picked = words.slice(range[0], range[1] + 1);
    const byMedia = new Map();
    for (const w of picked) {
      if (!byMedia.has(w.media)) byMedia.set(w.media, []);
      byMedia.get(w.media).push(w.id);
    }
    setBusy(true);
    try {
      for (const [media, ids] of byMedia) await api.speakersEdit(media, { action: "assign", speaker: name, word_ids: ids });
      await ed.reloadTranscript();
      notify(t("speakers_assigned", { n: picked.length }), "ok");
    } catch (e) { fail(e); } finally { setBusy(false); }
  };

  const cutSpeaker = async (keep) => {
    if (!active) return;
    setBusy(true);
    try {
      await ed.runCommand("speaker_cut", { speakers: [active.name], keep });
      await ed.reloadTranscript();
      notify(t(keep ? "speakers_keep_done" : "speakers_cut_done", { name: active.name }), "ok");
      setActive(null);
    } catch (e) { fail(e); } finally { setBusy(false); }
  };

  const command = async (name) => {
    setBusy(true);
    try {
      const res = await ed.runCommand(name, {});
      notify(`${t(name === "remove_fillers" ? "tool_fillers" : "tool_silences")}: ${t("saved_time", { s: ((res.summary?.saved_ms ?? 0) / 1000).toFixed(1) })}`, "ok");
      await ed.reloadTranscript();
    } catch (e) {
      fail(e);
    } finally {
      setBusy(false);
    }
  };

  const live = useRef({});
  live.current = { ed, fail, words };
  const fixWord = useCallback(async (idx, text) => {
    setEditing(null);
    const { ed: e, fail: f, words: ws } = live.current;
    const w = ws[idx];
    if (!text.trim() || text.trim() === w.text) return;
    try {
      await api.mediaTranscriptFix(w.media, [{ id: w.id, text: text.trim() }]);
      await e.reloadTranscript();
    } catch (err) { f(err); }
  }, []);

  const paragraphs = useMemo(() => {
    const out = [];
    let cur = null;
    words.forEach((w, i) => {
      const prev = words[i - 1];
      if (!cur || w.clip !== prev?.clip || w.t - prev.t1 > 1200 || w.speaker !== prev?.speaker) { cur = { start: w.t, items: [] }; out.push(cur); }
      cur.items.push(i);
    });
    return out;
  }, [words]);

  const setSpan = useCallback((i, el) => { spans.current[i] = el; }, []);
  const onWordDown = useCallback((e, i) => {
    if (e.button !== 0) return;
    if (e.shiftKey && anchor.current !== null) setSel({ a: anchor.current, b: i });
    else { anchor.current = i; setSel({ a: i, b: i }); dragging.current = true; }
    ed.pb.seek(wordsRef.current[i].t);
  }, [ed.pb]);
  const onWordEnter = useCallback((i) => { if (dragging.current && anchor.current !== null) setSel({ a: anchor.current, b: i }); }, []);
  const onSeek = useCallback((t0) => ed.pb.seek(t0), [ed.pb]);

  const missing = transcript?.missing || [];

  return (
    <div style={{ display: "flex", flexDirection: "column", flex: 1, minHeight: 0 }}>
      <div className="tl-head">
        <button type="button" className="btn btn-sm btn-danger" disabled={!range || busy} onClick={() => doCut(false)} title="Supr"><Icon name="scissors" size={14} />{t("text_cut_sel")}</button>
        <button type="button" className="btn btn-sm" disabled={!range || busy} onClick={() => doCut(true)}><Icon name="check" size={14} />{t("text_keep_sel")}</button>
        <span style={{ width: 1, height: 18, background: "var(--line-2)", margin: "0 4px" }} />
        <button type="button" className="btn btn-sm" disabled={busy} onClick={() => command("remove_fillers")}><Icon name="mic" size={14} />{t("tool_fillers")}</button>
        <button type="button" className="btn btn-sm" disabled={busy} onClick={() => command("remove_silences")}><Icon name="waveform" size={14} />{t("tool_silences")}</button>
        {busy ? <Spinner /> : null}
        <div style={{ flex: 1 }} />
        <span className="muted" style={{ fontSize: 11.5 }}>{range ? t("text_selected", { n: range[1] - range[0] + 1 }) : t("text_hint")}</span>
      </div>
      {words.length ? (
        <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 8, padding: "6px 12px", borderBottom: "1px solid var(--line)", background: "var(--panel)" }} data-testid="speakers-bar">
          <Icon name="mic" size={14} style={{ color: "var(--muted)" }} />
          <b style={{ fontSize: 12 }}>{t("speakers_title")}</b>
          {byName.length ? byName.map((s) => (
            <button key={s.name} type="button" className="chip" data-testid="speaker-chip" onClick={() => { setActive({ media: s.medias[0], id: s.id, name: s.name, color: s.color }); setDraftName(s.name); }}
              style={{ cursor: "pointer", border: `1px solid ${s.color || "var(--line-2)"}`, color: s.color, fontWeight: 700, height: 22, background: active?.name === s.name ? "var(--panel-3)" : undefined }}>
              <span style={{ width: 8, height: 8, borderRadius: 4, background: s.color, display: "inline-block" }} />{s.name}
            </button>
          )) : <span className="muted" style={{ fontSize: 11.5 }}>{t("speakers_none")}</span>}
          <div style={{ flex: 1 }} />
          {range && byName.length ? (
            <select className="field" style={{ width: 150, height: 26 }} value="" onChange={(e) => e.target.value && assignTo(e.target.value)} data-testid="speaker-assign" aria-label={t("speakers_assign")}>
              <option value="">{t("speakers_assign")}</option>
              {byName.map((s) => <option key={s.name} value={s.name}>{s.name}</option>)}
              <option value="__new">{t("speakers_new")}</option>
            </select>
          ) : null}
          <select className="field" style={{ width: 92, height: 26 }} value={numSpeakers} onChange={(e) => setNumSpeakers(e.target.value)} aria-label={t("speakers_n")} data-testid="speaker-count" title={t("speakers_n")}>
            <option value="">{t("speakers_auto")}</option>
            {[1, 2, 3, 4, 5, 6].map((n) => <option key={n} value={n}>{n}</option>)}
          </select>
          <button type="button" className="btn btn-sm" disabled={busy || missing.length > 0} onClick={diarize} data-testid="speaker-split" title={t("speakers_help")}><Icon name="mic" size={14} />{t("speakers_split")}</button>
        </div>
      ) : null}
      {active ? (
        <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 8, padding: "6px 12px", borderBottom: "1px solid var(--line)", background: "var(--panel-2)" }} data-testid="speaker-editor">
          <span style={{ width: 10, height: 10, borderRadius: 5, background: active.color, display: "inline-block" }} />
          <input className="field" style={{ width: 150, height: 26 }} value={draftName} onChange={(e) => setDraftName(e.target.value)} onKeyDown={(e) => { if (e.key === "Enter") renameSpeaker(); if (e.key === "Escape") setActive(null); e.stopPropagation(); }} aria-label={t("speakers_rename")} data-testid="speaker-name" />
          <button type="button" className="btn btn-sm" disabled={busy || !draftName.trim() || draftName.trim() === active.name} onClick={renameSpeaker} data-testid="speaker-rename">{t("speakers_rename")}</button>
          <button type="button" className="btn btn-sm" disabled={busy} onClick={() => cutSpeaker(true)} data-testid="speaker-keep"><Icon name="check" size={14} />{t("speakers_keep")} {active.name}</button>
          <button type="button" className="btn btn-sm btn-danger" disabled={busy} onClick={() => cutSpeaker(false)} data-testid="speaker-cut"><Icon name="scissors" size={14} />{t("speakers_cut")} {active.name}</button>
          <div style={{ flex: 1 }} />
          <button type="button" className="btn btn-sm btn-ghost btn-icon" onClick={() => setActive(null)} aria-label={t("close")}><Icon name="x" size={14} /></button>
        </div>
      ) : null}
      {missing.length ? (
        <div style={{ padding: "8px 12px", background: "color-mix(in srgb, var(--warn) 16%, var(--panel))", borderBottom: "1px solid color-mix(in srgb, var(--warn) 45%, var(--panel))", display: "flex", gap: 10, alignItems: "center" }}>
          <span style={{ flex: 1, fontSize: 12.5 }}>{t("text_missing", { n: missing.length })}</span>
          <button type="button" className="btn btn-sm btn-primary" disabled={busy} onClick={transcribe}><Icon name="mic" size={14} />{t("transcribe")}</button>
        </div>
      ) : null}
      <div ref={box} style={{ flex: 1, minHeight: 0, overflow: "auto", padding: "10px 18px 24px", fontSize: 15, lineHeight: 1.95, background: "var(--sunken)", userSelect: "none" }} data-testid="textview">
        {!transcript ? <Spinner /> : words.length === 0 ? <div className="muted" style={{ padding: 20 }}>{missing.length ? t("text_none_missing") : t("text_none")}</div> : paragraphs.map((p, pi) => {
          const first = p.items[0];
          const last = p.items[p.items.length - 1];
          const hit = range && range[1] >= first && range[0] <= last;
          return (
            <Paragraph
              key={pi}
              p={p}
              words={words}
              selA={hit ? range[0] : 1}
              selB={hit ? range[1] : 0}
              editing={editing !== null && editing >= first && editing <= last ? editing : -1}
              onSeek={onSeek}
              onWordDown={onWordDown}
              onWordEnter={onWordEnter}
              onEdit={setEditing}
              onFix={fixWord}
              setSpan={setSpan}
              onSpeaker={openSpeaker}
            />
          );
        })}
      </div>
    </div>
  );
}
