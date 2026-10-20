export interface TimingRow {
  id: string; text: string; start_s: number | null; end_s: number | null;
  section?: string | null; needs_review?: boolean;
}

export function rowsFromLyrics(text: string, timed: { text: string; time_s: number | null; end_s: number | null; needs_review?: boolean }[] = []): TimingRow[] {
  let section: string | null = null, cursor = 0;
  const result: TimingRow[] = [];
  for (const raw of text.split(/\r?\n/)) {
    const line = raw.replace(/^(\[\d+:\d+(?:[.:]\d+)?\])+/, '').trim();
    if (!line) continue;
    const heading = line.match(/^\[([^\]]+)\]$/);
    if (heading) { section = heading[1]; continue; }
    const index = timed.findIndex((item, i) => i >= cursor && item.text.trim() === line);
    const match = index >= 0 ? timed[index] : undefined;
    if (match) cursor = index + 1;
    result.push({ id: `line_${result.length}`, text: line, section,
      start_s: match?.time_s ?? null, end_s: match?.end_s ?? null, needs_review: match?.needs_review });
  }
  return result;
}

/** Each tap starts the next line; the final tap ends the last one. */
export function stampNext(rows: TimingRow[], index: number, time: number): { rows: TimingRow[]; next: number; finished: boolean } {
  if (!Number.isFinite(time) || time < 0 || index < 0 || index > rows.length || !rows.length) throw new Error('Invalid timing mark');
  const next = rows.map(row => ({ ...row }));
  if (index > 0) {
    const previous = next[index - 1];
    if (previous.start_s != null && time <= previous.start_s) throw new Error('The next mark must be after the preceding line starts');
    previous.end_s = Math.round(time * 1000) / 1000;
    previous.needs_review = false;
  }
  if (index < next.length) {
    next[index].start_s = Math.round(time * 1000) / 1000;
    next[index].end_s = null;
    next[index].needs_review = false;
  }
  return { rows: next, next: Math.min(index + 1, next.length), finished: index === next.length };
}

export function timingIssue(rows: TimingRow[], duration: number): { index: number; kind: 'incomplete' | 'range' | 'overlap' } | null {
  let end = 0;
  for (let index = 0; index < rows.length; index++) {
    const row = rows[index], a = row.start_s, b = row.end_s;
    if (a == null && b == null) continue;
    if (a == null || b == null) return { index, kind: 'incomplete' };
    if (![a, b].every(Number.isFinite) || a < 0 || b <= a || (duration > 0 && b > duration + .02)) return { index, kind: 'range' };
    if (a < end - .02) return { index, kind: 'overlap' };
    end = b;
  }
  return null;
}
