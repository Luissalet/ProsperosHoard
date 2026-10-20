import { useState } from "react";
import { Plus, Users, X, ImagePlus, Loader2 } from "lucide-react";
import { api, thumbUrl, type Character, type Group } from "../api";
import { useT } from "../i18n";
import { AssetPicker, useApp, useAsync } from "./ui";

export function ProjectCast() {
  const app = useApp();
  const { lang } = useT();
  const es = lang === "es";
  const pid = app.projectId!;
  const characters = useAsync(() => api.characters(pid), [pid, app.dataVersion]);
  const groups = useAsync(() => api.groups(pid), [pid, app.dataVersion]);
  const [draft, setDraft] = useState<{ kind: "character" | "group"; id?: string; name: string; prompt: string; ref: string | null; members: string[] } | null>(null);
  const [picking, setPicking] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const chars = characters.data?.items || [];
  const start = (kind: "character" | "group") => { setError(""); setDraft({ kind, name: "", prompt: "", ref: null, members: [] }); };
  const editCharacter = (char: Character) => { setError(""); setDraft({ kind: "character", id: char.id, name: char.name, prompt: char.prompt || "", ref: char.canonical_asset_id, members: [] }); };
  const editGroup = (group: Group) => { setError(""); setDraft({ kind: "group", id: group.id, name: group.name, prompt: group.concept || "", members: group.member_ids || [], ref: null }); };
  const save = async () => {
    if (!draft?.name.trim() || busy) return;
    setBusy(true); setError("");
    try {
      if (draft.kind === "character") {
        const fields = { name: draft.name.trim(), prompt: draft.prompt, canonical_asset_id: draft.ref };
        if (draft.id) await api.updateCharacter(draft.id, fields);
        else await api.createCharacter(pid, fields.name, { prompt: fields.prompt, canonical_asset_id: fields.canonical_asset_id, element: "character" });
      } else {
        const fields = { name: draft.name.trim(), concept: draft.prompt, member_ids: draft.members };
        if (draft.id) await api.updateGroup(draft.id, fields);
        else await api.createGroup(pid, fields.name, { concept: fields.concept, member_ids: fields.member_ids });
      }
      setDraft(null); app.bump(); app.toast(es ? "Reparto guardado" : "Cast saved", "ok");
    } catch (err) { setError((err as Error).message); }
    finally { setBusy(false); }
  };
  return <section className="project-cast dashboard-section">
    <div className="dashboard-section-head"><h2>{es ? "Reparto" : "Cast"}</h2><span className="spacer" />
      <button className="btn sm" disabled={busy} onClick={() => start("character")}><Plus size={15} />{es ? "Personaje" : "Character"}</button>
      <button className="btn sm" disabled={busy} onClick={() => start("group")}><Plus size={15} />{es ? "Grupo" : "Group"}</button>
      <button className="btn sm ghost" onClick={() => app.go("cast")}>{es ? "Gestionar reparto" : "Manage cast"}</button></div>
    {(characters.error || groups.error) && <p role="alert">{characters.error || groups.error}<button className="btn sm" onClick={() => { characters.reload(); groups.reload(); }}>{es ? "Reintentar" : "Retry"}</button></p>}
    <div className="project-cast-list">
      {chars.map(char => <button key={char.id} className="project-cast-person" onClick={() => editCharacter(char)}>
        {char.canonical_asset_id ? <img src={thumbUrl({ id: char.canonical_asset_id, kind: "image", thumb_path: "x" })} alt="" /> : <span className="cast-avatar"><Users size={20} /></span>}
        <span><strong>{char.name}</strong><small>{char.role || (es ? "Editar personaje" : "Edit character")}</small></span></button>)}
      {(groups.data?.items || []).map(group => <button key={group.id} className="project-cast-person" onClick={() => editGroup(group)}><span className="cast-avatar"><Users size={22} /></span><span><strong>{group.name}</strong><small>{group.member_ids.length} {es ? "miembros" : "members"}</small></span></button>)}
      {!chars.length && !characters.loading && <p className="hint">{es ? "Añade un personaje o un grupo aquí. Sus referencias estarán disponibles en todo el proyecto." : "Add a character or group here. Their references are available across the project."}</p>}
    </div>
    {draft && <form className="dashboard-cast-editor" onSubmit={event => { event.preventDefault(); save(); }}>
      <div className="row"><h3>{draft.id ? (es ? "Editar" : "Edit") : (es ? "Nuevo" : "New")} {draft.kind === "character" ? (es ? "personaje" : "character") : (es ? "grupo" : "group")}</h3><span className="spacer" /><button type="button" className="btn icon ghost" aria-label={es ? "Cerrar editor de reparto" : "Close cast editor"} disabled={busy} onClick={() => setDraft(null)}><X size={17} /></button></div>
      <div className="dashboard-cast-fields"><label className="field">{es ? "Nombre" : "Name"}<input autoFocus required value={draft.name} disabled={busy} onChange={event => setDraft({ ...draft, name: event.target.value })} /></label>
        <label className="field">{draft.kind === "character" ? (es ? "Aspecto y descripción" : "Appearance and description") : (es ? "Concepto del grupo" : "Group concept")}<textarea rows={3} value={draft.prompt} disabled={busy} onChange={event => setDraft({ ...draft, prompt: event.target.value })} /></label></div>
      {draft.kind === "character" ? <div className="row"><button type="button" className="btn" disabled={busy} onClick={() => setPicking(true)}><ImagePlus size={16} />{es ? "Elegir referencia" : "Choose reference"}</button>{draft.ref && <><img className="cast-draft-reference" src={thumbUrl({ id: draft.ref, kind: "image", thumb_path: "x" })} alt={es ? "Referencia elegida" : "Selected reference"} /><button type="button" className="btn sm ghost" disabled={busy} onClick={() => setDraft({ ...draft, ref: null })}>{es ? "Quitar" : "Remove"}</button></>}</div>
        : <fieldset className="cast-members"><legend>{es ? "Miembros" : "Members"}</legend>{chars.filter(char => !char.element || char.element === "character").map(char => <label key={char.id} className="check"><input type="checkbox" disabled={busy} checked={draft.members.includes(char.id)} onChange={event => setDraft({ ...draft, members: event.target.checked ? [...draft.members, char.id] : draft.members.filter(id => id !== char.id) })} />{char.name}</label>)}{!chars.length && <span className="hint">{es ? "Puedes crear el grupo ahora y añadir personajes después." : "Create the group now and add characters later."}</span>}</fieldset>}
      {error && <p className="err-text" role="alert">{error}</p>}
      <div className="row"><button type="submit" className="btn primary" disabled={busy || !draft.name.trim()}>{busy && <Loader2 size={15} className="spin" />}{es ? "Guardar en el proyecto" : "Save to project"}</button><button type="button" className="btn ghost" disabled={busy} onClick={() => setDraft(null)}>{es ? "Cancelar" : "Cancel"}</button></div>
    </form>}
    {picking && draft && <AssetPicker projectId={pid} kind="image" title={es ? "Referencia del personaje" : "Character reference"} onClose={() => setPicking(false)} onPick={asset => { setDraft({ ...draft, ref: asset.id }); setPicking(false); }} />}
  </section>;
}
