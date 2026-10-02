// "/" commands in prompt boxes: typing /plano (or /shot, /angulo, /luz,
// /lente, /movimiento, /composicion, or any shot name) opens the film
// language guide right there; picking an entry writes its English terms
// into the prompt. The guide is fetched once and shared.
import { useCallback, useEffect, useMemo, useState, type KeyboardEvent, type RefObject } from "react";
import { api, type CinemaEntry, type CinemaGuide } from "../api";
import { useT } from "../i18n";
import { CinemaArt } from "./CinemaArt";

let guidePromise: Promise<CinemaGuide> | null = null;
export function loadCinemaGuide(): Promise<CinemaGuide> {
  if (!guidePromise) guidePromise = api.cinema().catch((e) => { guidePromise = null; throw e; });
  return guidePromise;
}

export function useCinemaGuide(): CinemaGuide | null {
  const [guide, setGuide] = useState<CinemaGuide | null>(null);
  useEffect(() => { let alive = true; loadCinemaGuide().then((g) => alive && setGuide(g)).catch(() => undefined); return () => { alive = false; }; }, []);
  return guide;
}

export const norm = (s: string) => s.normalize("NFKD").replace(/[̀-ͯ]/g, "").toLowerCase();

/** Client mirror of cinema.search: a category word lists that category,
 * anything else matches names and aliases. */
export function searchCinema(guide: CinemaGuide, query: string): CinemaEntry[] {
  const q = norm(query.trim().replace(/^\//, ""));
  if (!q) return guide.items;
  for (const c of guide.categories) {
    if (c.aliases.some((a) => norm(a) === q) || norm(c.name.en) === q || norm(c.name.es) === q) {
      return guide.items.filter((e) => e.category === c.id);
    }
  }
  const score = (e: CinemaEntry): number => {
    const names = [e.name.es, e.name.en].map(norm);
    if (names.some((n) => n.startsWith(q))) return 0;
    if (names.some((n) => n.split(/[\s(/-]+/).some((w) => w.startsWith(q)))) return 1;
    if (names.some((n) => n.includes(q))) return 2;
    const al = [e.id, ...e.aliases].map(norm);
    if (al.some((a) => a.startsWith(q))) return 3;
    return al.some((a) => a.includes(q)) ? 4 : 9;
  };
  return guide.items.map((e) => [score(e), e] as const).filter(([sc]) => sc < 9).sort((a, b) => a[0] - b[0]).map(([, e]) => e);
}

type SlashState = { start: number; end: number; query: string; index: number };
type Field = HTMLTextAreaElement | HTMLInputElement;

/** Wire into a prompt box: call `update` from onChange, `onKeyDown` first
 * in the box's key handler (it returns true when it used the key), and
 * render `menu` inside a positioned wrapper. */
export function useSlashMenu(value: string, setValue: (v: string) => void, ref: RefObject<Field | null>, opts: { clips?: boolean } = {}) {
  const { lang, t } = useT();
  const guide = useCinemaGuide();
  const [st, setSt] = useState<SlashState | null>(null);

  const update = useCallback((text: string, caret: number | null) => {
    const end = caret ?? text.length;
    const m = /(^|[\s,(])\/([\p{L}\d_ -]{0,24})$/u.exec(text.slice(0, end));
    if (!m || /\s{2}/.test(m[2])) { setSt(null); return; }
    setSt({ start: end - m[2].length - 1, end, query: m[2], index: 0 });
  }, []);

  type Row = { kind: "cat"; id: string; label: string; alias: string } | { kind: "entry"; entry: CinemaEntry };
  const rows: Row[] = useMemo(() => {
    if (!st || !guide) return [];
    if (!st.query.trim()) {
      return guide.categories.map((c) => ({ kind: "cat" as const, id: c.id, label: c.name[lang === "es" ? "es" : "en"], alias: c.aliases[0] }));
    }
    let found = searchCinema(guide, st.query);
    if (!found.length && st.query.includes(" ")) found = searchCinema(guide, st.query.split(" ")[0]);
    return found.slice(0, 14).map((entry) => ({ kind: "entry" as const, entry }));
  }, [st, guide, lang]);

  const close = useCallback(() => setSt(null), []);

  const pick = useCallback((row: Row) => {
    if (!st) return;
    const el = ref.current;
    if (row.kind === "cat") {
      const text = `${value.slice(0, st.start)}/${row.alias}${value.slice(st.end)}`;
      const pos = st.start + row.alias.length + 1;
      setValue(text);
      setSt({ start: st.start, end: pos, query: row.alias, index: 0 });
      requestAnimationFrame(() => { el?.focus(); el?.setSelectionRange(pos, pos); });
      return;
    }
    const before = value.slice(0, st.start).replace(/[\s,]*$/, "");
    const after = value.slice(st.end).replace(/^[\s,]*/, "");
    const ins = row.entry.prompt;
    const text = `${before}${before ? ", " : ""}${ins}${after ? `, ${after}` : ""}`;
    const pos = (before ? before.length + 2 : 0) + ins.length;
    setValue(text);
    setSt(null);
    requestAnimationFrame(() => { el?.focus(); el?.setSelectionRange(pos, pos); });
  }, [st, value, setValue, ref]);

  const onKeyDown = useCallback((e: KeyboardEvent): boolean => {
    if (!st || !rows.length) return false;
    if (e.key === "ArrowDown") { e.preventDefault(); setSt({ ...st, index: (st.index + 1) % rows.length }); return true; }
    if (e.key === "ArrowUp") { e.preventDefault(); setSt({ ...st, index: (st.index - 1 + rows.length) % rows.length }); return true; }
    if (e.key === "Enter" || e.key === "Tab") { e.preventDefault(); pick(rows[Math.min(st.index, rows.length - 1)]); return true; }
    if (e.key === "Escape") { e.preventDefault(); e.stopPropagation(); setSt(null); return true; }
    return false;
  }, [st, rows, pick]);

  const L = lang === "es" ? "es" : "en";
  const menu = st && rows.length > 0 ? (
    <div className="mention-menu slash-menu nowheel nodrag" onMouseDown={(e) => e.preventDefault()}>
      <div className="slash-head small muted">{st.query.trim() ? t("slashPick") : t("slashCategories")}</div>
      <div className="slash-list">
        {rows.map((r, i) => r.kind === "cat" ? (
          <button key={r.id} className={i === st.index ? "on" : ""} onClick={() => pick(r)}>
            <span className="slash-cmd mono">/{r.alias}</span><span>{r.label}</span>
          </button>
        ) : (
          <button key={r.entry.id} className={i === st.index ? "on" : ""} onClick={() => pick(r)} title={r.entry.prompt}>
            <span className="slash-art"><CinemaArt id={r.entry.id} category={r.entry.category} /></span>
            <span className="slash-text">
              <strong>{r.entry.name[L]}</strong>
              {r.entry.video_only && !opts.clips && <span className="pill slash-clip">{t("slashClipOnly")}</span>}
              <span className="small muted">{r.entry.what[L]}</span>
            </span>
          </button>
        ))}
      </div>
      <div className="slash-foot small muted">{t("slashHint")}</div>
    </div>
  ) : null;

  return { update, onKeyDown, menu, close, open: !!st && rows.length > 0 };
}
