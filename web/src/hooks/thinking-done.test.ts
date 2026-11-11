import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { endThinking } from "./thinking-done.ts";

describe("ending a thinking block", () => {
  it("stops the block it is given", () => {
    const messages = [
      { id: "think_1", isStreaming: true },
      { id: "tool_1", isStreaming: true },
    ];

    assert.deepEqual(endThinking(messages, "think_1"), [
      { id: "think_1", isStreaming: false },
      { id: "tool_1", isStreaming: true },
    ]);
  });

  it("leaves everything alone when the step has no thinking", () => {
    const messages = [{ id: "tool_1", isStreaming: true }];

    assert.equal(endThinking(messages, null), messages);
  });
});
