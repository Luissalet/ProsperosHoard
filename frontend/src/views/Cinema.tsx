// The film-language guide: shot sizes, angles, camera moves, lenses,
// light and composition, each with a little (often animated) picture, what
// it is, when to use it and the words that ask a model for it.
import { useMemo, useState } from "react";
import { Aperture, Copy, Search, Wand2 } from "lucide-react";
import type { CinemaCategory, CinemaEntry } from "../api";
import { useT } from "../i18n";
import { Empty, useApp } from "../components/ui";
import { CinemaArt } from "../components/CinemaArt";
import { searchCinema, useCinemaGuide } from "../components/Slash";

export function CinemaView() {
  const { t, lang } = useT();
  const app = useApp();
  const guide = useCinemaGuide();
  const [cat, setCat] = useState<CinemaCategory | "all">(() => {
    try { return (sessionStorage.getItem("prospero.cinemaTab") as CinemaCategory) || "all"; } catch { return "all"; }
  });
  const [query, setQuery] = useState("");
  const L = lang === "es" ? "es" : "en";
  const pickCat = (c: CinemaCategory | "all") => { setCat(c); try { sessionStorage.setItem("prospero.cinemaTab", c); } catch { /* ignore */ } };

  const items: CinemaEntry[] = useMemo(() => {
    if (!guide) return [];
    const found = query.trim() ? searchCinema(guide, query) : guide.items;
    return cat === "all" ? found : found.filter((e) => e.category === cat);
  }, [guide, query, cat]);

  const copy = async (e: CinemaEntry) => {
    try { await navigator.clipboard.writeText(e.prompt); app.toast(t("cineCopied"), "ok"); } catch { app.toast(e.prompt, "info"); }
  };
  const sendToGenerate = (e: CinemaEntry) => {
    const pid = app.projectId;
    if (!pid) { app.go("projects"); return; }
    try {
      const key = `prospero.prompt.${pid}`;
      const cur = sessionStorage.getItem(key) || "";
      sessionStorage.setItem(key, cur.trim() ? `${cur.trim().replace(/[,.]$/, "")}, ${e.prompt}` : e.prompt);
    } catch { /* ignore */ }
    app.go("generate");
  };

  return (
    <>
      <div className="page-head">
        <div><h1>{t("cineTitle")}</h1><p>{t("cineLead")}</p></div>
        <div className="actions">
          <div className="cine-search"><Search size={14} /><input value={query} onChange={(e) => setQuery(e.target.value)} placeholder={t("cineSearch")} /></div>
        </div>
      </div>
      <div className="cine-tabs">
        <button className={cat === "all" ? "on" : ""} onClick={() => pickCat("all")}>{t("cineAll")}</button>
        {guide?.categories.map((c) => (
          <button key={c.id} className={cat === c.id ? "on" : ""} onClick={() => pickCat(c.id)}>{c.name[L]}</button>
        ))}
      </div>
      <p className="small muted cine-tip">{t("cineSlashTip")}</p>
      {guide && items.length === 0 ? <Empty icon={<Aperture size={34} />} text={t("cineNone")} /> : (
        <div className="cine-grid">
          {items.map((e) => (
            <article key={e.id} className="cine-card">
              <div className="cine-pic"><CinemaArt id={e.id} category={e.category} /></div>
              <div className="cine-body">
                <div className="row" style={{ gap: 6, alignItems: "baseline" }}>
                  <h3>{e.name[L]}</h3>
                  {L === "es" && <span className="small muted">{e.name.en}</span>}
                </div>
                <p>{e.what[L]}</p>
                <p className="small"><strong>{t("cineWhen")}</strong> {e.when[L]}</p>
                <div className="cine-prompt mono" title={t("cinePromptHint")}>{e.prompt}</div>
                <div className="row" style={{ gap: 6 }}>
                  {e.video_only && <span className="pill">{t("slashClipOnly")}</span>}
                  <span className="grow" />
                  <button className="btn sm ghost" onClick={() => copy(e)}><Copy size={13} /> {t("cineCopy")}</button>
                  <button className="btn sm" onClick={() => sendToGenerate(e)}><Wand2 size={13} /> {t("cineUse")}</button>
                </div>
              </div>
            </article>
          ))}
        </div>
      )}
    </>
  );
}
