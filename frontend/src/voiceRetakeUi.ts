/** Pure helpers for the Speak-tab word retake UI (unit-tested). */

export type WordTiming = { start_s: number; end_s: number; word: string };

export type RetakeReport = {
  cut_start_s: number;
  cut_end_s: number;
  cut_by?: { start?: string; end?: string } | string[] | Record<string, string>;
  replaced?: string;
  new_text?: string;
};

export function flattenTranscriptWords(segments: { words?: WordTiming[] }[]): WordTiming[] {
  const out: WordTiming[] = [];
  for (const seg of segments || []) {
    for (const w of seg.words || []) {
      const word = String(w.word || "").trim();
      if (!word) continue;
      out.push({ start_s: Number(w.start_s), end_s: Number(w.end_s), word });
    }
  }
  return out;
}

export function formatCutBy(
  report: RetakeReport | null | undefined,
  labels: { start: string; end: string },
): string | null {
  if (!report?.cut_by) return null;
  const cb = report.cut_by;
  if (Array.isArray(cb)) {
    const [start, end] = cb;
    if (!start && !end) return null;
    return `${labels.start}: ${start || "—"} · ${labels.end}: ${end || "—"}`;
  }
  if (typeof cb === "object") {
    const start = "start" in cb ? cb.start : (cb as Record<string, string>).left ?? (cb as Record<string, string>).start;
    const end = "end" in cb ? cb.end : (cb as Record<string, string>).right ?? (cb as Record<string, string>).end;
    if (!start && !end) return null;
    return `${labels.start}: ${start || "—"} · ${labels.end}: ${end || "—"}`;
  }
  return String(cb);
}

export function wordRangeAfterClick(
  index: number,
  selAnchor: number | null,
  selFirst: number | null,
  selLast: number | null,
): { selAnchor: number | null; selFirst: number; selLast: number } {
  if (selAnchor === null || (selFirst !== null && selLast !== null && selFirst !== selLast)) {
    return { selAnchor: index, selFirst: index, selLast: index };
  }
  return {
    selAnchor: null,
    selFirst: Math.min(selAnchor, index),
    selLast: Math.max(selAnchor, index),
  };
}
