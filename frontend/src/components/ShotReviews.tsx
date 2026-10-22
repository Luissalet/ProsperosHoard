import { useEffect, useState } from "react";
import { api, type Asset, type ProductionState, type ShotComment, type StyleProfile } from "../api";
import { useApp, useAsync } from "./ui";
import { useT } from "../i18n";

export function ShotReviewsCard({ state, onChanged }: { state: ProductionState; onChanged: () => void }) {
  const { lang } = useT();
  const es = lang === "es";
  const app = useApp();
  const comments = useAsync(() => api.shotComments(state.slug), [state.slug, state.lineage.length]);
  const profiles = useAsync(() => api.styleProfiles(), [state.slug]);
  const assets = useAsync(() => state.project_id ? api.assets(state.project_id, { kind: "video", limit: 200 }) : Promise.resolve({ items: [] as Asset[] }), [state.project_id]);
  const [selected, setSelected] = useState(state.style_profile?.id || "");
  const [profile, setProfile] = useState<StyleProfile | null>(null);
  const [video, setVideo] = useState("");
  const [targetDuration, setTargetDuration] = useState(state.style_profile?.target_duration_s || 15);
  const [shot, setShot] = useState(state.spec.shots?.[0]?.key || "");
  const [time, setTime] = useState(0);
  const [content, setContent] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const shots = state.spec.shots || [];

  useEffect(() => {
    setSelected(state.style_profile?.id || "");
    setTargetDuration(state.style_profile?.target_duration_s || 15);
    setShot(state.spec.shots?.[0]?.key || "");
  }, [state.slug]);

  useEffect(() => {
    let alive = true;
    setProfile(null);
    if (selected) api.styleProfile(selected).then(p => { if (alive) setProfile(p); }).catch(e => { if (alive) setError(e.message); });
    return () => { alive = false; };
  }, [selected]);
  useEffect(() => {
    const item = shots.find(s => s.key === shot);
    setTime(item?.start_s || 0);
  }, [shot, state.slug]);

  async function act(fn: () => Promise<void>) {
    setBusy(true); setError(""); setNotice("");
    try { await fn(); } catch (e) { setError((e as Error).message); }
    finally { setBusy(false); }
  }
  async function extract(assetId: string) {
    const p = await api.extractStyleProfile({ asset_id: assetId });
    profiles.reload(); setSelected(p.id); setProfile(p);
    setNotice(es ? "Perfil extraído. Revisa las medidas y edita las decisiones de cámara antes de aplicarlo." : "Profile extracted. Review measurements and edit camera choices before applying it.");
  }
  function editShot(index: number, patch: Partial<StyleProfile["shots"][number]>) {
    if (profile) setProfile({ ...profile, shots: profile.shots.map((s, i) => i === index ? { ...s, ...patch } : s) });
  }
  async function regenerate(key: string) {
    const r = await api.regenerateDependent(state.slug, { keys: [key], stage: "clips", run: true });
    setNotice(es ? `Planos a regenerar: ${r.regenerated_shots.join(", ")}. Conservados: ${r.preserved_shots.join(", ")}.` : `Shots queued: ${r.regenerated_shots.join(", ")}. Preserved: ${r.preserved_shots.join(", ")}.`);
    comments.reload(); onChanged(); app.refreshJobs();
  }
  const label = (a: string, b: string) => es ? a : b;
  return <div className="stack" aria-busy={busy}>
    {error && <p role="alert" className="small bad">{error}</p>}
    {notice && <p role="status" className="small">{notice}</p>}
    <section className="card stack">
      <h2>{label("Estilo de referencia", "Reference style")}</h2>
      <p className="small muted">{label("Extrae ritmo y colores de un vídeo local. La cámara se decide al editar; el análisis de cortes no observa sus movimientos.", "Extract rhythm and colors from a local video. Camera choices are editable; cut analysis does not observe camera movement.")}</p>
      <div className="row wrap">
        <label className="field grow">{label("Vídeo del proyecto", "Project video")}
          <select value={video} onChange={e => setVideo(e.target.value)} disabled={busy || assets.loading}>
            <option value="">{label("Selecciona un vídeo", "Choose a video")}</option>
            {(assets.data?.items || []).map(a => <option key={a.id} value={a.id}>{a.name || a.id}</option>)}
          </select>
        </label>
        <button className="btn" disabled={busy || !video} onClick={() => act(() => extract(video))}>{label("Extraer perfil", "Extract profile")}</button>
        <label className="btn">{label("Importar vídeo", "Import video")}<input type="file" accept="video/*" disabled={busy || !state.project_id} onChange={e => {
          const file = e.target.files?.[0];
          if (file && state.project_id) act(async () => { const a = await api.upload(state.project_id!, file); assets.reload(); setVideo(a.id); await extract(a.id); });
          e.target.value = "";
        }} /></label>
      </div>
      {assets.error && <p role="alert">{assets.error}</p>}
      {!assets.loading && !assets.data?.items.length && <p className="small muted">{label("No hay vídeos en este proyecto. Importa una referencia para analizarla localmente.", "This project has no videos. Import a reference to analyze it locally.")}</p>}
      <label className="field">{label("Perfil reutilizable", "Reusable profile")}
        <select value={selected} onChange={e => setSelected(e.target.value)} disabled={busy || profiles.loading}>
          <option value="">{label("Selecciona un perfil", "Choose a profile")}</option>
          {(profiles.data?.profiles || []).map(p => <option key={p.id} value={p.id}>{p.name} · {p.shot_count} {label("planos", "shots")}</option>)}
        </select>
      </label>
      {profiles.error && <p role="alert">{profiles.error}</p>}
      {profile && <>
        <label className="field">{label("Nombre del perfil", "Profile name")}<input value={profile.name} onChange={e => setProfile({ ...profile, name: e.target.value })} disabled={busy} /></label>
        <p className="small muted">{label("Cortes estimados por cambio de imagen; paleta tomada en el centro de cada plano. Los campos editados son decisiones creativas.", "Cuts are estimated from image changes; palettes are sampled at each shot midpoint. Edited fields are creative decisions.")}</p>
        <div className="row wrap">{profile.palette.map((color, i) => <label className="field" key={i}>{label("Color para todos los planos", "Color for all shots")} {i + 1}<input type="color" value={color} disabled={busy} onChange={e => {
          const palette = profile.palette.map((c, j) => j === i ? e.target.value : c);
          setProfile({ ...profile, palette, shots: profile.shots.map(s => ({ ...s, palette })) });
        }} /></label>)}</div>
        {profile.shots.map((s, i) => <details key={s.key}>
          <summary>{label("Plano", "Shot")} {i + 1} · {s.span.duration_s.toFixed(2)} s {label("en la referencia", "in the reference")}</summary>
          <div className="row wrap">
            <label className="field">{label("Peso de duración", "Duration weight")}<input type="number" min="0.01" step="0.01" value={s.relative_duration} disabled={busy} onChange={e => editShot(i, { relative_duration: Number(e.target.value) })} /></label>
            {(["shot_type", "angle", "movement"] as const).map((key, j) => <label className="field" key={key}>{[label("Encuadre", "Framing"), label("Ángulo", "Angle"), label("Movimiento de cámara", "Camera movement")][j]}
              <input value={s.camera[key] === "unknown" ? "" : s.camera[key]} placeholder={label("Sin observar", "Unobserved")} disabled={busy} onChange={e => editShot(i, { camera: { ...s.camera, [key]: e.target.value || "unknown", observed: false } })} />
            </label>)}
            <label className="field">{label("Transición de entrada", "Incoming transition")}<select value={s.transition_in.type === "none" ? "cut" : s.transition_in.type} disabled={busy} onChange={e => editShot(i, { transition_in: { ...s.transition_in, type: e.target.value, observed: false, measured: false } })}>
              <option value="cut">{label("Corte", "Cut")}</option><option value="crossfade">{label("Encadenado", "Crossfade")}</option><option value="dip_black">{label("Fundido a negro", "Dip to black")}</option><option value="flash_white">{label("Destello blanco", "White flash")}</option>
            </select></label>
          </div>
        </details>)}
        <div className="row wrap">
          <button className="btn" disabled={busy} onClick={() => act(async () => { setProfile(await api.updateStyleProfile(profile.id, profile)); profiles.reload(); setNotice(label("Perfil guardado.", "Profile saved.")); })}>{label("Guardar perfil", "Save profile")}</button>
          <label className="field">{label("Duración de destino (s)", "Target duration (s)")}<input type="number" min="0.5" step="0.5" value={targetDuration} disabled={busy} onChange={e => setTargetDuration(Number(e.target.value))} /></label>
          <button className="btn primary" disabled={busy || !Number.isFinite(targetDuration) || targetDuration <= 0} onClick={() => act(async () => { await api.updateStyleProfile(profile.id, profile); await api.applyStyleProfile(state.slug, { profile_id: profile.id, target_duration_s: targetDuration }); onChanged(); setNotice(label("Perfil aplicado. Se conservan los planos aprobados y las tomas anteriores.", "Profile applied. Approved shots and previous takes are retained.")); })}>{label("Aplicar a esta producción", "Apply to this production")}</button>
        </div>
      </>}
    </section>
    <section className="card stack">
      <h2>{label("Revisión por plano", "Shot review")}</h2>
      <p className="small muted">{label("Cada comentario conserva el instante y la identidad de la toma. Una corrección puede regenerar el plano y sus continuaciones; las tomas anteriores siguen disponibles.", "Comments retain their timestamp and take identity. A correction can regenerate a shot and its continuations; previous takes remain available.")}</p>
      <div className="row wrap">
        <label className="field">{label("Plano", "Shot")}<select value={shot} disabled={busy} onChange={e => setShot(e.target.value)}>{shots.map(s => <option key={s.key} value={s.key}>{s.key}{s.locked ? label(" · aprobado", " · approved") : ""}</option>)}</select></label>
        <label className="field">{label("Instante en la producción (s)", "Production timestamp (s)")}<input type="number" min="0" step="0.1" value={time} disabled={busy} onChange={e => setTime(Number(e.target.value))} /></label>
      </div>
      <label className="field">{label("Comentario", "Comment")}<textarea value={content} disabled={busy} onChange={e => setContent(e.target.value)} /></label>
      <button className="btn" disabled={busy || !shot || !content.trim() || !Number.isFinite(time)} onClick={() => act(async () => { await api.addShotComment(state.slug, { shot_key: shot, time_s: time, content }); setContent(""); comments.reload(); })}>{label("Guardar comentario", "Save comment")}</button>
      {comments.loading && <p role="status">{label("Cargando comentarios…", "Loading comments…")}</p>}
      {comments.error && <p role="alert">{comments.error}</p>}
      {!comments.loading && !comments.data?.comments.length && <p className="small muted">{label("Todavía no hay comentarios sobre esta producción.", "This production has no comments yet.")}</p>}
      {(comments.data?.comments || []).map((c: ShotComment) => <article key={c.id}>
        <p><strong>{label("Plano", "Shot")} {c.shot_key} · {c.time_s.toFixed(1)} s</strong>{c.resolved ? label(" · resuelto", " · resolved") : ""}{c.is_stale ? label(" · la toma ha cambiado", " · take has changed") : ""}</p>
        <p>{c.content}</p><p className="small muted">{c.author} · {new Date(c.created_at).toLocaleString(lang)}</p>
        <div className="row wrap">
          {!c.resolved && <button className="btn sm" disabled={busy} onClick={() => act(async () => { await api.resolveShotComment(state.slug, c.id); comments.reload(); })}>{label("Marcar como resuelto", "Mark resolved")}</button>}
          {!c.resolved && <button className="btn sm" disabled={busy || !!shots.find(s => s.key === c.shot_key)?.locked} onClick={() => act(() => regenerate(c.shot_key))}>{label("Regenerar plano y dependientes", "Regenerate shot and dependents")}</button>}
        </div>
      </article>)}
    </section>
  </div>;
}
