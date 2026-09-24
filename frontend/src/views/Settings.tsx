import { useEffect, useState } from "react";
import { Save, Upload } from "lucide-react";
import { api, type BackendStatus, type WorkflowSpec } from "../api";
import { useT } from "../i18n";
import { useApp, useAsync } from "../components/ui";

// The configured folders, as saved. `import_roots` also holds the built-in ones
// (home, data/inbox) resolved and deduplicated, so slicing it by position can
// drop a user folder; servers that predate `import_roots_user` fall back to that.
function userRoots(s: BackendStatus): string[] {
  return s.overrides.import_roots_user ?? s.overrides.import_roots.slice(2);
}
function builtinRoots(s: BackendStatus): string[] {
  if (!s.overrides.import_roots_user) return s.overrides.import_roots.slice(0, 2);
  const mine = new Set(s.overrides.import_roots_user);
  return s.overrides.import_roots.filter((r) => !mine.has(r));
}

export function SettingsView() {
  const { t } = useT();
  const app = useApp();
  const status = useAsync(() => api.backend(), []);
  const workflows = useAsync(() => api.workflows(), [app.dataVersion]);
  const [faustusUrl, setFaustusUrl] = useState("");
  const [token, setToken] = useState("");
  const [comfyUrl, setComfyUrl] = useState("");
  const [roots, setRoots] = useState("");
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    const s = status.data;
    if (!s) return;
    setFaustusUrl(s.overrides.faustus_url || "");
    setComfyUrl(s.demo ? "" : s.overrides.comfy_url || "");
    setRoots(userRoots(s).join("\n"));
  }, [status.data]);

  const save = async () => {
    setSaving(true);
    try {
      const body: Record<string, unknown> = {
        faustus_url: faustusUrl, import_roots: roots.split("\n").map((x) => x.trim()).filter(Boolean),
      };
      if (!status.data?.demo) body.comfy_url = comfyUrl;
      if (token) body.faustus_token = token;
      await api.setBackend(body);
      setToken("");
      status.reload();
      app.toast(t("saved"), "ok");
    } catch (e) {
      app.toast((e as Error).message, "bad");
    } finally {
      setSaving(false);
    }
  };

  const importWorkflow = async (file: File | undefined) => {
    if (!file) return;
    try {
      const spec = await api.importWorkflow(file);
      app.toast(`${t("saved")}: ${spec.name}`, "ok");
      app.bump();
    } catch (e) {
      app.toast((e as Error).message, "bad");
    }
  };

  const s = status.data;
  return (
    <>
      <div className="page-head"><div><h1>{t("settingsTitle")}</h1></div></div>
      <div className="grid-2" style={{ alignItems: "start" }}>
        <div className="card stack">
          <label className="field">{t("faustusUrl")}
            <input value={faustusUrl} onChange={(e) => setFaustusUrl(e.target.value)} placeholder="http://127.0.0.1:8000" className="mono" /></label>
          <label className="field">{t("faustusToken")}
            <input type="password" value={token} onChange={(e) => setToken(e.target.value)} placeholder={s?.token_set ? "••••••••" : ""} autoComplete="off" />
            {s?.token_set && <span className="hint">{t("tokenSet")}</span>}</label>
          <label className="field">{t("comfyUrl")}
            <input value={comfyUrl} onChange={(e) => setComfyUrl(e.target.value)} placeholder="http://127.0.0.1:8188" className="mono" disabled={s?.demo} />
            {s?.demo && <span className="hint">{t("demoHint")}</span>}</label>
          <label className="field">{t("importRoots")}
            <textarea value={roots} onChange={(e) => setRoots(e.target.value)} rows={3} className="mono" placeholder="D:\\Music" />
            <span className="hint">{t("importRootsHint")}</span>
            {s && <span className="hint mono">{builtinRoots(s).join(" · ")}</span>}</label>
          <button className="btn primary" onClick={save} disabled={saving}><Save size={15} /> {t("save")}</button>
        </div>
        <div className="card stack">
          <h2>{t("customWorkflows")}</h2>
          <label className="btn" style={{ alignSelf: "flex-start" }}>
            <Upload size={15} /> {t("importWorkflow")}
            <input type="file" accept=".json" hidden onChange={(e) => importWorkflow(e.target.files?.[0])} />
          </label>
          {(workflows.data?.custom || []).map((w) => <WorkflowCard key={w.template} spec={w} onSaved={() => app.bump()} />)}
          <div className="panel-title">{t("template")}</div>
          {(workflows.data?.builtin || []).map((w) => (
            <div key={w.template} className="row small"><span className="mono">{w.template}</span><span className="pill">{w.kind}</span><span className="pill">{w.vram_class}</span></div>
          ))}
        </div>
      </div>
    </>
  );
}

function WorkflowCard({ spec, onSaved }: { spec: WorkflowSpec; onSaved: () => void }) {
  const { t } = useT();
  const app = useApp();
  const [map, setMap] = useState<Record<string, string>>(spec.map);
  const [vramClass, setVramClass] = useState(spec.vram_class);
  const dirty = JSON.stringify(map) !== JSON.stringify(spec.map) || vramClass !== spec.vram_class;
  const save = async () => {
    try {
      await api.updateWorkflow(spec.template, { map, vram_class: vramClass });
      app.toast(t("saved"), "ok");
      onSaved();
    } catch (e) {
      app.toast((e as Error).message, "bad");
    }
  };
  return (
    <div className="recipe-box stack" style={{ gap: 8 }}>
      <div className="row"><strong>{spec.name}</strong><span className="pill">{spec.kind}</span>
        <select value={vramClass} onChange={(e) => setVramClass(e.target.value)} style={{ padding: "2px 8px" }}>
          {["sdxl", "sd15", "svd"].map((v) => <option key={v}>{v}</option>)}
        </select>
        {spec.auto_detected && <span className="pill gold">auto</span>}
        <span className="mono muted small" style={{ marginLeft: "auto" }}>{spec.template}</span></div>
      <div className="panel-title" style={{ margin: 0 }}>{t("paramMap")}</div>
      {Object.entries(map).map(([k, v]) => (
        <div key={k} className="row small">
          <span className="mono" style={{ width: 140 }}>{k}</span>
          <input className="mono grow" value={v} onChange={(e) => setMap({ ...map, [k]: e.target.value })} />
        </div>
      ))}
      {dirty && <button className="btn sm primary" style={{ alignSelf: "flex-start" }} onClick={save}>{t("save")}</button>}
    </div>
  );
}
