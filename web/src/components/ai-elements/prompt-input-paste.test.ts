import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  attachmentKind,
  deletionRange,
  expandPastes,
  findAllTokens,
  findFileTokens,
  findTokens,
  makeFileToken,
  makeToken,
  markAttachments,
  nameAttachments,
  nextPasteId,
  referencedIds,
  shouldUsePlaceholder,
  splitPastedBlocks,
  tokenAtCaret,
} from "./prompt-input-paste.ts";

const zh = {
  noun: "粘贴",
  lines: (n: number) => `${n} 行`,
  chars: (n: number) => `${n} 字`,
};
const en = {
  noun: "Pasted",
  lines: (n: number) => `${n} lines`,
  chars: (n: number) => `${n} chars`,
};

describe("when a paste becomes a placeholder", () => {
  it("anything over fifty characters, counted as characters", () => {
    assert.equal(shouldUsePlaceholder("一".repeat(50)), false);
    assert.equal(shouldUsePlaceholder("一".repeat(51)), true);
    assert.equal(shouldUsePlaceholder("😀".repeat(50)), false);
  });

  it("labels several lines by lines and one line by characters", () => {
    assert.equal(makeToken(1, "a\nb\nc\n", zh), "⟦粘贴 1 · 3 行⟧");
    assert.equal(makeToken(2, "x".repeat(60), en), "⟦Pasted 2 · 60 chars⟧");
  });

  it("takes the smallest free id", () => {
    assert.equal(nextPasteId([]), 1);
    assert.equal(nextPasteId([1, 2, 4]), 3);
  });
});

describe("sending", () => {
  const stored = new Map([
    [3, "第三段"],
    [1, "first"],
  ]);

  it("puts the full text back where each placeholder sits, numbered in reading order", () => {
    const text = "把 ⟦粘贴 3 · 5 字⟧ 翻译，再和 ⟦Pasted 1 · 2 lines⟧ 对比";
    assert.equal(
      expandPastes(text, (id) => stored.get(id)),
      '把 <pasted_text index="1">第三段</pasted_text> 翻译，再和 <pasted_text index="2">first</pasted_text> 对比',
    );
  });

  it("repeats the same index for a placeholder used twice, and leaves unknown ones alone", () => {
    const text = "⟦粘贴 1⟧ ⟦粘贴 9⟧ ⟦粘贴 1⟧";
    assert.equal(
      expandPastes(text, (id) => stored.get(id)),
      '<pasted_text index="1">first</pasted_text> ⟦粘贴 9⟧ <pasted_text index="1">first</pasted_text>',
    );
  });

  it("lists the ids a message uses", () => {
    assert.deepEqual([...referencedIds("a ⟦粘贴 2 · 3 行⟧ b ⟦Pasted 5⟧")], [2, 5]);
  });

  it("does not take ordinary brackets for a placeholder", () => {
    assert.deepEqual(findTokens("[Pasted 1] ⟦other 1⟧ ⟦粘贴 x⟧"), []);
  });
});

describe("deleting a placeholder whole", () => {
  const text = "ab⟦粘贴 1 · 3 行⟧cd";
  const start = 2;
  const end = start + "⟦粘贴 1 · 3 行⟧".length;

  it("Backspace right after it, or from inside it", () => {
    assert.deepEqual(deletionRange(text, end, end, "Backspace"), { start, end });
    assert.deepEqual(deletionRange(text, start + 3, start + 3, "Backspace"), { start, end });
    assert.equal(deletionRange(text, start, start, "Backspace"), null);
  });

  it("Delete right before it, not right after", () => {
    assert.deepEqual(deletionRange(text, start, start, "Delete"), { start, end });
    assert.equal(deletionRange(text, end, end, "Delete"), null);
  });

  it("widens a selection that clips it", () => {
    assert.deepEqual(deletionRange(text, 1, start + 2, "Backspace"), { start: 1, end });
    assert.equal(deletionRange(text, 0, 2, "Backspace"), null);
  });

  it("finds the placeholder the caret touches", () => {
    assert.equal(tokenAtCaret(text, end)?.id, 1);
    assert.equal(tokenAtCaret(text, 0), null);
  });
});

describe("a sent message in the history", () => {
  it("folds each pasted block back out of the text around it", () => {
    const message = '请翻译 <pasted_text index="1">line 1\nline 2</pasted_text> 谢谢';
    assert.deepEqual(splitPastedBlocks(message), [
      { kind: "text", text: "请翻译 " },
      { kind: "paste", index: 1, content: "line 1\nline 2" },
      { kind: "text", text: " 谢谢" },
    ]);
  });

  it("leaves a message without pastes as one piece", () => {
    assert.deepEqual(splitPastedBlocks("hello"), [{ kind: "text", text: "hello" }]);
  });
});

describe("file placeholders", () => {
  const labels = { image: "图片", file: "文件" };

  it("names the file and its kind, and survives awkward names", () => {
    assert.equal(makeFileToken(1, "image", "shot.png", labels), "⟦图片 1 · shot.png⟧");
    assert.equal(makeFileToken(2, "file", "a⟧b\nc.pdf", labels), "⟦文件 2 · a b c.pdf⟧");
    assert.equal(attachmentKind("image/png"), "image");
    assert.equal(attachmentKind("application/pdf"), "file");
  });

  it("becomes a marker by position among the files, then the stored name", () => {
    const text = "把 ⟦图片 3 · a.png⟧ 和 ⟦File 1 · b.pdf⟧ 对比，⟦文件 9 · gone.txt⟧";
    const positions = new Map([
      [3, 0],
      [1, 1],
    ]);
    const marked = markAttachments(text, (id) => positions.get(id));
    assert.equal(marked, '把 <attachment index="0"/> 和 <attachment index="1"/> 对比，');
    const named = nameAttachments(marked, (i) => ["a_1f2e3d.png", undefined][i]);
    assert.equal(named, '把 <attachment file="a_1f2e3d.png"/> 和  对比，');
  });

  it("is deleted whole like a pasted-text placeholder", () => {
    const text = "x⟦图片 1 · a.png⟧y";
    const end = 1 + "⟦图片 1 · a.png⟧".length;
    assert.deepEqual(deletionRange(text, end, end, "Backspace"), { start: 1, end });
  });

  it("is found alongside pasted text, in order", () => {
    const text = "⟦粘贴 1 · 3 行⟧ then ⟦Image 1 · a.png⟧";
    assert.deepEqual(
      findAllTokens(text).map((token) => token.start),
      [0, text.indexOf("⟦Image")],
    );
    assert.deepEqual(findFileTokens(text).map((token) => token.id), [1]);
  });
});
