import React, { useEffect, useRef, useState } from "react";
import { api } from "../api.js";
import { useApp } from "../App.jsx";
import { Field, Icon, Spinner, Toggle } from "../components/ui.jsx";
import { setGlPreview, useGlPreview, webglAvailable } from "../editor/previewPrefs.js";
import { useCopilotForm, useCopilotRefresh } from "../copilot-hooks.js";
import { changedFields } from "../copilot-core.js";
import { mergeSettingsDraft, settingsFields } from "../settings-draft.js";


function Stat({ label, value, tone }) {
  return (
    <div style={{ display: "flex", justifyContent: "space-between", gap: 12, padding: "5px 0", borderBottom: "1px solid #ffffff0a" }}>
      <span className="muted">{label}</span>
      <span className={`chip ${tone ? `chip-${tone}` : ""}`} style={{ maxWidth: "70%" }}><span className="ellipsis">{String(value)}</span></span>
    </div>
  );
}

export default function Settings() {
  const { t, fail, notify, presets } = useApp();
  const [settings, setSettings] = useState(null);
  const [draft, setDraft] = useState({});
  const [status, setStatus] = useState(null);
  const [saving, setSaving] = useState(false);
  const state = useRef({ settings, draft });
  state.current = { settings, draft };
  const readGeneration = useRef(0);
  const glOn = useGlPreview();
  const glAvailable = webglAvailable();

  const reload = async () => {
    const generation = ++readGeneration.current;
    try {
      const next = await api.settings();
      if (generation !== readGeneration.current) return;
      const latest = state.current;
      const merged = mergeSettingsDraft(settingsFields(latest.settings), latest.draft, next);
      state.current = { settings: next, draft: merged };
      setSettings(next);
      setDraft(merged);
    } catch (e) { fail(e); }
  };

  useEffect(() => {
    reload();
    api.status().then(setStatus).catch(fail);
    return () => { ++readGeneration.current; };
  }, [fail]);
  useCopilotRefresh(reload);
  useCopilotForm("settings-form", { initial: settingsFields(settings), current: draft });

  const set = (k, v) => {
    const next = { ...state.current.draft, [k]: v };
    state.current = { ...state.current, draft: next };
    setDraft(next);
  };
  const dirty = settings && Object.keys(changedFields(settingsFields(settings), draft)).length > 0;

  const save = async () => {
    const submitted = { ...state.current.draft };
    const patch = changedFields(settingsFields(state.current.settings), submitted);
    ++readGeneration.current;
    setSaving(true);
    try {
      const next = await api.settingsUpdate(patch);
      ++readGeneration.current;
      const merged = mergeSettingsDraft(submitted, state.current.draft, next);
      state.current = { settings: next, draft: merged };
      setSettings(next);
      setDraft(merged);
      notify(t("settings_saved"), "ok");
      api.status().then(setStatus).catch(() => {});
    } catch (e) {
      fail(e);
    } finally {
      setSaving(false);
    }
  };

  if (!settings) return <div style={{ padding: 30 }}><Spinner /></div>;
  const ff = status?.ffmpeg || {};
  const llm = status?.model?.llm;
  const speech = status?.speech || {};
  const yn = (v) => (v ? t("yes") : t("no"));

  return (
    <div className="page">
      <div className="page-inner" style={{ maxWidth: 1020 }}>
        <div className="section-title" style={{ marginTop: 0 }}><h2>{t("nav_settings")}</h2></div>
        <div style={{ display: "grid", gridTemplateColumns: "minmax(0, 3fr) minmax(0, 2fr)", gap: 18, alignItems: "start" }}>
          <div className="panel" style={{ padding: 16 }}>
            <h3 style={{ fontSize: 13.5, marginBottom: 12 }}>{t("settings_speech")}</h3>
            <Field label={t("set_whisper_model")} help={t("set_whisper_model_help")}>
              <input className="field" value={draft.whisper_model} placeholder="large-v3-turbo" onChange={(e) => set("whisper_model", e.target.value)} />
            </Field>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
              <Field label={t("set_whisper_device")}>
                <select className="field" value={draft.whisper_device} onChange={(e) => set("whisper_device", e.target.value)}>
                  <option value="auto">auto</option><option value="cuda">cuda</option><option value="cpu">cpu</option>
                </select>
              </Field>
              <Field label={t("set_language")} help={t("set_language_help")}>
                <input className="field" value={draft.transcript_language} placeholder="es" onChange={(e) => set("transcript_language", e.target.value)} />
              </Field>
            </div>
            <Toggle checked={draft.auto_transcribe === "on"} onChange={(v) => set("auto_transcribe", v ? "on" : "off")} label={t("set_auto_transcribe")} />
            <div className="muted" style={{ fontSize: 11.5, margin: "2px 0 14px 22px" }}>{t("set_auto_transcribe_help")}</div>

            <h3 style={{ fontSize: 13.5, margin: "10px 0 12px" }}>{t("settings_render")}</h3>
            <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 12 }}>
              <Field label={t("set_hwdec")} help={t("set_hwdec_help")}>
                <select className="field" value={draft.hwdec} onChange={(e) => set("hwdec", e.target.value)}>
                  <option value="auto">auto</option><option value="on">{t("on")}</option><option value="off">{t("off")}</option>
                </select>
              </Field>
              <Field label={t("set_default_export")}>
                <select className="field" value={draft.default_export} onChange={(e) => set("default_export", e.target.value)}>
                  {(presets?.export_presets || []).map((p) => <option key={p.id} value={p.id}>{p.label}</option>)}
                </select>
              </Field>
            </div>
            <Field label={t("set_export_folder")} help={t("set_export_folder_help")}>
              <input className="field" value={draft.export_folder} placeholder={t("set_export_folder_ph")} onChange={(e) => set("export_folder", e.target.value)} />
            </Field>

            <Field label={t("set_face_detector")} help={t("set_face_detector_help")}>
              <select className="field" value={draft.face_detector || "auto"} onChange={(e) => set("face_detector", e.target.value)} data-testid="face-detector">
                {["auto", "yunet", "haar", "saliency"].map((d) => <option key={d} value={d}>{d}</option>)}
              </select>
            </Field>
            <Field label={t("set_notify_via")} help={t("set_notify_via_help")}>
              <select className="field" value={draft["notify.via"] || "auto"} onChange={(e) => set("notify.via", e.target.value)} data-testid="notify-via">
                <option value="auto">auto</option><option value="hub">{t("set_notify_hub")}</option><option value="off">{t("off")}</option>
              </select>
            </Field>
            <h3 style={{ fontSize: 13.5, margin: "10px 0 12px" }}>{t("gl_preview_settings")}</h3>
            <Toggle checked={glOn && glAvailable} onChange={setGlPreview} label={t("gl_preview")} />
            <div className="muted" style={{ fontSize: 11.5, margin: "2px 0 4px 22px" }} data-testid="gl-setting-help">{t("gl_preview_help")}</div>
            <div className="muted" style={{ fontSize: 11.5, margin: "0 0 14px 22px" }}>{glAvailable ? (glOn ? t("gl_preview_status_on") : t("gl_preview_status_off")) : t("gl_preview_status_missing")} · {t("gl_preview_setting_help")}</div>

            <h3 style={{ fontSize: 13.5, margin: "10px 0 12px" }}>{t("settings_model")}</h3>
            <Field label={t("set_model")} help={t("set_model_help")}>
              <input className="field" value={draft.model} placeholder="auto" onChange={(e) => set("model", e.target.value)} />
            </Field>
            <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 6 }}>
              <button type="button" className="btn btn-primary" disabled={!dirty || saving} onClick={save}>{saving ? <Spinner /> : <Icon name="check" size={14} />}{t("save")}</button>
            </div>
          </div>

          <div className="panel" style={{ padding: 16 }}>
            <h3 style={{ fontSize: 13.5, marginBottom: 10 }}>{t("status_title")}</h3>
            {!status ? <Spinner /> : (
              <>
                <Stat label="ffmpeg" value={ff.error ? ff.error : ff.version} tone={ff.error ? "err" : "ok"} />
                <Stat label={t("status_nvenc")} value={yn(ff.nvenc)} tone={ff.nvenc ? "ok" : undefined} />
                <Stat label={t("status_hwdec")} value={yn(ff.hwdec)} />
                <Stat label={t("status_libass")} value={yn(ff.libass)} tone={ff.libass ? "ok" : "warn"} />
                <Stat label={t("status_vidstab")} value={yn(ff.vidstab)} />
                <Stat label={t("status_speech")} value={speech.available ? `${speech.engine}${speech.cuda_devices ? ` · GPU ×${speech.cuda_devices}` : " · CPU"}` : t("status_unavailable")} tone={speech.available ? "ok" : "warn"} />
                <Stat label={t("status_faces")} value={status.faces ? `${status.faces.active}${status.faces.yunet?.downloaded ? "" : status.faces.preferred !== "saliency" ? ` (${t("status_faces_pending")})` : ""}` : "—"} tone={status.faces?.active === "yunet" ? "ok" : undefined} />
                <Stat label={t("status_model")} value={llm ? (llm.state === "ready" || llm.model ? `${llm.provider || ""} ${llm.model || ""}`.trim() : t("status_unavailable")) : "—"} tone={llm?.model ? "ok" : "warn"} />
                {llm && !llm.model && llm.reason ? <div className="muted" style={{ fontSize: 11.5, marginTop: 6 }}>{llm.reason}</div> : null}
                <Stat label={t("status_encoder")} value={settings.encoder} />
                <Stat label={t("status_data")} value={status.data_dir} />
                <Stat label={t("status_counts")} value={t("status_counts_v", status.counts)} />
                <Stat label={t("status_version")} value={status.version} />
              </>
            )}
          </div>
        </div>
      </div>
    </div>
  );
}
