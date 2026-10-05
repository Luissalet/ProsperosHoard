import { useEffect, useRef, useState } from "react";
import { Download, FolderInput, Images, RotateCcw, Search, Trash2, Upload } from "lucide-react";
import { api, type Asset, type TrashItem } from "../api";
import { useT } from "../i18n";
import { AssetTile, ConfirmButton, Empty, Modal, useApp, useDebounced } from "../components/ui";
import { useDeleteAssets } from "../components/useDeleteAssets";
import { LinkDownload } from "../components/LinkDownload";
import { assetName, kindName, sourceName } from "../messages";

export function LibraryView() {
  const { t, lang } = useT();
  const app = useApp();
  const pid = app.projectId!;
  const [query, setQuery] = useState("");
  const [kind, setKind] = useState("");
  const [source, setSource] = useState("");
  const [fav, setFav] = useState(false);
  const [minRating, setMinRating] = useState(0);
  const [items, setItems] = useState<Asset[]>([]);
  const [next, setNext] = useState<number | null>(null);
  const [focus, setFocus] = useState(0);
  const [loading, setLoading] = useState(true);
  const [loadError, setLoadError] = useState("");
  const [reload, setReload] = useState(0);
  const request = useRef(0);
  const [pathOpen, setPathOpen] = useState(false);
  const [linkOpen, setLinkOpen] = useState(false);
  const [path, setPath] = useState("");
  const fileRef = useRef<HTMLInputElement>(null);
  const [picked, setPicked] = useState<string[] | null>(null);
  const [showTrash, setShowTrash] = useState(false);
  const [trash, setTrash] = useState<TrashItem[]>([]);
  const del = useDeleteAssets();
  const loadTrash = () => api.trash(pid).then((r) => setTrash(r.items)).catch((e) => app.toast(e.message, "bad"));
  useEffect(() => { if (showTrash) loadTrash(); }, [showTrash, pid, app.dataVersion]);
  const dq = useDebounced(query, 250);

  const params = { query: dq, kind, source, favourite: fav || undefined, min_rating: minRating || undefined, limit: 60 };
  const hasFilters = !!query || !!kind || !!source || fav || minRating > 0;
  const clearFilters = () => { setQuery(""); setKind(""); setSource(""); setFav(false); setMinRating(0); setPicked(null); };
  useEffect(() => {
    const id = ++request.current;
    setLoading(true); setLoadError(""); setNext(null); setPicked(null);
    api.assets(pid, params).then((r) => {
      if (request.current !== id) return;
      setItems(r.items); setNext(r.next_offset); setFocus(0);
    }).catch((e) => { if (request.current === id) setLoadError(e.message); })
      .finally(() => { if (request.current === id) setLoading(false); });
    return () => { if (request.current === id) request.current++; };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pid, dq, kind, source, fav, minRating, app.dataVersion, reload]);

  const more = async () => {
    if (next === null || loading || query !== dq) return;
    const id = request.current;
    setLoading(true); setLoadError("");
    try {
      const r = await api.assets(pid, { ...params, offset: next });
      if (request.current !== id) return;
      setItems((xs) => [...xs, ...r.items]); setNext(r.next_offset);
    } catch (e) { if (request.current === id) setLoadError((e as Error).message); }
    finally { if (request.current === id) setLoading(false); }
  };

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const el = e.target as HTMLElement;
      if (el && (el.tagName === "INPUT" || el.tagName === "TEXTAREA" || el.tagName === "SELECT")) return;
      if (document.querySelector(".overlay, .modal-back")) return;
      if (e.key === "j" || e.key === "J") setFocus((f) => Math.min(items.length - 1, f + 1));
      if (e.key === "k" || e.key === "K") setFocus((f) => Math.max(0, f - 1));
      if (e.key === "Enter" && items[focus]) app.openAsset(items[focus].id, items.map((x) => x.id));
      if (e.key === "Delete" && picked && picked.length) { del.remove(picked).then(() => setPicked([])); }
      if ((e.key === "f" || e.key === "F") && items[focus]) {
        const a = items[focus];
        api.updateAsset(a.id, { favourite: !a.favourite }).then((u) => setItems((xs) => xs.map((x) => (x.id === u.id ? u : x))));
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [items, focus, app, picked, del]);

  useEffect(() => {
    document.querySelector(".tile.focused")?.scrollIntoView({ block: "nearest" });
  }, [focus]);

  const upload = async (files: FileList | null) => {
    if (!files) return;
    for (const f of Array.from(files)) {
      try {
        await api.upload(pid, f);
        app.toast(`${t("saved")}: ${f.name}`, "ok");
      } catch (e) {
        app.toast(`${f.name}: ${(e as Error).message}`, "bad");
      }
    }
    app.bump();
  };

  const importPath = async () => {
    try {
      const a = await api.importPath(pid, path);
      app.toast(`${t("saved")}: ${a.name}`, "ok");
      setPathOpen(false);
      setPath("");
      app.bump();
    } catch (e) {
      app.toast((e as Error).message, "bad");
    }
  };

  return (
    <div onDragOver={(e) => e.preventDefault()} onDrop={(e) => { if (e.dataTransfer.files.length) { e.preventDefault(); upload(e.dataTransfer.files); } }}>
      <div className="page-head">
        <div><h1>{t("libraryTitle")}</h1></div>
        <div className="actions">
          {picked && picked.length > 0 && (
            <button className="btn danger" onClick={async () => { await del.remove(picked); setPicked([]); }}>
              <Trash2 size={16} /> {t("deleteSelected", { n: picked.length })}</button>
          )}
          <button className={`btn${picked ? " primary" : ""}`} onClick={() => { setPicked(picked ? null : []); setShowTrash(false); }}>
            {picked ? t("selectDone") : t("selectMode")}</button>
          <button className={`btn${showTrash ? " primary" : ""}`} onClick={() => { setShowTrash(!showTrash); setPicked(null); }}>
            <Trash2 size={16} /> {t("trashTitle")}</button>
          <button className="btn" onClick={() => setLinkOpen(true)}><Download size={16} /> {t("dlTitle")}</button>
          <button className="btn" onClick={() => setPathOpen(true)}><FolderInput size={16} /> {t("importPath")}</button>
          <button className="btn primary" onClick={() => fileRef.current?.click()}><Upload size={16} /> {t("upload")}</button>
          <input ref={fileRef} type="file" multiple hidden onChange={(e) => upload(e.target.files)}
            accept=".png,.jpg,.jpeg,.webp,.bmp,.gif,.mp3,.wav,.flac,.ogg,.m4a,.mp4,.mov,.webm,.mkv,.lrc,.txt,.ttf,.otf" />
        </div>
      </div>
      <div className="filters">
        <div className="search">
          <Search size={16} />
          <input id="library-search" aria-label={t("search")} value={query} onChange={(e) => setQuery(e.target.value)} placeholder={t("search")} />
        </div>
        <select aria-label={t("kindAll")} value={kind} onChange={(e) => setKind(e.target.value)}>
          <option value="">{t("kindAll")}</option>
          {["image", "video", "audio", "lyrics", "font"].map((k) => <option key={k} value={k}>{kindName(k, lang)}</option>)}
        </select>
        <select aria-label={t("sourceAll")} value={source} onChange={(e) => setSource(e.target.value)}>
          <option value="">{t("sourceAll")}</option>
          {["generated", "rendered", "import", "derived"].map((k) => <option key={k} value={k}>{sourceName(k, lang)}</option>)}
        </select>
        <select value={minRating} onChange={(e) => setMinRating(Number(e.target.value))} aria-label={t("minRating")}>
          <option value={0}>{t("minRating")}</option>
          {[1, 2, 3, 4, 5].map((n) => <option key={n} value={n}>{"★".repeat(n)}</option>)}
        </select>
        <label className="check"><input type="checkbox" checked={fav} onChange={(e) => setFav(e.target.checked)} /> {t("favouritesOnly")}</label>
        {hasFilters && <button className="btn sm" onClick={clearFilters}>{t("clearFilters")}</button>}
        <span className="muted small" style={{ marginLeft: "auto" }}>{items.length}{next !== null ? "+" : ""}</span>
      </div>
      {del.bar}
      {!showTrash && loading && <p className="muted" role="status">{t("loading")}</p>}
      {!showTrash && loadError && <div className="row wrap" role="alert"><span>{loadError}</span><button className="btn" onClick={() => setReload((n) => n + 1)}>{t("recheck")}</button></div>}
      {showTrash ? (
        <div className="stack">
          <div className="row">
            <span className="muted small grow">{trash.length} · {t("trashTitle")}</span>
            {trash.length > 0 && (
              <ConfirmButton className="btn sm danger" onConfirm={async () => {
                try { const r = await api.emptyTrash(pid); app.toast(t("trashPurged", { n: r.purged.length }), "ok"); loadTrash(); }
                catch (e) { app.toast((e as Error).message, "bad"); }
              }}><Trash2 size={13} /> {t("trashEmptyAll")}</ConfirmButton>
            )}
          </div>
          {trash.length === 0 ? <Empty icon={<Trash2 size={30} />} text={t("trashEmpty")} /> : (
            <div className="masonry">
              {trash.map((x) => (
                <div key={x.id} className="tile" style={{ cursor: "default" }}>
                  {x.thumb ? <img src={`/api/trash/${x.id}/thumb`} alt="" loading="lazy" style={{ opacity: 0.7 }} />
                    : <div className="media-icon"><Trash2 size={22} /></div>}
                  <div className="tile-meta" style={{ opacity: 1 }}>
                    <span className="ellipsis grow">{assetName(x.name, lang) || x.id}</span>
                    <button className="btn sm" onClick={async () => {
                      try { await api.restoreAsset(x.id); app.toast(t("restored"), "ok"); app.bump(); loadTrash(); }
                      catch (e) { app.toast((e as Error).message, "bad"); }
                    }}><RotateCcw size={12} /> {t("trashRestore")}</button>
                  </div>
                </div>
              ))}
            </div>
          )}
        </div>
      ) : loading || loadError ? null : items.length === 0 ? <Empty icon={<Images size={34} />} text={t(hasFilters ? "libraryNoMatches" : "noAssets")} /> : (
        <>
          <div className="masonry">
            {items.map((a, i) => (
              <AssetTile key={a.id} asset={a} focused={i === focus} selecting={!!picked} selected={picked?.includes(a.id)}
                onClick={() => { setFocus(i); if (picked) setPicked(picked.includes(a.id) ? picked.filter((x) => x !== a.id) : [...picked, a.id]);
                  else app.openAsset(a.id, items.map((x) => x.id)); }} />
            ))}
          </div>
          {next !== null && <div style={{ textAlign: "center", marginTop: 16 }}><button className="btn" disabled={loading || query !== dq} onClick={more}>{t("loadMore")}</button></div>}
        </>
      )}
      {linkOpen && (
        <Modal title={t("dlTitle")} onClose={() => setLinkOpen(false)} wide>
          <p className="small muted">{t("dlHint")}</p>
          <LinkDownload projectId={pid} onDone={() => setLinkOpen(false)} />
        </Modal>
      )}
      {pathOpen && (
        <Modal title={t("importPath")} onClose={() => setPathOpen(false)}
          footer={<><button className="btn ghost" onClick={() => setPathOpen(false)}>{t("cancel")}</button>
            <button className="btn primary" onClick={importPath} disabled={!path.trim()}>{t("create")}</button></>}>
          <label className="field">{t("importPath")}
            <input value={path} onChange={(e) => setPath(e.target.value)} placeholder={t("pathPlaceholder")} className="mono" autoFocus
              onKeyDown={(e) => e.key === "Enter" && importPath()} />
            <span className="hint">{t("importRootsHint")}</span>
          </label>
        </Modal>
      )}
    </div>
  );
}
