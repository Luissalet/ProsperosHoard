import { useState } from "react";
import {
  ArrowDown, ArrowUp, Box, Clock, IdCard, ImagePlus, Layers, Loader2, MapPin, Pencil, Sparkles, Trash2, UploadCloud, UserPlus,
  Users, Volume2, Wand2, X,
} from "lucide-react";
import { api, ApiError, fileUrl, thumbUrl, type CastElement, type Character, type Group, type LibraryEntry, type PackInspect } from "../api";
import { useT } from "../i18n";
import { AssetPicker, ConfirmButton, Empty, Modal, useApp, useAsync } from "../components/ui";
import { CharacterKitModal } from "./CharacterKit";

type Draft = { id?: string; element: CastElement; name: string; role: string; bio: string; prompt: string; negative: string; palette: string;
  canonical_asset_id: string | null; crop: string; voice_backend: string; voice_id: string; speed: number };

const emptyDraft: Draft = { element: "character", name: "", role: "", bio: "", prompt: "", negative: "", palette: "#ff4d8d", canonical_asset_id: null,
  crop: "full", voice_backend: "piper", voice_id: "es_ES-davefx-medium", speed: 1 };

export function CastView() {
  const { t } = useT();
  const app = useApp();
  const pid = app.projectId!;
  const chars = useAsync(() => api.characters(pid), [pid, app.dataVersion]);
  const groups = useAsync(() => api.groups(pid), [pid, app.dataVersion]);
  const deleted = useAsync(() => api.deletedCharacters(pid), [pid, app.dataVersion]);
  const [forceDelete, setForceDelete] = useState<string | null>(null);
  const voices = useAsync(() => api.voices(), []);
  const [draft, setDraft] = useState<Draft | null>(null);
  const [picking, setPicking] = useState(false);
  const [speaking, setSpeaking] = useState<string | null>(null);
  const [groupDraft, setGroupDraft] = useState<{ id?: string; name: string; concept: string; members: string[] } | null>(null);
  const [rendering, setRendering] = useState<string | null>(null);
  const [kitCharId, setKitCharId] = useState<string | null>(null);
  const [importing, setImporting] = useState<{ file: File; inspect: PackInspect; rename: string } | null>(null);
  const [inspecting, setInspecting] = useState(false);
  const [library, setLibrary] = useState(false);
  const [tab, setTab] = useState<CastElement>(() => {
    try { return (sessionStorage.getItem("prospero.castTab") as CastElement) || "character"; } catch { return "character"; }
  });
  const pickTab = (k: CastElement) => { setTab(k); try { sessionStorage.setItem("prospero.castTab", k); } catch { /* ignore */ } };

  const all = chars.data?.items || [];
  const byId = Object.fromEntries(all.map((c) => [c.id, c]));
  const elementOf = (c: Character): CastElement => c.element || "character";
  const list = all.filter((c) => elementOf(c) === tab);
  const people = all.filter((c) => elementOf(c) === "character");
  const count = (k: CastElement) => all.filter((c) => elementOf(c) === k).length;
  const newLabel = tab === "location" ? t("newLocation") : tab === "prop" ? t("newProp") : t("newCharacter");
  const newIcon = tab === "location" ? <MapPin size={16} /> : tab === "prop" ? <Box size={16} /> : <UserPlus size={16} />;
  const startNew = () => setDraft({ ...emptyDraft, element: tab });
  const refPrompt = (c: Character) => elementOf(c) === "location"
    ? `@${c.name}, wide establishing shot, empty, no people, no characters, even light`
    : elementOf(c) === "prop"
      ? `@${c.name}, the object alone, centred on a plain light background, soft studio light, product shot`
      : `@${c.name}, full body character portrait, front view, plain studio background, even light`;

  const edit = (c: Character) => setDraft({
    id: c.id, element: elementOf(c), name: c.name, role: c.role || "", bio: c.bio || "", prompt: c.prompt || "", negative: c.negative || "",
    palette: c.palette.join(" "), canonical_asset_id: c.canonical_asset_id, crop: "full", voice_backend: c.voice?.backend || "piper",
    voice_id: c.voice?.voice_id || "es_ES-davefx-medium", speed: c.voice?.speed || 1,
  });

  const save = async () => {
    if (!draft) return;
    const fields = {
      role: draft.role, bio: draft.bio, prompt: draft.prompt, negative: draft.negative,
      palette: draft.palette.split(/[\s,]+/).filter(Boolean),
      canonical_asset_id: draft.canonical_asset_id || undefined,
      ...(draft.canonical_asset_id && draft.crop !== "full" ? { canonical_crop: draft.crop } : {}),
      ...(draft.element === "character" ? { voice: { backend: draft.voice_backend, voice_id: draft.voice_id, speed: draft.speed } } : {}),
      ...(draft.id ? {} : { element: draft.element }),
    } as Partial<Character> & { canonical_crop?: string };
    try {
      if (draft.id) await api.updateCharacter(draft.id, { ...fields, name: draft.name });
      else await api.createCharacter(pid, draft.name, fields);
      setDraft(null);
      app.bump();
    } catch (e) {
      app.toast((e as Error).message, "bad");
    }
  };

  const remove = async (force = false) => {
    if (!draft?.id) return;
    try {
      const r = await api.deleteCharacter(draft.id, force);
      app.toast(t("castDeleted", { name: r.name }), "ok");
      setDraft(null);
      setForceDelete(null);
      app.bump();
    } catch (e) {
      if (e instanceof ApiError && e.code === "in_use") setForceDelete(e.message);
      else app.toast((e as Error).message, "bad");
    }
  };
  const restore = async (c: Character) => {
    try { await api.restoreCharacter(c.id); app.toast(t("castRestored", { name: c.name }), "ok"); app.bump(); }
    catch (e) { app.toast((e as Error).message, "bad"); }
  };
  const removeGroup = async (g: Group) => {
    try { await api.deleteGroup(g.id); app.bump(); } catch (e) { app.toast((e as Error).message, "bad"); }
  };

  const speak = async (c: Character) => {
    setSpeaking(c.id);
    try {
      const a = await api.voice(pid, t("voiceTestLine"), c.id);
      new Audio(fileUrl(a.id)).play().catch(() => undefined);
      app.bump();
    } catch (e) {
      app.toast((e as Error).message, "bad");
    } finally {
      setSpeaking(null);
    }
  };

  const saveGroup = async () => {
    if (!groupDraft) return;
    try {
      if (groupDraft.id) await api.updateGroup(groupDraft.id, { name: groupDraft.name, concept: groupDraft.concept, member_ids: groupDraft.members });
      else await api.createGroup(pid, groupDraft.name, { concept: groupDraft.concept, member_ids: groupDraft.members });
      setGroupDraft(null);
      app.bump();
    } catch (e) {
      app.toast((e as Error).message, "bad");
    }
  };

  const photocards = async (g: Group) => {
    setRendering(g.id);
    try {
      const r = await api.photocardSet(pid, g.id);
      app.toast(`${t("rendered", { name: `${r.front_ids.length * 2} photocards` })}${r.skipped_members?.length ? ` · ${r.skipped_members.join(", ")}?` : ""}`, "ok");
      app.bump();
      app.openAsset(r.contact_sheet_id, [r.contact_sheet_id, ...r.front_ids.flatMap((f, i) => [f, r.back_ids[i]])]);
    } catch (e) {
      app.toast((e as Error).message, "bad");
    } finally {
      setRendering(null);
    }
  };

  const pickPackFile = async (file: File) => {
    setInspecting(true);
    try {
      const inspect = await api.charPackInspect(file);
      setImporting({ file, inspect, rename: "" });
    } catch (e) {
      app.toast((e as Error).message, "bad");
    } finally {
      setInspecting(false);
    }
  };

  const confirmImport = async () => {
    if (!importing) return;
    try {
      const r = await api.charPackImport(pid, importing.file, importing.rename.trim() || undefined);
      app.toast(t("kitPackImported", { name: r.name }), "ok");
      setImporting(null);
      app.bump();
    } catch (e) {
      app.toast((e as Error).message, "bad");
    }
  };

  const moveMember = (i: number, d: number) => {
    if (!groupDraft) return;
    const m = [...groupDraft.members];
    const j = i + d;
    if (j < 0 || j >= m.length) return;
    [m[i], m[j]] = [m[j], m[i]];
    setGroupDraft({ ...groupDraft, members: m });
  };

  return (
    <>
      <div className="page-head">
        <div>
          <h1>{t("castTitle")}</h1>
          <p>{t("castLead")}</p>
        </div>
        <div className="actions">
          <label className="btn" style={{ cursor: inspecting ? "default" : "pointer" }}>
            {inspecting ? <Loader2 size={16} className="spin" /> : <UploadCloud size={16} />} {t("kitImportPack")}
            <input type="file" accept=".hoardchar" hidden disabled={inspecting}
              onChange={(e) => { const f = e.target.files?.[0]; if (f) pickPackFile(f); e.target.value = ""; }} />
          </label>
          <button className="btn" onClick={() => setLibrary(true)}><Layers size={16} /> {t("kitLibraryTitle")}</button>
          <button className="btn" onClick={() => setGroupDraft({ name: "", concept: "", members: people.map((c) => c.id) })}><Users size={16} /> {t("newGroup")}</button>
          <button className="btn primary" onClick={startNew}>{newIcon} {newLabel}</button>
        </div>
      </div>

      <div className="tabs-bar">
        {([["character", t("castTabCharacters")], ["location", t("castTabLocations")], ["prop", t("castTabProps")]] as [CastElement, string][]).map(([k, label]) => (
          <button key={k} className={tab === k ? "on" : ""} onClick={() => pickTab(k)}>
            {label}{count(k) > 0 && <span className="muted small"> · {count(k)}</span>}
          </button>
        ))}
      </div>

      {list.length === 0 ? (
        <Empty icon={tab === "location" ? <MapPin size={34} /> : tab === "prop" ? <Box size={34} /> : <UserPlus size={34} />}
          text={tab === "location" ? t("noLocations") : tab === "prop" ? t("noProps") : t("noCast")}>
          <button className="btn primary" onClick={startNew}>{newIcon} {newLabel}</button>
        </Empty>
      ) : (
        <div className="cast-grid">
          {list.map((c) => (
            <article key={c.id} className="char-card">
              <div className="portrait" style={{ background: `linear-gradient(160deg, ${c.palette[0] || "#ff4d8d"}55, ${c.palette[1] || "#120d18"})` }}>
                {c.canonical_asset_id && (
                  <button className="portrait-preview" onClick={() => app.openAsset(c.canonical_asset_id!)}
                    aria-label={`${t("enlarge")} · ${c.name}`}>
                    <img src={thumbUrl({ id: c.canonical_asset_id, thumb_path: "x", kind: "image" })} alt={c.name} />
                  </button>
                )}
                {!c.canonical_asset_id && (
                  <button className="btn sm primary portrait-cta"
                    title={elementOf(c) === "location" ? t("elementRefHintLocation") : elementOf(c) === "prop" ? t("elementRefHintProp") : t("makeRefHint")}
                    onClick={() => {
                    try { sessionStorage.setItem(`prospero.prompt.${c.project_id}`, refPrompt(c)); } catch { /* ignore */ }
                    app.toast(t("makeRefToast", { name: c.name }), "info");
                    app.go("generate");
                  }}><Wand2 size={14} /> {t("makeRef")}</button>
                )}
                {c.role && <span className="pill badge-dark role" style={{ background: "rgba(10,6,14,.72)", color: "#fff" }}>{c.role}</span>}
              </div>
              <div className="body">
                <div className="row">
                  <div className="grow">
                    <div className="name">{c.name}</div>
                    <div className="muted small">{t("mention")} <code>@{c.name}</code></div>
                  </div>
                  <div className="chips">{c.palette.map((hex) => <span key={hex} className="chip" style={{ background: hex }} title={hex} />)}</div>
                </div>
                {c.prompt && <div className="fragment">{c.prompt}</div>}
                {c.bio && <div className="muted small">{c.bio}</div>}
                {(c.kit?.adapters?.length ?? 0) > 0 && (
                  <div className="row wrap" style={{ gap: 5 }}>
                    {c.kit!.adapters!.filter((a) => a.enabled).map((a) => (
                      <span key={a.id} className="pill accent">{t("kitAdaptersPill", { arch: a.arch })}</span>
                    ))}
                  </div>
                )}
                <div className="row">
                  {elementOf(c) === "character" && (
                    <button className="btn sm" onClick={() => speak(c)} disabled={speaking === c.id}>
                      {speaking === c.id ? <Loader2 size={14} className="spin" /> : <Volume2 size={14} />} {t("voiceTest")}
                    </button>
                  )}
                  <button className="btn sm ghost" onClick={() => setKitCharId(c.id)}><Sparkles size={14} /> {t("kitButton")}</button>
                  <button className="btn sm ghost" onClick={() => edit(c)}><Pencil size={14} /> {t("edit")}</button>
                </div>
              </div>
            </article>
          ))}
        </div>
      )}

      {(deleted.data?.items || []).length > 0 && (
        <details className="card" style={{ marginTop: 16 }}>
          <summary className="small muted" style={{ cursor: "pointer" }}>{t("castDeletedList", { n: deleted.data!.items.length })}</summary>
          <div className="stack" style={{ marginTop: 10, gap: 6 }}>
            {deleted.data!.items.map((c) => (
              <div key={c.id} className="row" style={{ background: "var(--surface-2)", borderRadius: 8, padding: "6px 10px" }}>
                {c.canonical_asset_id
                  ? <img src={thumbUrl({ id: c.canonical_asset_id, thumb_path: "x", kind: "image" })} alt="" style={{ width: 28, height: 28, borderRadius: 6, objectFit: "cover" }} />
                  : c.element === "location" ? <MapPin size={16} /> : c.element === "prop" ? <Box size={16} /> : <UserPlus size={16} />}
                <span className="grow">{c.name} <span className="muted small">{c.element === "location" ? t("elementLocation") : c.element === "prop" ? t("elementProp") : c.role || ""}</span></span>
                <button className="btn sm" onClick={() => restore(c)}>{t("trashRestore")}</button>
              </div>
            ))}
          </div>
        </details>
      )}

      <h2 style={{ margin: "30px 0 12px", fontSize: 18 }}>{t("groups")}</h2>
      {(groups.data?.items || []).length === 0 ? <Empty icon={<Users size={30} />} text={t("noGroups")} /> : (
        <div className="stack">
          {groups.data!.items.map((g) => (
            <div key={g.id} className="card">
              <h2>{g.name}
                <div className="card-actions">
                  <button className="btn sm ghost" onClick={() => setGroupDraft({ id: g.id, name: g.name, concept: g.concept || "", members: g.member_ids })}><Pencil size={14} /> {t("edit")}</button>
                  <ConfirmButton onConfirm={() => removeGroup(g)} className="btn sm ghost danger"><Trash2 size={14} /></ConfirmButton>
                  <button className="btn sm primary" onClick={() => photocards(g)} disabled={rendering === g.id}>
                    {rendering === g.id ? <Loader2 size={14} className="spin" /> : <IdCard size={14} />} {t("photocardSet")}
                  </button>
                </div>
              </h2>
              {g.concept && <p className="muted" style={{ marginTop: -4 }}>{g.concept}</p>}
              <div className="row wrap">
                {g.member_ids.map((id, i) => byId[id] && (
                  <span key={id} className="pill" style={{ padding: "4px 10px 4px 4px", gap: 8 }}>
                    {byId[id].canonical_asset_id
                      ? <img src={thumbUrl({ id: byId[id].canonical_asset_id!, thumb_path: "x", kind: "image" })} alt="" style={{ width: 24, height: 24, borderRadius: "50%", objectFit: "cover" }} />
                      : <span className="chip" style={{ background: byId[id].palette[0], borderRadius: "50%" }} />}
                    {i + 1}. {byId[id].name}
                  </span>
                ))}
              </div>
            </div>
          ))}
        </div>
      )}

      {draft && (
        <Modal title={draft.id ? draft.name : draft.element === "location" ? t("newLocation") : draft.element === "prop" ? t("newProp") : t("newCharacter")} onClose={() => { setDraft(null); setForceDelete(null); }}
          footer={<>
            {draft.id && (forceDelete
              ? <button className="btn danger" title={forceDelete} onClick={() => remove(true)}><Trash2 size={14} /> {t("castDeleteAnyway")}</button>
              : <ConfirmButton onConfirm={() => remove()} className="btn ghost danger"><Trash2 size={14} /> {t("delete")}</ConfirmButton>)}
            <span className="grow" />
            <button className="btn ghost" onClick={() => { setDraft(null); setForceDelete(null); }}>{t("cancel")}</button>
            <button className="btn primary" onClick={save} disabled={!draft.name.trim()}>{t("save")}</button></>}>
          <div className="stack">
            {forceDelete && <p className="small warn-text" style={{ margin: 0 }}>{forceDelete}</p>}
            <div className="grid-2">
              <label className="field">{t("name")}<input value={draft.name} onChange={(e) => setDraft({ ...draft, name: e.target.value })} autoFocus /></label>
              <label className="field">{t("role")}<input value={draft.role} onChange={(e) => setDraft({ ...draft, role: e.target.value })} /></label>
            </div>
            <label className="field">{draft.element === "location" ? t("lookLocation") : draft.element === "prop" ? t("lookProp") : t("lookPrompt")}<textarea value={draft.prompt} onChange={(e) => setDraft({ ...draft, prompt: e.target.value })} /></label>
            <div className="grid-2">
              <label className="field">{t("negative")}<input value={draft.negative} onChange={(e) => setDraft({ ...draft, negative: e.target.value })} /></label>
              <label className="field">{t("palette")} <span className="hint">{t("paletteHint")}</span>
                <input value={draft.palette} onChange={(e) => setDraft({ ...draft, palette: e.target.value })} className="mono" /></label>
            </div>
            <label className="field">{t("bio")}<textarea value={draft.bio} rows={2} onChange={(e) => setDraft({ ...draft, bio: e.target.value })} /></label>
            <div className="field">
              {t("canonical")}
              <div className="drop-slot">
                {draft.canonical_asset_id
                  ? <button className="reference-mini" aria-label={t("enlarge")} onClick={() => app.openAsset(draft.canonical_asset_id!)}>
                      <img src={thumbUrl({ id: draft.canonical_asset_id, thumb_path: "x", kind: "image" })} alt={draft.name} />
                    </button>
                  : <ImagePlus size={22} />}
                <button className="btn sm" onClick={() => setPicking(true)}>{t("pickReference")}</button>
                {draft.canonical_asset_id && <button className="btn sm ghost" onClick={() => setDraft({ ...draft, canonical_asset_id: null })}><X size={14} /></button>}
              </div>
              {draft.element !== "character" && <p className="muted small" style={{ margin: "6px 0 0" }}>{t("elementRefNote")}</p>}
              {draft.canonical_asset_id && draft.element === "character" && (
                <label className="field" style={{ marginTop: 8 }}>{t("canonicalCrop")} <span className="hint">{t("cropHint")}</span>
                  <select value={draft.crop} onChange={(e) => setDraft({ ...draft, crop: e.target.value })}>
                    <option value="full">{t("cropFull")}</option>
                    <option value="left_third">{t("cropLeft")}</option>
                    <option value="middle_third">{t("cropMiddle")}</option>
                    <option value="right_third">{t("cropRight")}</option>
                  </select></label>
              )}
            </div>
            {draft.element === "character" && <>
            <div className="grid-3">
              <label className="field">{t("voiceBackend")}
                <select value={draft.voice_backend} onChange={(e) => setDraft({ ...draft, voice_backend: e.target.value })}>
                  <option value="piper">Piper</option><option value="faustus">Faustus TTS</option>
                </select></label>
              <label className="field">{t("voice")}
                <select value={draft.voice_id} onChange={(e) => setDraft({ ...draft, voice_id: e.target.value })}>
                  {(voices.data?.items || []).map((v) => <option key={v.id} value={v.id}>{v.label}{v.downloaded ? "" : " ↓"}</option>)}
                </select></label>
              <label className="field">{t("speed")} <span className="mono">{draft.speed.toFixed(2)}</span>
                <input type="range" min={0.5} max={2} step={0.05} value={draft.speed} onChange={(e) => setDraft({ ...draft, speed: Number(e.target.value) })} /></label>
            </div>
            <p className="muted small" style={{ margin: 0 }}>{t("noVoiceCloning")}</p>
            </>}
          </div>
        </Modal>
      )}
      {picking && draft && <AssetPicker projectId={pid} onClose={() => setPicking(false)}
        onPick={(a) => { setDraft({ ...draft, canonical_asset_id: a.id, crop: "full" }); setPicking(false); }} />}

      {groupDraft && (
        <Modal title={groupDraft.id ? groupDraft.name : t("newGroup")} onClose={() => setGroupDraft(null)}
          footer={<><button className="btn ghost" onClick={() => setGroupDraft(null)}>{t("cancel")}</button>
            <button className="btn primary" onClick={saveGroup} disabled={!groupDraft.name.trim()}>{t("save")}</button></>}>
          <div className="stack">
            <label className="field">{t("name")}<input value={groupDraft.name} onChange={(e) => setGroupDraft({ ...groupDraft, name: e.target.value })} autoFocus /></label>
            <label className="field">{t("concept")}<textarea value={groupDraft.concept} rows={2} onChange={(e) => setGroupDraft({ ...groupDraft, concept: e.target.value })} /></label>
            <div className="field">{t("members")}
              <div className="stack" style={{ gap: 6 }}>
                {groupDraft.members.map((id, i) => byId[id] && (
                  <div key={id} className="row" style={{ background: "var(--surface-2)", borderRadius: 8, padding: "6px 10px" }}>
                    <span className="mono muted">{i + 1}</span><span className="grow">{byId[id].name}</span>
                    <button className="btn sm icon ghost" onClick={() => moveMember(i, -1)} title={t("moveUp")}><ArrowUp size={14} /></button>
                    <button className="btn sm icon ghost" onClick={() => moveMember(i, 1)} title={t("moveDown")}><ArrowDown size={14} /></button>
                    <button className="btn sm icon ghost" onClick={() => setGroupDraft({ ...groupDraft, members: groupDraft.members.filter((x) => x !== id) })} title={t("remove")}><X size={14} /></button>
                  </div>
                ))}
                <select value="" onChange={(e) => e.target.value && setGroupDraft({ ...groupDraft, members: [...groupDraft.members, e.target.value] })}>
                  <option value="">+ {t("addMember")}</option>
                  {people.filter((c) => !groupDraft.members.includes(c.id)).map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
                </select>
              </div>
            </div>
          </div>
        </Modal>
      )}

      {kitCharId && byId[kitCharId] && <CharacterKitModal character={byId[kitCharId]} onClose={() => setKitCharId(null)} />}

      {importing && (
        <Modal title={t("kitImportPreview")} onClose={() => setImporting(null)}
          footer={<><button className="btn ghost" onClick={() => setImporting(null)}>{t("cancel")}</button>
            <button className="btn primary" onClick={confirmImport}>{t("kitConfirmImportBtn")}</button></>}>
          <div className="stack">
            <div className="row">
              <strong className="grow">{importing.inspect.name}</strong>
              {importing.inspect.role && <span className="pill">{importing.inspect.role}</span>}
            </div>
            {importing.inspect.look && <div className="fragment">{importing.inspect.look}</div>}
            <div className="row wrap small muted">
              {importing.inspect.canonical && <span>{t("canonical")}</span>}
              <span>{t("reference")}: {importing.inspect.references}</span>
              <span>{t("kitTabDataset")}: {importing.inspect.dataset}</span>
              {importing.inspect.adapters.length > 0 && <span>{t("kitAdaptersTitle")}: {importing.inspect.adapters.map((a) => a.arch).join(", ")}</span>}
            </div>
            <label className="field">{t("kitRenameField")}
              <input value={importing.rename} placeholder={importing.inspect.name}
                onChange={(e) => setImporting({ ...importing, rename: e.target.value })} /></label>
          </div>
        </Modal>
      )}

      {library && <LibraryModal projectId={pid} onClose={() => setLibrary(false)} onUsed={() => app.bump()} />}
    </>
  );
}

function LibraryModal({ projectId, onClose, onUsed }: { projectId: string; onClose: () => void; onUsed: () => void }) {
  const { t } = useT();
  const app = useApp();
  const lib = useAsync(() => api.libraryList(projectId), [projectId]);
  const [historyOf, setHistoryOf] = useState<string | null>(null);
  const [history, setHistory] = useState<Awaited<ReturnType<typeof api.libraryHistory>> | null>(null);
  const [casting, setCasting] = useState<LibraryEntry | null>(null);
  const [version, setVersion] = useState<number | undefined>(undefined);

  const openHistory = async (e: LibraryEntry) => {
    setHistoryOf(e.id);
    try { setHistory(await api.libraryHistory(projectId, e.id)); } catch (err) { app.toast((err as Error).message, "bad"); }
  };

  const use = async (e: LibraryEntry) => {
    try {
      const r = await api.libraryUse(projectId, e.id, version);
      app.toast(t("kitLibraryUsed", { name: r.name }), "ok");
      onUsed();
      setCasting(null);
    } catch (err) { app.toast((err as Error).message, "bad"); }
  };

  const del = async (e: LibraryEntry) => {
    try { await api.libraryDelete(projectId, e.id); lib.reload(); } catch (err) { app.toast((err as Error).message, "bad"); }
  };

  const items = lib.data?.items || [];
  return (
    <Modal title={t("kitLibraryTitle")} onClose={onClose} wide>
      <p className="small muted" style={{ marginTop: -6 }}>{t("kitLibraryLead")}</p>
      {items.length === 0 ? <Empty icon={<Layers size={30} />} text={t("kitNoLibraryEntries")} /> : (
        <div className="stack">
          {items.map((e) => (
            <div key={e.id} className="row" style={{ background: "var(--surface-2)", borderRadius: 9, padding: 10, alignItems: "flex-start", gap: 10 }}>
              {e.has_preview ? <img src={api.libraryPreviewUrl(e.id)} alt="" style={{ width: 64, height: 64, borderRadius: 8, objectFit: "cover", flex: "none" }} />
                : <div className="media-icon" style={{ width: 64, height: 64, flex: "none" }}><Layers size={20} /></div>}
              <div className="stack grow" style={{ gap: 4 }}>
                <div className="row"><strong className="grow">{e.name}</strong>{e.role && <span className="pill">{e.role}</span>}</div>
                <span className="small muted">{t("kitLibraryVersionsCount", { n: e.versions })} · v{e.version}
                  {e.adapters.length > 0 && ` · ${e.adapters.join(", ")}`}</span>
                {historyOf === e.id && history && (
                  <ul className="small" style={{ margin: 0, paddingLeft: 18 }}>
                    {[...history.versions].reverse().map((v) => (
                      <li key={v.v}>v{v.v} · {v.at.slice(0, 10)} · {v.changes.join(", ")}{v.note ? ` · ${v.note}` : ""}</li>
                    ))}
                  </ul>
                )}
                {casting?.id === e.id && (
                  <div className="row small" style={{ gap: 6 }}>
                    <select value={version ?? ""} onChange={(ev) => setVersion(ev.target.value ? Number(ev.target.value) : undefined)}>
                      <option value="">{t("kitLibraryVersion", { v: e.version })}</option>
                    </select>
                    <button className="btn sm primary" onClick={() => use(e)}>{t("kitCastIntoProject")}</button>
                  </div>
                )}
              </div>
              <div className="row wrap" style={{ gap: 6, flex: "none" }}>
                <button className="btn sm" onClick={() => setCasting(casting?.id === e.id ? null : e)}><UploadCloud size={14} /> {t("kitCastIntoProject")}</button>
                <button className="btn sm ghost" onClick={() => openHistory(e)}><Clock size={14} /> {t("kitViewHistory")}</button>
                <ConfirmButton onConfirm={() => del(e)} className="btn sm ghost danger"><Trash2 size={14} /></ConfirmButton>
              </div>
            </div>
          ))}
        </div>
      )}
    </Modal>
  );
}
