// Promo from a web page: the brand of a site is scanned into a kit (logo, three colours, fonts, phrases), a script is written, every frame
// is built in code, checked on a contact sheet and rendered in 16:9 and 9:16 with a soundtrack written in code.
import { useEffect, useState } from "react";
import { AlertTriangle, CheckCircle2, Film, Loader2, Megaphone, Palette, Play, ScanSearch } from "lucide-react";
import { api, fileUrl, type Asset, type PromoKit } from "../api";
import { useT } from "../i18n";
import { useApp, useAsync, useTrackedJob } from "../components/ui";

const L = (lang: string, en: string, es: string) => (lang === "es" ? es : en);
const ISSUE_LABELS: Record<string, [string, string]> = {
  text_out: ["Text outside the frame or its margin", "Texto fuera del cuadro o de su margen"],
  text_cut: ["Text cut off", "Texto cortado"],
  text_small: ["Text too small to read", "Texto demasiado pequeño"],
  overlap: ["Text overlaps other text", "Texto solapado"],
  contrast: ["Not enough contrast", "Contraste insuficiente"],
  palette: ["Colour outside the brand palette", "Color fuera de la paleta de la marca"],
  frozen: ["Shot without change", "Plano sin cambio"],
  jump: ["Jump between shots", "Salto entre planos"],
  slideshow: ["Reads as a slideshow", "Parece una presentación de diapositivas"],
};
interface Issue { kind: string; shot: string; t: number; detail: string; severity: string; format?: string }

export function PromoFromWebView() {
  const { lang } = useT();
  const app = useApp();
  const projectId = app.projectId!;
  const { data: options } = useAsync(() => api.promoOptions(), []);
  const { data: kits, reload: reloadKits } = useAsync(() => api.promoKits(), []);
  const { data: videoPage, reload: reloadVideos } = useAsync(() => api.assets(projectId, { kind: "video", limit: 200 }), [projectId, app.dataVersion]);
  const { data: audioPage } = useAsync(() => api.assets(projectId, { kind: "audio", limit: 100 }), [projectId, app.dataVersion]);
  const [url, setUrl] = useState("");
  const [length, setLength] = useState(30);
  const [formats, setFormats] = useState<string[]>(["16:9", "9:16"]);
  const [vibe, setVibe] = useState("keynote");
  const [fps, setFps] = useState(60);
  const [language, setLanguage] = useState("auto");
  const [music, setMusic] = useState("");
  const [kit, setKit] = useState<PromoKit | null>(null);
  const [kitAssets, setKitAssets] = useState<Record<string, string>>({});
  const [busy, setBusy] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);
  const job = useTrackedJob(jobId);
  const finished = job && ["done", "failed", "cancelled"].includes(job.state) ? job : null;
  const running = !!job && !finished;
  const result = finished?.state === "done" ? (finished.outputs as Record<string, any> | null) : null;
  const promos = (videoPage?.items || []).filter((a: Asset) => a.tags.includes("promo"));

  useEffect(() => {
    if (!finished) return;
    if (finished.state === "done") { app.bump(); reloadVideos(); reloadKits(); app.toast(L(lang, "Promo ready", "Promo listo"), "ok"); setError(null); }
    else setError(finished.message || finished.state);
  }, [finished?.id, finished?.state]); // eslint-disable-line react-hooks/exhaustive-deps

  const act = async (what: string, fn: () => Promise<void>) => {
    setBusy(what); setError(null);
    try { await fn(); } catch (e) { setError((e as Error).message); } finally { setBusy(null); }
  };
  const scan = () => act("scan", async () => {
    const r = await api.brandScan(url, projectId);
    setKit(r); setKitAssets(r.assets || {}); app.bump(); reloadKits();
  });
  const create = () => act("render", async () => {
    const r = await api.promoRender(projectId, { url: kit ? undefined : url, kit_id: kit?.id, length_s: length, formats, vibe, fps, language,
      music_asset_id: music || undefined });
    setJobId(r.job.id); app.refreshJobs(); app.toast(L(lang, "Making the promo", "Haciendo el promo"), "info");
  });
  const loadKit = (id: string) => act("kit", async () => { const k = await api.promoKit(id); setKit(k); setUrl(k.url); setKitAssets({}); });
  const toggle = (f: string) => setFormats((cur) => (cur.includes(f) ? cur.filter((x) => x !== f) : [...cur, f]));
  const issues: Issue[] = (result?.checks?.remaining as Issue[]) || [];
  const canCreate = !busy && !running && formats.length > 0 && (!!kit || !!url.trim());

  return (
    <div className="stack promo-web">
      <div className="card">
        <h2><Megaphone size={17} /> {L(lang, "Promo from a web page", "Promo desde una web")}</h2>
        <p className="small muted">{L(lang,
          "Give the address of a page: its logo, three main colours and fonts are scanned, the shots are written, every frame is built in code, checked on a contact sheet and rendered in 16:9 and 9:16 with sound written in code.",
          "Indica la dirección de una web: se escanean su logo, sus tres colores principales y sus fuentes, se escriben los planos, cada fotograma se construye con código, se revisa en una hoja de contactos y se renderiza en 16:9 y 9:16 con un sonido escrito en código.")}</p>
        <div className="row wrap" style={{ gap: 8 }}>
          <input className="grow" value={url} onChange={(e) => { setUrl(e.target.value); setKit(null); }} placeholder="https://www.python.org" data-testid="promo-url" />
          <button className="btn" disabled={!!busy || running || !url.trim()} onClick={scan} data-testid="promo-scan">
            {busy === "scan" ? <Loader2 size={14} className="spin" /> : <ScanSearch size={14} />} {L(lang, "Scan the brand", "Escanear la marca")}</button>
          {(kits?.items || []).length > 0 && (
            <select value="" aria-label={L(lang, "Saved brand kits", "Kits de marca guardados")} data-testid="promo-kits" onChange={(e) => e.target.value && loadKit(e.target.value)}>
              <option value="">{L(lang, "Saved brand kits…", "Kits de marca guardados…")}</option>
              {(kits?.items || []).map((k) => <option key={k.id} value={k.id}>{k.site_name || k.host}</option>)}
            </select>)}
        </div>
        <div className="row wrap" style={{ gap: 12, marginTop: 12 }}>
          <label className="small">{L(lang, "Length (s)", "Duración (s)")}{" "}
            <input type="number" min={options?.length_s.min ?? 6} max={options?.length_s.max ?? 120} value={length} style={{ width: 80 }}
              onChange={(e) => setLength(Number(e.target.value))} data-testid="promo-length" /></label>
          <span className="small">{L(lang, "Formats", "Formatos")}:{" "}
            {Object.keys(options?.formats || { "16:9": 1, "9:16": 1 }).map((f) => (
              <label key={f} style={{ marginRight: 10 }}><input type="checkbox" checked={formats.includes(f)} onChange={() => toggle(f)} data-testid={`promo-format-${f}`} /> {f}</label>))}</span>
          <label className="small">{L(lang, "Style", "Estilo")}{" "}
            <select value={vibe} onChange={(e) => setVibe(e.target.value)} data-testid="promo-vibe">
              {Object.entries(options?.vibes || { keynote: { label: "Keynote", about: "" } }).map(([id, v]) => <option key={id} value={id} title={v.about}>{v.label}</option>)}
            </select></label>
          <label className="small">{L(lang, "Frame rate", "Fotogramas")}{" "}
            <select value={fps} onChange={(e) => setFps(Number(e.target.value))}>
              {(options?.fps || [60, 30]).map((f) => <option key={f} value={f}>{f} fps</option>)}</select></label>
          <label className="small">{L(lang, "Text language", "Idioma del texto")}{" "}
            <select value={language} onChange={(e) => setLanguage(e.target.value)}>
              <option value="auto">{L(lang, "As the site", "El de la web")}</option><option value="es">Castellano</option><option value="en">English</option></select></label>
          <label className="small">{L(lang, "Music", "Música")}{" "}
            <select value={music} onChange={(e) => setMusic(e.target.value)} data-testid="promo-music">
              <option value="">{L(lang, "Sound written in code", "Sonido escrito en código")}</option>
              {(audioPage?.items || []).map((a: Asset) => <option key={a.id} value={a.id}>{a.name}</option>)}</select></label>
        </div>
        <div className="row wrap" style={{ gap: 8, marginTop: 12 }}>
          <button className="btn primary" disabled={!canCreate} onClick={create} data-testid="promo-create">
            {running || busy === "render" ? <Loader2 size={14} className="spin" /> : <Play size={14} />} {L(lang, "Make the promo", "Hacer el promo")}</button>
          {job && !finished && <span className="small muted" data-testid="promo-progress">{Math.round((job.progress || 0) * 100)}% · {job.message || job.state}</span>}
        </div>
        {error && <div className="manim-error" role="alert" data-testid="promo-error"><AlertTriangle size={15} /> <span>{error}</span></div>}
      </div>

      {kit && (
        <div className="card" data-testid="promo-kit">
          <h2><Palette size={16} /> {L(lang, "Brand kit", "Kit de marca")} <span className="pill">{kit.site_name}</span></h2>
          <div className="row wrap" style={{ gap: 16, alignItems: "flex-start" }}>
            {kitAssets.logo && <img src={fileUrl(kitAssets.logo)} alt="logo" style={{ maxWidth: 220, maxHeight: 90, background: "#8884", borderRadius: 8, padding: 8 }} data-testid="promo-logo" />}
            <div className="row" style={{ gap: 8 }}>{kit.colors.map((c) => (
              <div key={c.role} className="small" style={{ textAlign: "center" }} data-testid="promo-colour">
                <div style={{ width: 72, height: 48, borderRadius: 8, background: c.hex, border: "1px solid #8886" }} />
                <div>{c.hex.toUpperCase()}</div><div className="muted">{c.role}{c.derived ? "*" : ""}</div></div>))}</div>
            <div className="small grow" style={{ minWidth: 240 }}>
              <div><strong>{kit.headline}</strong></div>
              {kit.value_prop && <div className="muted">{kit.value_prop}</div>}
              <div className="muted" style={{ marginTop: 6 }}>{L(lang, "Fonts", "Fuentes")}: {kit.fonts.display.family} / {kit.fonts.body.family}
                {" "}({kit.fonts.display.source === "site" ? L(lang, "the site's own", "las de la web") : L(lang, "open substitute", "sustituta libre")})</div>
              <ul>{kit.phrases.map((p) => <li key={p}>{p}</li>)}</ul>
            </div>
          </div>
          {kitAssets.board && <img className="tr-sheet" src={fileUrl(kitAssets.board)} alt="brand board" onClick={() => app.openAsset(kitAssets.board)} style={{ maxWidth: 560 }} />}
        </div>
      )}

      {result && (
        <div className="card" data-testid="promo-result">
          <h2><Film size={16} /> {L(lang, "Result", "Resultado")}
            {issues.length === 0 && <span className="pill ok" style={{ marginLeft: 8 }}><CheckCircle2 size={12} /> {L(lang, "checks passed", "revisión superada")}</span>}</h2>
          <p className="small muted">{L(lang, "Script written by", "Guion escrito por")}: {result.written_by === "model" ? L(lang, "the writing model", "el modelo de escritura") : L(lang, "a template from the site's phrases", "una plantilla con las frases de la web")}
            {" · "}{result.checks?.rounds} {L(lang, "check round(s)", "ronda(s) de revisión")}
            {result.loudness?.lufs != null ? ` · ${result.loudness.lufs} LUFS` : ""}</p>
          {(result.checks?.fixes || []).length > 0 && <div className="small" data-testid="promo-fixes"><strong>{L(lang, "Corrected automatically", "Corregido automáticamente")}</strong>
            <ul>{(result.checks.fixes as string[]).map((f) => <li key={f}>{f}</li>)}</ul></div>}
          {issues.length > 0 && <div className="manim-warn" data-testid="promo-issues"><strong><AlertTriangle size={14} /> {L(lang, "Still to look at", "Pendiente de mirar")}</strong>
            <ul className="small">{issues.map((i, n) => <li key={n}>{(ISSUE_LABELS[i.kind] || [i.kind, i.kind])[lang === "es" ? 1 : 0]} · {i.format} · {i.shot} · {i.t.toFixed(2)} s</li>)}</ul></div>}
          <div className="manim-clips">{Object.entries((result.videos || {}) as Record<string, string>).map(([fmt, id]) => (
            <div key={fmt} className="manim-clip" data-testid="promo-video">
              <div className="small"><strong>{fmt}</strong> · {result.files?.[fmt]?.width}x{result.files?.[fmt]?.height} · {result.files?.[fmt]?.fps} fps · {result.files?.[fmt]?.audio?.codec}</div>
              <video src={fileUrl(id)} controls preload="metadata" poster={`/generation/api/assets/${id}/thumb`} />
              {result.contact_sheets?.[fmt] && <img className="tr-sheet" src={fileUrl(result.contact_sheets[fmt])} alt={L(lang, "Contact sheet", "Hoja de contactos")}
                onClick={() => app.openAsset(result.contact_sheets[fmt])} data-testid="promo-sheet" />}
            </div>))}</div>
          <button className="btn sm" onClick={() => app.go("delivery")}>{L(lang, "Use it in an edit plan", "Usar en un plan de montaje")}</button>
        </div>
      )}

      {promos.length > 0 && (
        <div className="card">
          <h2><Film size={16} /> {L(lang, "Promos of this project", "Promos de este proyecto")}</h2>
          <div className="manim-clips">{promos.map((a) => (
            <div key={a.id} className="manim-clip" data-testid="promo-clip">
              <video src={fileUrl(a.id)} controls preload="metadata" poster={`/generation/api/assets/${a.id}/thumb`} />
              <div className="small">{a.name} · {a.duration_s?.toFixed(1)} s</div>
            </div>))}</div>
        </div>
      )}
    </div>
  );
}
