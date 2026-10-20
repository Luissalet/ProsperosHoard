import assert from "node:assert/strict";
import { describe, it } from "node:test";
import {
  closeOpenSlashMenus,
  dialogOwnsEscape,
  registerSlashCloser,
} from "../src/escapePriority.ts";

describe("dialogOwnsEscape", () => {
  it("owns Escape when no slash menu is open", () => {
    assert.equal(dialogOwnsEscape({ querySelector: () => null }), true);
    assert.equal(dialogOwnsEscape(null), true);
  });

  it("defers Escape while a slash menu is open inside the dialog", () => {
    const menu = {};
    assert.equal(
      dialogOwnsEscape({ querySelector: (sel) => (sel === ".slash-menu" ? menu : null) }),
      false,
    );
  });
});

describe("closeOpenSlashMenus", () => {
  it("closes registered menus even when focus is elsewhere", () => {
    let closed = 0;
    const unregister = registerSlashCloser(() => {
      closed += 1;
    });
    closeOpenSlashMenus();
    assert.equal(closed, 1);
    unregister();
    closeOpenSlashMenus();
    assert.equal(closed, 1);
  });

  it("dialog Escape path: defer + close leaves a second Escape for the modal", () => {
    const menu = {};
    const root = { querySelector: (sel) => (sel === ".slash-menu" ? menu : null) };
    let menuOpen = true;
    const unregister = registerSlashCloser(() => {
      menuOpen = false;
    });

    assert.equal(dialogOwnsEscape(root), false);
    closeOpenSlashMenus();
    assert.equal(menuOpen, false);

    // After close, DOM would drop .slash-menu; simulate that.
    const empty = { querySelector: () => null };
    assert.equal(dialogOwnsEscape(empty), true);
    unregister();
  });
});
