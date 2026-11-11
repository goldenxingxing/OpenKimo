import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  createDeliveryWindow,
  isDuplicateRender,
} from "./duplicate-delivery.ts";

describe("createDeliveryWindow", () => {
  it("flags an identical frame seen before", () => {
    const w = createDeliveryWindow();
    assert.equal(w.checkAndAdd('{"a":1}'), false);
    assert.equal(w.checkAndAdd('{"b":2}'), false);
    assert.equal(w.checkAndAdd('{"a":1}'), true);
  });

  it("evicts old entries so long-past frames are not flagged forever", () => {
    const w = createDeliveryWindow(2);
    assert.equal(w.checkAndAdd("x"), false);
    assert.equal(w.checkAndAdd("y"), false);
    assert.equal(w.checkAndAdd("z"), false); // evicts "x"
    assert.equal(w.checkAndAdd("x"), false); // forgotten — not a duplicate
  });

  it("clear forgets everything", () => {
    const w = createDeliveryWindow();
    assert.equal(w.checkAndAdd("same"), false);
    w.clear();
    assert.equal(w.checkAndAdd("same"), false);
  });
});

describe("isDuplicateRender", () => {
  it("matches identical assistant text messages", () => {
    assert.equal(
      isDuplicateRender(
        { role: "assistant", variant: "text", content: "hello" },
        { role: "assistant", variant: "text", content: "hello" },
      ),
      true,
    );
  });

  it("matches identical thinking messages", () => {
    assert.equal(
      isDuplicateRender(
        { role: "assistant", variant: "thinking", thinking: "hmm" },
        { role: "assistant", variant: "thinking", thinking: "hmm" },
      ),
      true,
    );
  });

  it("does not match different content, role, or variant", () => {
    const base = { role: "assistant", variant: "text", content: "hello" };
    assert.equal(isDuplicateRender(base, { ...base, content: "world" }), false);
    assert.equal(
      isDuplicateRender(base, { ...base, role: "user" }),
      false,
    );
    assert.equal(
      isDuplicateRender(base, { ...base, variant: "thinking" }),
      false,
    );
  });

  it("does not match when there is no previous message", () => {
    assert.equal(
      isDuplicateRender(undefined, {
        role: "assistant",
        variant: "text",
        content: "hello",
      }),
      false,
    );
  });

  it("does not match on empty incoming content (nothing rendered yet)", () => {
    assert.equal(
      isDuplicateRender(
        { role: "assistant", variant: "text", content: "" },
        { role: "assistant", variant: "text", content: "" },
      ),
      false,
    );
  });
});
