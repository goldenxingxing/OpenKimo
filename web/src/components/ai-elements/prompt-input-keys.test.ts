import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { isNewlineShortcut } from "./prompt-input-keys.ts";

const press = (
  modifiers: Partial<{ metaKey: boolean; ctrlKey: boolean; altKey: boolean; shiftKey: boolean }>,
  key = "Enter",
) => ({ key, metaKey: false, ctrlKey: false, altKey: false, shiftKey: false, ...modifiers });

describe("isNewlineShortcut", () => {
  it("is Cmd+Enter on macOS, not Ctrl+Enter", () => {
    assert.equal(isNewlineShortcut(press({ metaKey: true }), true), true);
    assert.equal(isNewlineShortcut(press({ ctrlKey: true }), true), false);
  });

  it("is Ctrl+Enter elsewhere, not the Windows key", () => {
    assert.equal(isNewlineShortcut(press({ ctrlKey: true }), false), true);
    assert.equal(isNewlineShortcut(press({ metaKey: true }), false), false);
  });

  it("leaves plain Enter to submit and Shift+Enter to the browser", () => {
    for (const isMac of [true, false]) {
      assert.equal(isNewlineShortcut(press({}), isMac), false);
      assert.equal(isNewlineShortcut(press({ shiftKey: true }), isMac), false);
    }
  });

  it("does not claim other keys or extra modifiers", () => {
    assert.equal(isNewlineShortcut(press({ metaKey: true }, "k"), true), false);
    assert.equal(isNewlineShortcut(press({ metaKey: true, altKey: true }), true), false);
    assert.equal(isNewlineShortcut(press({ metaKey: true, shiftKey: true }), true), false);
    assert.equal(isNewlineShortcut(press({ ctrlKey: true, metaKey: true }), false), false);
  });
});
