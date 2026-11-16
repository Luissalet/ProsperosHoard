import { useEffect, useState } from "react";
import { ArrowDown, ArrowUp, Clapperboard, ImagePlus, Play, Plus, RefreshCw, Save, Trash2, Wand2 } from "lucide-react";
import { api, fileUrl, type CuratedVoice, type DirectorCatalog, type DirectorFilm, type DirectorShot, type ProductionState } from "../api";
import { useT } from "../i18n";
import { Modal, useApp } from "../components/ui";

const ASPECTS = ["16:9", "9:16", "1:1"] as const;
type Draft = Pick<DirectorShot, "text" | "visual" | "action" | "framing" | "move" | "effects"> & { id?: string };

function useCatalog(lang: string): DirectorCatalog | null {
  const [catalog, setCatalog] = useState<DirectorCatalog | null>(null);
  useEffect(() => { api.directorCatalog(lang === "es" ? "es" : "en").then(setCatalog).catch(() => undefined); }, [lang]);
  return catalog;
}

/** One shot's fields: the narrator's line, the first frame, what moves, framing, camera move and effects. */
function ShotFields({ shot, catalog, onChange, es }: { shot: Draft; catalog: DirectorCatalog; onChange: (s: Draft) => void; es: boolean }) {
  const framing = catalog.axes.find((a) => a.id === "framing")?.options.filter((o) => o.id !== "auto") || [];
  const moves = catalog.presets.filter((p) => p.category === "camera" || p.category === "epic");
  const effects = catalog.presets.filter((p) => p.category === "vfx" || p.category === "mood");
  const toggle = (id: string) => onChange({ ...shot, effects: shot.effects.includes(id) ? shot.effects.filter((e) => e !== id) : [...shot.effects, id].slice(0, 3) });
  return (
    <div className="stack" style={{ gap: 6 }}>
      <label className="field">{es ? "Narración" : "Narration"}
        <textarea rows={2} value={shot.text} onChange={(e) => onChange({ ...shot, text: e.target.value })} /></label>
      <label className="field">{es ? "Imagen inicial (en inglés para el modelo)" : "First frame (English for the model)"}
        <textarea rows={2} value={shot.visual} onChange={(e) => onChange({ ...shot, visual: e.target.value })} /></label>
      <label className="field">{es ? "Qué se mueve" : "What moves"}
        <input value={shot.action} onChange={(e) => onChange({ ...shot, action: e.target.value })} /></label>
      <div className="grid-2">
        <label className="field">{es ? "Encuadre" : "Framing"}
          <select value={shot.framing} onChange={(e) => onChange({ ...shot, framing: e.target.value })}>
            {framing.map((o) => <option key={o.id} value={o.id}>{o.name}</option>)}
          </select></label>
        <label className="field">{es ? "Movimiento de cámara" : "Camera move"}
          <select value={shot.move} onChange={(e) => onChange({ ...shot, move: e.target.value })}>
            {moves.map((p) => <option key={p.id} value={p.id} title={p.prompt}>{catalog.categories[p.category]} · {p.name}</option>)}
          </select></label>
      </div>
      <div className="row wrap small" style={{ gap: 10 }} role="group" aria-label={es ? "Efectos y ambiente" : "Effects and mood"}>
        <span className="muted">{es ? "Efectos y ambiente:" : "Effects and mood:"}</span>
        {effects.map((p) => (
          <label key={p.id} className="row" style={{ gap: 4 }} title={p.prompt}>
            <input type="checkbox" checked={shot.effects.includes(p.id)} onChange={() => toggle(p.id)} /> {p.name}
          </label>
        ))}
      </div>
    </div>
  );
}

/** "New directed film": an idea, the direction, the shot list (written for you, then edited), and roll. */
export function DirectorModal({ onClose, onStarted }: { onClose: () => void; onStarted: (slug: string) => void }) {
  const { lang } = useT();
  const es = lang === "es";
  const app = useApp();
  const catalog = useCatalog(lang);
  const [idea, setIdea] = useState("");
  const [name, setName] = useState("");
  const [language, setLanguage] = useState(es ? "es" : "en");
  const [duration, setDuration] = useState(30);
  const [aspect, setAspect] = useState<(typeof ASPECTS)[number]>("16:9");
  const [picks, setPicks] = useState<Record<string, string>>({});
  const [useEra, setUseEra] = useState(false);
  const [era, setEra] = useState(1980);
  const [move, setMove] = useState("push-in");
  const [engine, setEngine] = useState("auto");
  const [takes, setTakes] = useState(1);
  const [endFrames, setEndFrames] = useState(false);
  const [sfx, setSfx] = useState(true);
  const [music, setMusic] = useState<"none" | "compose">("compose");
  const [musicTags, setMusicTags] = useState("cinematic ambient score, soft strings, slow build, no vocals");
  const [voice, setVoice] = useState("");
  const [curated, setCurated] = useState<CuratedVoice[]>([]);
  const [review, setReview] = useState(true);
  const [shots, setShots] = useState<Draft[] | null>(null);
  const [source, setSource] = useState<string>("");
  const [busy, setBusy] = useState(false);

  useEffect(() => { api.voices().then((r) => setCurated(r.items)).catch(() => undefined); }, []);

  const direction = () => ({
    picks: Object.fromEntries(Object.entries(picks).filter(([, v]) => v && v !== "auto")),
    era: useEra ? era : null, move, engine, takes, end_frames: endFrames, sfx,
  });
  const setPick = (axis: string, value: string) => {
    if (axis !== "genre" || !catalog) return setPicks({ ...picks, [axis]: value });
    const sets = catalog.axes.find((a) => a.id === "genre")?.options.find((o) => o.id === value)?.sets || {};
    const next: Record<string, string> = { ...picks, genre: value };
    for (const [k, v] of Object.entries(sets)) if (!picks[k] || picks[k] === "auto") next[k] = v;
    setPicks(next);
  };
  const planShots = async () => {
    setBusy(true);
    try {
      const out = await api.directorPlan({ idea: idea.trim(), language, duration_s: duration, director: direction() });
      setShots(out.shots.map(({ id, text, visual, action, framing, move: m, effects }) => ({ id, text, visual, action, framing, move: m, effects })));
      setSource(out.source === "llm" ? "" : (es ? "Sin modelo de lenguaje: un plano por frase. Revisa y completa las imágenes." : "No language model: one shot per sentence. Review the pictures."));
      if (!name.trim() && out.title) setName(out.title);
    } catch (e) {
      app.toast((e as Error).message, "bad");
    } finally {
      setBusy(false);
    }
  };
  const roll = async () => {
    setBusy(true);
    try {
      const out = await api.directorCreate({
        idea: idea.trim(), name: name.trim() || undefined, language, duration_s: duration, director: direction(),
        shots: shots || undefined, project: app.projectId || undefined,
        settings: { animatic: review, animatic_autocontinue: !review },
        options: {
          ...(voice ? { voice: { backend: "piper", voice_id: voice } } : {}),
          music: music === "compose" ? { mode: "compose", tags: musicTags.trim() || undefined } : { mode: "none" },
          timeline: { aspects: [aspect], qualities: ["preview"] },
        },
      });
      app.toast(es ? "Película en marcha" : "Film started", "ok");
      app.refreshJobs();
      onStarted(out.production.slug);
    } catch (e) {
      app.toast((e as Error).message, "bad");
    } finally {
      setBusy(false);
    }
  };
  const moveShot = (i: number, d: number) => {
    if (!shots) return;
    const next = [...shots];
    const [s] = next.splice(i, 1);
    next.splice(Math.max(0, Math.min(next.length, i + d)), 0, s);
    setShots(next);
  };
  const ready = !!idea.trim() || !!shots?.length;
  const moves = catalog?.presets.filter((p) => p.category === "camera" || p.category === "epic") || [];

  return (
    <Modal title={es ? "Nueva película dirigida" : "New directed film"} onClose={onClose} wide
      footer={<>
        <button className="btn" disabled={!idea.trim() || busy} onClick={planShots}><Wand2 size={14} /> {shots ? (es ? "Volver a planificar" : "Plan again") : (es ? "Planificar planos" : "Plan the shots")}</button>
        <button className="btn primary" disabled={!ready || busy} onClick={roll}><Play size={14} /> {es ? "Rodar película" : "Roll the film"}</button>
      </>}>
      {!catalog ? <p className="muted">{es ? "Cargando el catálogo…" : "Loading the catalog…"}</p> : (
        <div className="stack">
          <label className="field">{es ? "Idea o historia" : "Idea or story"}
            <textarea rows={3} value={idea} onChange={(e) => setIdea(e.target.value)} autoFocus
              placeholder={es ? "Una farera que mantiene la luz encendida la noche de la gran tormenta." : "A lighthouse keeper keeps the light on through the great storm."} /></label>
          <div className="grid-3">
            <label className="field">{es ? "Título" : "Title"}<input value={name} onChange={(e) => setName(e.target.value)} /></label>
            <label className="field">{es ? "Idioma de la voz" : "Voice language"}
              <select value={language} onChange={(e) => { setLanguage(e.target.value); setVoice(""); }}>
                <option value="es">Español</option><option value="en">English</option><option value="fr">Français</option>
                <option value="it">Italiano</option><option value="pt">Português</option><option value="de">Deutsch</option>
              </select></label>
            <label className="field">{es ? "Duración (s)" : "Length (s)"}
              <input type="number" min={10} max={180} step={5} value={duration} onChange={(e) => setDuration(Number(e.target.value) || 30)} /></label>
            <label className="field">{es ? "Voz" : "Voice"}
              <select value={voice} onChange={(e) => setVoice(e.target.value)}>
                <option value="">{es ? "La de siempre del idioma" : "The language's default"}</option>
                {curated.filter((v) => v.lang.startsWith(language)).map((v) => <option key={v.id} value={v.id}>{v.label}</option>)}
              </select></label>
            <label className="field">{es ? "Formato" : "Aspect"}
              <select value={aspect} onChange={(e) => setAspect(e.target.value as (typeof ASPECTS)[number])}>
                {ASPECTS.map((a) => <option key={a} value={a}>{a}</option>)}
              </select></label>
            <label className="field">{es ? "Motor de vídeo" : "Video engine"}
              <select value={engine} onChange={(e) => setEngine(e.target.value)}>
                {catalog.engines.map((x) => <option key={x.id} value={x.id}>{x.name}</option>)}
              </select></label>
          </div>
          <h3 style={{ margin: "4px 0 0" }}>{es ? "Dirección" : "Direction"}</h3>
          <div className="grid-3">
            {catalog.axes.filter((a) => a.id !== "framing").map((axis) => (
              <label key={axis.id} className="field">{axis.name}
                <select value={picks[axis.id] || "auto"} onChange={(e) => setPick(axis.id, e.target.value)}>
                  {axis.options.map((o) => <option key={o.id} value={o.id} title={o.prompt}>{o.name}</option>)}
                </select></label>
            ))}
            <label className="field">{es ? "Movimiento por defecto" : "Default move"}
              <select value={move} onChange={(e) => setMove(e.target.value)}>
                {moves.map((p) => <option key={p.id} value={p.id}>{catalog.categories[p.category]} · {p.name}</option>)}
              </select></label>
            <label className="field">{es ? "Imágenes por plano" : "Stills per shot"}
              <input type="number" min={1} max={4} value={takes} onChange={(e) => setTakes(Math.max(1, Math.min(4, Number(e.target.value) || 1)))} /></label>
          </div>
          <div className="row wrap" style={{ gap: 14 }}>
            <label className="row small" style={{ gap: 6 }}><input type="checkbox" checked={useEra} onChange={(e) => setUseEra(e.target.checked)} /> {es ? "Época" : "Era"}</label>
            <input type="range" min={1960} max={2020} step={1} value={era} disabled={!useEra} onChange={(e) => setEra(Number(e.target.value))}
              aria-label={es ? "Año de la época" : "Era year"} style={{ width: 180 }} />
            <span className="small mono">{useEra ? era : "—"}</span>
            <label className="row small" style={{ gap: 6 }}><input type="checkbox" checked={endFrames} onChange={(e) => setEndFrames(e.target.checked)} /> {es ? "Cada plano acaba en la imagen del siguiente" : "Each shot ends on the next one's picture"}</label>
            <label className="row small" style={{ gap: 6 }}><input type="checkbox" checked={sfx} onChange={(e) => setSfx(e.target.checked)} /> {es ? "Efecto de sonido en los cortes" : "Sound effect on cuts"}</label>
          </div>
          <div className="grid-2">
            <label className="field">{es ? "Música" : "Music"}
              <select value={music} onChange={(e) => setMusic(e.target.value as "none" | "compose")}>
                <option value="compose">{es ? "Componer (local)" : "Compose (local)"}</option><option value="none">{es ? "Sin música" : "No music"}</option>
              </select></label>
            {music === "compose" ? <label className="field">{es ? "Estilo de la música" : "Music style"}<input value={musicTags} onChange={(e) => setMusicTags(e.target.value)} /></label> : <span />}
          </div>
          <label className="row small" style={{ gap: 6 }}><input type="checkbox" checked={review} onChange={(e) => setReview(e.target.checked)} /> {es ? "Parar en el animático antes de animar los planos" : "Stop at the animatic before animating the shots"}</label>
          {shots && (
            <div className="stack">
              <h3 style={{ margin: "4px 0 0" }}>{es ? `Planos (${shots.length})` : `Shots (${shots.length})`}</h3>
              {source && <p className="small gold-text" style={{ margin: 0 }}>{source}</p>}
              {shots.map((shot, i) => (
                <div key={shot.id || i} className="card" style={{ padding: 10 }}>
                  <div className="row" style={{ justifyContent: "space-between" }}>
                    <strong className="small">{es ? "Plano" : "Shot"} {i + 1}</strong>
                    <span className="row" style={{ gap: 4 }}>
                      <button className="btn sm ghost" aria-label={es ? "Subir" : "Up"} disabled={i === 0} onClick={() => moveShot(i, -1)}><ArrowUp size={13} /></button>
                      <button className="btn sm ghost" aria-label={es ? "Bajar" : "Down"} disabled={i === shots.length - 1} onClick={() => moveShot(i, 1)}><ArrowDown size={13} /></button>
                      <button className="btn sm ghost" aria-label={es ? "Quitar plano" : "Remove shot"} disabled={shots.length < 2} onClick={() => setShots(shots.filter((_, k) => k !== i))}><Trash2 size={13} /></button>
                    </span>
                  </div>
                  <ShotFields shot={shot} catalog={catalog} es={es} onChange={(s) => setShots(shots.map((x, k) => (k === i ? s : x)))} />
                </div>
              ))}
              <button className="btn sm" style={{ alignSelf: "flex-start" }} onClick={() => setShots([...shots, { text: "", visual: "", action: "", framing: "medium", move, effects: [] }])}>
                <Plus size={13} /> {es ? "Añadir plano" : "Add shot"}</button>
            </div>
          )}
        </div>
      )}
    </Modal>
  );
}

/** A directed film shot by shot: each shot's still (and its other takes), its clip, and edits that redo only that shot. */
export function DirectorShots({ state, onChanged }: { state: ProductionState; onChanged: () => void }) {
  const { lang } = useT();
  const es = lang === "es";
  const app = useApp();
  const catalog = useCatalog(lang);
  const [film, setFilm] = useState<DirectorFilm | null>(null);
  const [editing, setEditing] = useState<string | null>(null);
  const [draft, setDraft] = useState<Draft | null>(null);
  const busy = state.status === "running" || state.status === "queued";
  useEffect(() => { api.directorFilm(state.slug).then((r) => setFilm(r.director)).catch(() => undefined); }, [state.slug, state.status, state.stage]);
  if (!film || !film.shots.length) return null;

  const send = async (changes: Record<string, unknown>[]) => {
    try {
      const out = await api.directorEdit(state.slug, changes);
      setFilm(out.director);
      setEditing(null);
      app.toast(es ? "Plano en cola: solo se rehace lo que cambió" : "Queued: only what changed is redone", "ok");
      app.refreshJobs();
      onChanged();
    } catch (e) {
      app.toast((e as Error).message, "bad");
    }
  };
  const name = (id: string) => catalog?.presets.find((p) => p.id === id)?.name || id;
  const framingName = (id: string) => catalog?.axes.find((a) => a.id === "framing")?.options.find((o) => o.id === id)?.name || id;

  return (
    <div className="card">
      <h2><Clapperboard size={16} /> {es ? "Planos" : "Shots"}{film.title ? ` · ${film.title}` : ""}</h2>
      <div className="stack">
        {film.shots.map((shot, i) => (
          <div key={shot.id} className="row" style={{ alignItems: "flex-start", gap: 12, borderTop: i ? "1px solid var(--border)" : undefined, paddingTop: i ? 10 : 0 }}>
            <div className="stack" style={{ gap: 4, width: 220, flex: "none" }}>
              {shot.clip ? (
                <div className="video-frame" style={{ width: 220 }}>
                  <video src={fileUrl(shot.clip)} controls preload="metadata" poster={`/generation/api/assets/${shot.clip}/thumb`} />
                </div>
              ) : shot.still ? (
                <button className="tile" onClick={() => app.openAsset(shot.still!)}><img src={`/generation/api/assets/${shot.still}/thumb`} alt="" loading="lazy" /></button>
              ) : <div className="media-icon" style={{ height: 120 }}><Clapperboard size={22} /></div>}
              {(shot.takes || []).length > 1 && (
                <div className="row wrap" style={{ gap: 4 }} role="group" aria-label={es ? "Otras imágenes del plano" : "Other stills of the shot"}>
                  {(shot.takes || []).map((id) => (
                    <button key={id} className={`tile${id === shot.still ? " on" : ""}`} style={{ width: 50, padding: 0, outline: id === shot.still ? "2px solid var(--accent)" : undefined }}
                      title={id === shot.still ? (es ? "En uso" : "In use") : (es ? "Usar esta imagen" : "Use this still")}
                      disabled={busy || id === shot.still} onClick={() => send([{ id: shot.id, take: id }])}>
                      <img src={`/generation/api/assets/${id}/thumb`} alt="" loading="lazy" />
                    </button>
                  ))}
                </div>
              )}
            </div>
            <div className="stack grow" style={{ gap: 4 }}>
              <div className="row" style={{ justifyContent: "space-between" }}>
                <span className="small muted">{es ? "Plano" : "Shot"} {i + 1} · {framingName(shot.framing)} · {name(shot.move)}
                  {shot.effects.length ? ` · ${shot.effects.map(name).join(", ")}` : ""}
                  {shot.duration_s ? ` · ${shot.duration_s.toFixed(1)} s` : ""}</span>
                <span className="row" style={{ gap: 4 }}>
                  <button className="btn sm" disabled={busy} onClick={() => { setEditing(shot.id); setDraft({ ...shot }); }}>{es ? "Editar" : "Edit"}</button>
                  <button className="btn sm" disabled={busy} onClick={() => send([{ id: shot.id, redo: "image" }])}><ImagePlus size={13} /> {es ? "Otra imagen" : "New still"}</button>
                  <button className="btn sm" disabled={busy || !shot.still} onClick={() => send([{ id: shot.id, redo: "clip" }])}><RefreshCw size={13} /> {es ? "Otro clip" : "New clip"}</button>
                </span>
              </div>
              {editing === shot.id && draft && catalog ? (
                <div className="stack">
                  <ShotFields shot={draft} catalog={catalog} es={es} onChange={setDraft} />
                  <div className="row" style={{ gap: 6 }}>
                    <button className="btn sm primary" onClick={() => send([{ id: shot.id, text: draft.text, visual: draft.visual, action: draft.action, framing: draft.framing, move: draft.move, effects: draft.effects }])}>
                      <Save size={13} /> {es ? "Guardar y rehacer" : "Save and redo"}</button>
                    <button className="btn sm ghost" onClick={() => setEditing(null)}>{es ? "Cancelar" : "Cancel"}</button>
                  </div>
                </div>
              ) : (
                <>
                  <span>{shot.text}</span>
                  <span className="muted" style={{ fontSize: 11.5 }}>{shot.visual}{shot.action ? ` — ${shot.action}` : ""}</span>
                </>
              )}
            </div>
          </div>
        ))}
      </div>
    </div>
  );
}
