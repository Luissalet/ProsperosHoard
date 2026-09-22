import { useEffect, useState } from "react";
import { LayoutGrid, Plus, X } from "lucide-react";
import { api, type Asset, type Board } from "../api";
import { useT, type MessageKey } from "../i18n";
import { AssetPicker, AssetTile, ConfirmButton, Empty, useApp, useAsync } from "../components/ui";

const KINDS: [string, MessageKey][] = [["moodboard", "kindMoodboard"], ["storyboard", "kindStoryboard"], ["shotlist", "kindShotlist"]];

export function BoardsView() {
  const { t } = useT();
  const app = useApp();
  const pid = app.projectId!;
  const boards = useAsync(() => api.boards(pid), [pid, app.dataVersion]);
  const [name, setName] = useState("");
  const [kind, setKind] = useState("moodboard");
  const [adding, setAdding] = useState<Board | null>(null);
  const [assetCache, setAssetCache] = useState<Record<string, Asset>>({});

  const list = boards.data?.items || [];
  useEffect(() => {
    const needed = [...new Set(list.flatMap((b) => b.items.map((i) => i.asset_id)))].filter((id) => !assetCache[id]).slice(0, 100);
    if (!needed.length) return;
    Promise.all(needed.map((id) => api.asset(id).catch(() => null))).then((found) => {
      setAssetCache((c) => ({ ...c, ...Object.fromEntries(found.filter(Boolean).map((a) => [a!.id, a!])) }));
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [boards.data]);

  const create = async () => {
    if (!name.trim()) return;
    try {
      await api.createBoard(pid, name.trim(), kind);
      setName("");
      boards.reload();
    } catch (e) {
      app.toast((e as Error).message, "bad");
    }
  };

  const setItems = async (b: Board, items: Board["items"]) => {
    try {
      await api.setBoardItems(b.id, items);
      boards.reload();
    } catch (e) {
      app.toast((e as Error).message, "bad");
    }
  };

  return (
    <>
      <div className="page-head">
        <div><h1>{t("boardsTitle")}</h1><p>{t("noBoards")}</p></div>
        <div className="actions">
          <input value={name} onChange={(e) => setName(e.target.value)} placeholder={t("name")} onKeyDown={(e) => e.key === "Enter" && create()} />
          <select value={kind} onChange={(e) => setKind(e.target.value)}>{KINDS.map(([k, key]) => <option key={k} value={k}>{t(key)}</option>)}</select>
          <button className="btn primary" onClick={create} disabled={!name.trim()}><Plus size={16} /> {t("newBoard")}</button>
        </div>
      </div>
      {list.length === 0 ? <Empty icon={<LayoutGrid size={34} />} text={t("noBoards")} /> : (
        <div className="stack">
          {list.map((b) => (
            <div key={b.id} className="card"
              onDragOver={(e) => e.preventDefault()}
              onDrop={(e) => {
                const id = e.dataTransfer.getData("text/prospero-asset");
                if (id && !b.items.some((i) => i.asset_id === id)) setItems(b, [...b.items, { asset_id: id, note: "" }]);
              }}>
              <h2>{b.name} <span className="pill">{t((KINDS.find(([k]) => k === b.kind)?.[1] || "kindMoodboard"))}</span>
                <span className="muted small" style={{ fontWeight: 400 }}>{b.items.length}</span>
                <div className="card-actions">
                  <button className="btn sm" onClick={() => setAdding(b)}><Plus size={14} /> {t("addToBoard")}</button>
                  <ConfirmButton armedLabel={t("confirmDelete")} onConfirm={async () => { await api.deleteBoard(b.id); boards.reload(); }}>{t("delete")}</ConfirmButton>
                </div>
              </h2>
              {b.items.length === 0 ? <p className="muted">{t("boardEmpty")}</p> : (
                <div className="thumb-grid">
                  {b.items.map((item, i) => assetCache[item.asset_id] && (
                    <div key={item.asset_id} style={{ position: "relative" }}>
                      <AssetTile asset={assetCache[item.asset_id]} square onClick={() => app.openAsset(item.asset_id, b.items.map((x) => x.asset_id))} />
                      <span className="pill badge-dark" style={{ position: "absolute", left: 8, bottom: 8, background: "rgba(10,6,14,.72)", color: "#fff" }}>{i + 1}</span>
                      <button className="btn sm icon" style={{ position: "absolute", top: 6, right: 6 }} title={t("remove")}
                        onClick={() => setItems(b, b.items.filter((x) => x.asset_id !== item.asset_id))}><X size={13} /></button>
                    </div>
                  ))}
                </div>
              )}
            </div>
          ))}
        </div>
      )}
      {adding && <AssetPicker projectId={pid} onClose={() => setAdding(null)} onPick={(a) => {
        if (!adding.items.some((i) => i.asset_id === a.id)) setItems(adding, [...adding.items, { asset_id: a.id, note: "" }]);
        setAdding(null);
      }} />}
    </>
  );
}
