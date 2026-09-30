import { useEffect, useState } from "react";
import { Trash2, Undo2 } from "lucide-react";
import { api } from "../api";
import { useT } from "../i18n";
import { ConfirmButton, useApp } from "./ui";

// Deleting results: they go to the trash, and for ten seconds an undo bar
// brings them back. An asset in use (a character's canonical image, a
// cover, a board) is refused with the reason and a "detach and delete".
export function useDeleteAssets() {
  const { t } = useT();
  const app = useApp();
  const [last, setLast] = useState<string[] | null>(null);
  const [blocked, setBlocked] = useState<{ ids: string[]; why: string } | null>(null);

  useEffect(() => {
    if (!last) return;
    const h = setTimeout(() => setLast(null), 10000);
    return () => clearTimeout(h);
  }, [last]);

  const remove = async (ids: string[], force = false): Promise<string[]> => {
    if (!ids.length) return [];
    try {
      const r = await api.deleteAssets(ids, force);
      if (r.deleted.length) {
        setLast(r.deleted);
        app.toast(t("deleted", { n: r.deleted.length }), "ok");
        app.bump();
      }
      const inUse = r.failed.filter((f) => f.error === "asset_in_use");
      if (inUse.length) setBlocked({ ids: inUse.map((f) => f.id), why: inUse[0].message });
      else setBlocked(null);
      for (const f of r.failed.filter((x) => x.error !== "asset_in_use")) app.toast(f.message, "bad");
      return r.deleted;
    } catch (e) {
      app.toast((e as Error).message, "bad");
      return [];
    }
  };

  const undo = async () => {
    if (!last) return;
    const ids = last;
    setLast(null);
    for (const id of ids) {
      try { await api.restoreAsset(id); } catch (e) { app.toast((e as Error).message, "bad"); }
    }
    app.toast(t("restored"), "ok");
    app.bump();
  };

  const bar = (last || blocked) ? (
    <div className="row wrap small" style={{ gap: 8, marginBottom: 10 }}>
      {last && (
        <>
          <span className="muted">{t("deleted", { n: last.length })}</span>
          <button className="btn sm" onClick={undo}><Undo2 size={13} /> {t("undo")}</button>
        </>
      )}
      {blocked && (
        <>
          <span className="err-text">{t("deleteInUse", { why: blocked.why })}</span>
          <ConfirmButton className="btn sm danger" onConfirm={() => remove(blocked.ids, true)}>
            <Trash2 size={13} /> {t("deleteForce")}</ConfirmButton>
        </>
      )}
    </div>
  ) : null;

  return { remove, bar };
}
