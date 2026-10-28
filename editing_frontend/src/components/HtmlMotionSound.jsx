import React, { useEffect, useState } from "react";
import { api as appApi } from "../api.js";

const words = {
  en: { title: "Sound", hint: "Mark the picture moments that deserve a sound. Each effect is generated from code and placed by its measured sync point (taps and knocks lead the picture a few milliseconds, whooshes follow it); the mix is mastered to -14 LUFS under -1 dBTP.",
    none: "No sound events yet.", time: "Time (s)", role: "Role", gain: "Gain (dB)", add: "Add event", remove: "Remove", apply: "Save sound", saved: "Sound saved as revision",
    music: "Music bed", noMusic: "No music", musicGain: "Music gain (dB)", lufs: "Target loudness (LUFS)", preview: "Listen to the mix", previewing: "Mixing",
    exportAudio: "Export with sound", loud: "Loudness", peak: "True peak", sync: "Worst sync error", effects: "effects", notes: "Notes", dirty: "Save the composition before changing its sound.",
    where: "Sync point", offset: "Offset", ms: "ms", owned: "The sound events come from this animation's cues: change them in the Direction section. Music and loudness can still be changed here." },
  es: { title: "Sonido", hint: "Marca los momentos de la imagen que merecen un sonido. Cada efecto se genera desde código y se coloca por su punto de sincronía medido (los toques y golpes se adelantan unos milisegundos a la imagen, los whoosh la siguen); la mezcla se masteriza a -14 LUFS bajo -1 dBTP.",
    none: "Todavía no hay eventos de sonido.", time: "Tiempo (s)", role: "Rol", gain: "Ganancia (dB)", add: "Añadir evento", remove: "Quitar", apply: "Guardar sonido", saved: "Sonido guardado como revisión",
    music: "Música de fondo", noMusic: "Sin música", musicGain: "Ganancia de la música (dB)", lufs: "Sonoridad objetivo (LUFS)", preview: "Escuchar la mezcla", previewing: "Mezclando",
    exportAudio: "Exportar con sonido", loud: "Sonoridad", peak: "Pico real", sync: "Peor error de sincronía", effects: "efectos", notes: "Notas", dirty: "Guarda la composición antes de cambiar su sonido.",
    where: "Punto de sincronía", offset: "Desfase", ms: "ms", owned: "Los eventos de sonido salen de los hitos de esta animación: se cambian en la sección Dirección. La música y la sonoridad se pueden cambiar aquí." },
};

async function request(path, options = {}) {
  const response = await fetch(path, { ...options, headers: { "Content-Type": "application/json" } });
  const data = await response.json();
  if (!response.ok) throw new Error(data.error || data.detail || `HTTP ${response.status}`);
  return data;
}

export default function HtmlMotionSound({ saved, dirty, busy, lang, exportAudio, setExportAudio, onChanged, operate, cuesOwn = false }) {
  const t = words[String(lang).startsWith("es") ? "es" : "en"];
  const [roles, setRoles] = useState([]);
  const [library, setLibrary] = useState([]);
  const [events, setEvents] = useState([]);
  const [music, setMusic] = useState("");
  const [musicGain, setMusicGain] = useState(0);
  const [lufs, setLufs] = useState(-14);
  const [mix, setMix] = useState(null);
  const [notice, setNotice] = useState("");
  const declared = saved?.sound || null;
  const key = `${saved?.id}:${saved?.revision}`;

  useEffect(() => {
    request("/editing/api/html-motion/sound/roles").then((data) => setRoles(data.roles.filter((role) => role.sounded !== false))).catch(() => {});
    appApi.media().then((data) => setLibrary((data.media || []).filter((item) => item.has_audio))).catch(() => {});
  }, []);
  useEffect(() => {
    setEvents((declared?.events || []).map((event) => ({ t: event.t, role: event.role, gain_db: event.gain_db ?? "" })));
    setMusic(declared?.music?.media || "");
    setMusicGain(declared?.music?.gain_db ?? 0);
    setLufs(declared?.lufs ?? -14);
    setMix(null); setNotice("");
  }, [key]);

  const edit = (index, patch) => setEvents((rows) => rows.map((row, i) => (i === index ? { ...row, ...patch } : row)));
  const add = () => setEvents((rows) => [...rows, { t: rows.length ? Math.min(Number(saved.duration), Number(rows[rows.length - 1].t) + 0.5) : 0, role: roles[0]?.role || "tap", gain_db: "" }]);
  const apply = () => operate(async () => {
    const body = {
      composition: saved.id, action: cuesOwn ? "get" : "set", expected_revision: saved.revision, lufs: Number(lufs),
      ...(cuesOwn ? {} : { events: events.map((row) => ({ t: Number(row.t), role: row.role, ...(row.gain_db === "" ? {} : { gain_db: Number(row.gain_db) }) })) }),
      ...(music ? { music: { media: music, gain_db: Number(musicGain) } } : { clear_music: true }),
    };
    const result = await request(`/editing/api/html-motion/${saved.id}/sound`, { method: "POST", body: JSON.stringify(body) });
    setNotice(`${t.saved} ${result.revision}`);
    await onChanged?.();
  });
  const listen = () => operate(async () => {
    setMix(null);
    setMix(await request(`/editing/api/html-motion/${saved.id}/sound/preview?revision=${saved.revision}`, { method: "POST", body: "{}" }));
  });
  const roleInfo = (name) => roles.find((role) => role.role === name);

  return (
    <fieldset className="html-motion-sound" disabled={dirty}>
      <legend>{t.title}</legend>
      <p className="muted">{t.hint}</p>
      {dirty && <p className="muted">{t.dirty}</p>}
      {cuesOwn && <p className="muted" role="status">{t.owned}</p>}
      {events.length === 0 ? <p className="muted">{t.none}</p> : (
        <table className="html-motion-sound-table">
          <thead><tr><th>{t.time}</th><th>{t.role}</th><th>{t.gain}</th><th>{t.where}</th><th /></tr></thead>
          <tbody>{events.map((row, index) => {
            const info = roleInfo(row.role);
            return (
              <tr key={index} aria-disabled={cuesOwn}>
                <td><input className="field" disabled={cuesOwn} aria-label={`${t.time} ${index + 1}`} type="number" min="0" max={saved.duration} step="any" value={row.t} onChange={(e) => edit(index, { t: e.target.value })} /></td>
                <td><select className="field" disabled={cuesOwn} aria-label={`${t.role} ${index + 1}`} value={row.role} onChange={(e) => edit(index, { role: e.target.value })}>
                  {roles.map((role) => <option key={role.role} value={role.role}>{role.role}</option>)}
                  {!roleInfo(row.role) && <option value={row.role}>{row.role}</option>}
                </select></td>
                <td><input className="field" disabled={cuesOwn} aria-label={`${t.gain} ${index + 1}`} type="number" step="any" placeholder="0" value={row.gain_db} onChange={(e) => edit(index, { gain_db: e.target.value })} /></td>
                <td className="muted">{info ? `${info.align || info.sync_point} ${info.offset_ms > 0 ? "+" : ""}${info.offset_ms} ${t.ms}` : ""}</td>
                <td><button type="button" className="btn" disabled={cuesOwn} onClick={() => setEvents((rows) => rows.filter((_, i) => i !== index))}>{t.remove}</button></td>
              </tr>
            );
          })}</tbody>
        </table>
      )}
      <div className="html-motion-actions">
        <button type="button" className="btn" disabled={busy || cuesOwn || !roles.length} onClick={add}>{t.add}</button>
        <label className="field-label"><span>{t.music}</span>
          <select className="field" value={music} onChange={(e) => setMusic(e.target.value)}>
            <option value="">{t.noMusic}</option>{library.map((item) => <option key={item.id} value={item.id}>{item.name}</option>)}
          </select></label>
        {music && <label className="field-label"><span>{t.musicGain}</span><input className="field" type="number" step="any" value={musicGain} onChange={(e) => setMusicGain(e.target.value)} /></label>}
        <label className="field-label"><span>{t.lufs}</span><input className="field" type="number" min="-30" max="-8" step="any" value={lufs} onChange={(e) => setLufs(e.target.value)} /></label>
      </div>
      <div className="html-motion-actions">
        <button type="button" className="btn btn-primary" disabled={busy || dirty} onClick={apply}>{t.apply}</button>
        <button type="button" className="btn" disabled={busy || dirty || !declared} onClick={listen}>{t.preview}</button>
        <label className="html-motion-check"><input type="checkbox" checked={exportAudio} onChange={(e) => setExportAudio(e.target.checked)} /> {t.exportAudio}</label>
        {notice && <span role="status">{notice}</span>}
      </div>
      {mix && <div className="html-motion-mix">
        <audio controls src={mix.url} aria-label={t.preview} />
        <p>{t.loud}: {mix.lufs} LUFS · {t.peak}: {mix.true_peak_db} dBTP · {t.sync}: {mix.max_sync_error_ms ?? "-"} {t.ms} · {mix.placements?.length || 0} {t.effects}</p>
        {mix.notes?.length > 0 && <p className="muted">{t.notes}: {mix.notes.join(" ")}</p>}
      </div>}
    </fieldset>
  );
}
