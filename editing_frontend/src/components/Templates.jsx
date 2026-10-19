import React, { useCallback, useEffect, useState } from "react";
import { api } from "../api.js";
import { go, useApp } from "../App.jsx";
import { ConfirmButton, Field, Icon, Modal, Spinner } from "./ui.jsx";
import { fmtMs } from "../editor/time.js";

const short = (ms) => fmtMs(ms).replace(/\.\d+$/, "");

// Save a project as a template: name the clips that change from one project to the next (the "slots").
export function SaveTemplateDialog({ project, onClose, onSaved }) {
  const { t, fail, notify } = useApp();
  const [view, setView] = useState(null);
  const [name, setName] = useState(`${project.name}`);
  const [slots, setSlots] = useState({});
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    api.project(project.id).then((v) => {
      setView(v);
      const found = {};
      for (const tr of v.doc.tracks) for (const c of tr.clips) if (c.slot) found[c.id] = c.slot;
      setSlots(found);
    }).catch(fail);
  }, [project.id, fail]);
  const clips = view ? view.doc.tracks.flatMap((tr) => tr.clips.filter((c) => c.type === "media").map((c) => ({ ...c, role: tr.role }))) : [];
  const named = Object.entries(slots).filter(([, v]) => v && v.trim());
  const save = async () => {
    setBusy(true);
    try {
      await api.templateSave(project.id, { name: name.trim() || project.name, slots: Object.fromEntries(named.map(([k, v]) => [k, v.trim()])) });
      notify(t("template_saved"), "ok");
      onSaved?.();
      onClose();
    } catch (e) { fail(e); setBusy(false); }
  };
  return (
    <Modal title={t("template_save")} onClose={onClose} width={640}
      footer={(
        <>
          <button type="button" className="btn" onClick={onClose}>{t("cancel")}</button>
          <button type="button" className="btn btn-primary" disabled={busy || !named.length} title={!named.length ? t("template_slot_none") : undefined} onClick={save} data-testid="template-save-go">{busy ? <Spinner /> : null}{t("save")}</button>
        </>
      )}>
      <Field label={t("template_name")}><input className="field" value={name} onChange={(e) => setName(e.target.value)} data-testid="template-name" /></Field>
      <div className="muted" style={{ fontSize: 12, marginBottom: 8 }}>{t("template_save_help")}</div>
      <span className="label">{t("template_clips")}</span>
      {!view ? <Spinner /> : clips.map((c) => (
        <div key={c.id} style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 6 }}>
          <div style={{ flex: 1, minWidth: 0 }}>
            <div className="ellipsis" style={{ fontSize: 12.5, fontWeight: 600 }}>{view.media[c.media]?.name || c.media}</div>
            <div className="muted num" style={{ fontSize: 11 }}>{short(c.start)}–{short(c.end)} · {c.role}</div>
          </div>
          <input className="field" style={{ width: 150 }} placeholder={t("template_slot")} value={slots[c.id] || ""} onChange={(e) => setSlots({ ...slots, [c.id]: e.target.value })} data-testid="template-slot-input" />
        </div>
      ))}
    </Modal>
  );
}

// Fill the slots of a template with library media and open the new project.
function UseTemplateDialog({ template, media, onClose }) {
  const { t, fail, notify } = useApp();
  const [name, setName] = useState("");
  const [picked, setPicked] = useState({});
  const [busy, setBusy] = useState(false);
  const fillable = template.slots.filter((s) => s.type === "media");
  const create = async () => {
    setBusy(true);
    try {
      const slots = Object.fromEntries(Object.entries(picked).filter(([, v]) => v));
      const r = await api.templateCreate(template.id, { name: name.trim() || template.name, slots });
      notify(r.unfilled?.length ? t("template_unfilled", { s: r.unfilled.join(", ") }) : t("template_created"), "ok");
      go(`p/${r.id}`);
    } catch (e) { fail(e); setBusy(false); }
  };
  return (
    <Modal title={t("template_new_project", { name: template.name })} onClose={onClose} width={640}
      footer={(
        <>
          <button type="button" className="btn" onClick={onClose}>{t("cancel")}</button>
          <button type="button" className="btn btn-primary" disabled={busy} onClick={create} data-testid="template-create-go">{busy ? <Spinner /> : null}{t("create")}</button>
        </>
      )}>
      <Field label={t("name")}><input className="field" value={name} placeholder={template.name} onChange={(e) => setName(e.target.value)} /></Field>
      {fillable.map((s) => (
        <div key={s.clip} style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 8 }}>
          <div style={{ width: 150 }}>
            <div style={{ fontWeight: 600, fontSize: 12.5 }}>{s.slot}</div>
            <div className="muted" style={{ fontSize: 11 }}>{t(`template_rule_${s.rule}`)} · {short(s.length_ms)}</div>
          </div>
          <select className="field" style={{ flex: 1 }} value={picked[s.slot] || ""} onChange={(e) => setPicked({ ...picked, [s.slot]: e.target.value })} data-testid="template-slot-media">
            <option value="">{t("template_keep")} ({s.name})</option>
            {(media || []).filter((m) => m.kind === s.track_kind || m.kind === "image").map((m) => <option key={m.id} value={m.id}>{m.name}</option>)}
          </select>
        </div>
      ))}
    </Modal>
  );
}

export function TemplatesSection({ media, refreshKey }) {
  const { t, fail } = useApp();
  const [list, setList] = useState(null);
  const [using, setUsing] = useState(null);
  const load = useCallback(async () => {
    try { setList((await api.templates()).templates); } catch (e) { fail(e); }
  }, [fail]);
  useEffect(() => { load(); }, [load, refreshKey]);
  return (
    <>
      <div className="section-title"><h2>{t("templates_title")} {list ? <span className="muted" style={{ fontWeight: 400 }}>({list.length})</span> : null}</h2></div>
      {!list ? <Spinner /> : list.length === 0 ? (
        <div className="panel muted" style={{ padding: 18, textAlign: "center", fontSize: 12.5 }}>{t("templates_empty")}</div>
      ) : (
        <div className="grid-cards" data-testid="templates">
          {list.map((tp) => (
            <div key={tp.id} className="pcard" style={{ cursor: "default" }} data-testid="template-card">
              <div style={{ padding: "11px 12px" }}>
                <div className="ellipsis" style={{ fontWeight: 600, fontSize: 13.5 }} title={tp.name}><Icon name="clip" size={14} style={{ color: "var(--accent)", marginRight: 6 }} />{tp.name}</div>
                <div className="muted num" style={{ fontSize: 11.5, margin: "3px 0 6px" }}>{tp.canvas} · {tp.duration_ms ? short(tp.duration_ms) : "0:00"}</div>
                <div style={{ display: "flex", flexWrap: "wrap", gap: 4, marginBottom: 8 }}>
                  {tp.slots.map((s) => <span key={s.clip} className="chip" title={s.name}>{s.slot}</span>)}
                </div>
                <div style={{ display: "flex", gap: 6 }}>
                  <button type="button" className="btn btn-sm btn-primary" onClick={() => setUsing(tp)} data-testid="template-use">{t("template_use")}</button>
                  <ConfirmButton label={t("delete")} confirmLabel={t("confirm_delete")} cancelLabel={t("cancel")} onConfirm={async () => { try { await api.projectDelete(tp.id); load(); } catch (e) { fail(e); } }} />
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
      {using ? <UseTemplateDialog template={using} media={media} onClose={() => setUsing(null)} /> : null}
    </>
  );
}
