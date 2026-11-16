import assert from "node:assert/strict";
import test from "node:test";
import { classifyQuestion } from "./question-snapshot.ts";

test("a live question is actionable", () => {
  const result = classifyQuestion({ isReplay: false, wasDismissed: false });

  assert.equal(result.state, "question-requested");
  assert.equal(result.submitted, false);
  assert.equal(result.actionable, true);
});

test("a replayed question is closed until the server confirms it", () => {
  const result = classifyQuestion({ isReplay: true, wasDismissed: false });

  assert.equal(result.state, "question-responded");
  assert.equal(result.submitted, true);
  assert.equal(result.actionable, false);
});

test("a dismissed question stays closed even when it arrives live", () => {
  const result = classifyQuestion({ isReplay: false, wasDismissed: true });

  assert.equal(result.state, "question-responded");
  assert.equal(result.resolved, true);
  assert.equal(result.actionable, false);
});
