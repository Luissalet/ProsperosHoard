/** Editing history excludes run state: undo never reruns or deletes generated media. */
export function freeNodePosition(preferred: { x: number; y: number }, size: { width: number; height: number },
  occupied: { x: number; y: number; width: number; height: number }[]) {
  const gap = 36;
  const fits = (p: { x: number; y: number }) => occupied.every(r =>
    p.x + size.width + gap <= r.x || p.x >= r.x + r.width + gap || p.y + size.height + gap <= r.y || p.y >= r.y + r.height + gap);
  if (fits(preferred)) return preferred;
  const stepX = size.width + gap, stepY = size.height + gap;
  for (let ring = 1; ring <= 8; ring++) {
    const candidates = [[ring, 0], [-ring, 0], [0, ring], [0, -ring]];
    for (let i = 1; i <= ring; i++) candidates.push([ring, i], [ring, -i], [-ring, i], [-ring, -i], [i, ring], [-i, ring], [i, -ring], [-i, -ring]);
    for (const [dx, dy] of candidates) {
      const point = { x: preferred.x + dx * stepX, y: preferred.y + dy * stepY };
      if (fits(point)) return point;
    }
  }
  return { x: Math.max(...occupied.map(r => r.x + r.width), preferred.x) + gap, y: preferred.y };
}

export class EditHistory<T> {
  private past: T[] = [];
  private future: T[] = [];
  private present: T;
  private group: string | undefined;
  private at = 0;
  private limit: number;

  constructor(initial: T, limit = 50) { this.present = structuredClone(initial); this.limit = limit; }
  get canUndo() { return this.past.length > 0; }
  get canRedo() { return this.future.length > 0; }
  reset(value: T) { this.present = structuredClone(value); this.past = []; this.future = []; this.group = undefined; }
  commit(value: T, group?: string, now = Date.now()) {
    if (JSON.stringify(value) === JSON.stringify(this.present)) return false;
    if (!group || group !== this.group || now - this.at > 700) {
      this.past.push(this.present);
      if (this.past.length > this.limit) this.past.shift();
    }
    this.present = structuredClone(value);
    this.future = [];
    this.group = group;
    this.at = now;
    return true;
  }
  undo(): T | null {
    const value = this.past.pop();
    if (value === undefined) return null;
    this.future.push(this.present); this.present = value; this.group = undefined;
    return structuredClone(value);
  }
  redo(): T | null {
    const value = this.future.pop();
    if (value === undefined) return null;
    this.past.push(this.present); this.present = value; this.group = undefined;
    return structuredClone(value);
  }
}

/** Serialize saves and drain edits made in flight before a dependent action proceeds. */
export class SaveQueue<T> {
  private revision = 0;
  private savedRevision = 0;
  private tail: Promise<boolean> = Promise.resolve(true);
  private read: () => T;
  private write: (value: T) => Promise<void>;
  private onSaved: () => void;
  private onError: (error: unknown) => void;

  constructor(options: { read: () => T; write: (value: T) => Promise<void>; onSaved: () => void; onError: (error: unknown) => void }) {
    this.read = options.read; this.write = options.write; this.onSaved = options.onSaved; this.onError = options.onError;
  }
  get isDirty() { return this.revision !== this.savedRevision; }
  changed() { this.revision++; }
  clean() { this.savedRevision = this.revision; }
  flush(): Promise<boolean> {
    this.tail = this.tail.then(async () => {
      while (this.isDirty) {
        const revision = this.revision;
        const value = this.read();
        try { await this.write(value); }
        catch (error) { this.onError(error); return false; }
        this.savedRevision = revision;
      }
      this.onSaved();
      return true;
    });
    return this.tail;
  }
}
