/** Slash menus own Escape first; closing the dialog would discard the draft. */

type CloseFn = () => void;
const slashClosers = new Set<CloseFn>();

/** Register while a slash menu is open; returns unregister. */
export function registerSlashCloser(close: CloseFn): () => void {
  slashClosers.add(close);
  return () => {
    slashClosers.delete(close);
  };
}

/** Close every registered slash menu (focus-independent). */
export function closeOpenSlashMenus(): void {
  for (const close of [...slashClosers]) close();
}

export function dialogOwnsEscape(root: ParentNode | null | undefined): boolean {
  return !root?.querySelector(".slash-menu");
}
