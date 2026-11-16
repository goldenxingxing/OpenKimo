import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { shouldClearSubagentRunning } from "./subagent-state.ts";

describe("shouldClearSubagentRunning", () => {
  it("clears on the subagent's own TurnEnd", () => {
    assert.equal(shouldClearSubagentRunning("TurnEnd"), true);
  });

  it("does not clear on any other inner event", () => {
    assert.equal(shouldClearSubagentRunning("ContentPart"), false);
    assert.equal(shouldClearSubagentRunning("ToolCall"), false);
    assert.equal(shouldClearSubagentRunning("ToolCallPart"), false);
    assert.equal(shouldClearSubagentRunning("ToolResult"), false);
    assert.equal(shouldClearSubagentRunning("StepRetry"), false);
    assert.equal(shouldClearSubagentRunning("StatusUpdate"), false);
    assert.equal(shouldClearSubagentRunning("StepBegin"), false);
    assert.equal(shouldClearSubagentRunning("TurnBegin"), false);
  });

  it("does not clear on a nested SubagentEvent — the parent subagent is still running", () => {
    assert.equal(shouldClearSubagentRunning("SubagentEvent"), false);
  });
});
