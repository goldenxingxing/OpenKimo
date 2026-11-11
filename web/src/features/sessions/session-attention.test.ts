import assert from "node:assert/strict";
import { describe, it } from "node:test";

import {
  type AttentionState,
  EMPTY_ATTENTION,
  type SessionActivity,
  applyActivity,
  applySnapshot,
  attentionTitle,
  markSeen,
} from "./session-attention.ts";

function act(
  session_id: string,
  seq: number,
  state: SessionActivity["state"],
  awaiting_input = false,
): SessionActivity {
  return { session_id, state, awaiting_input, seq };
}

function run(events: SessionActivity[], viewing: string | null = null): AttentionState {
  return events.reduce((state, event) => applyActivity(state, event, viewing), EMPTY_ATTENTION);
}

const labels = {
  one: (title: string) => `【需要你查看】${title}`,
  many: (count: number) => `【${count} 个会话需要你查看】`,
  untitled: "未命名会话",
};

describe("which sessions need the person", () => {
  it("a finished reply, an error and an open approval all count the same", () => {
    const state = run([
      act("a", 1, "busy"),
      act("a", 2, "idle"),
      act("b", 1, "busy"),
      act("b", 2, "error"),
      act("c", 1, "busy"),
      act("c", 2, "busy", true),
    ]);
    assert.deepEqual(state.pending, ["a", "b", "c"]);
  });

  it("the session being looked at is never announced", () => {
    const state = run([act("a", 1, "busy"), act("a", 2, "idle")], "a");
    assert.deepEqual(state.pending, []);
  });

  it("a session that runs again, or whose approval was answered, drops out", () => {
    let state = run([act("a", 1, "busy"), act("a", 2, "busy", true)]);
    assert.deepEqual(state.pending, ["a"]);
    state = applyActivity(state, act("a", 3, "busy"), null);
    assert.deepEqual(state.pending, []);
  });

  it("an answered approval followed by the end of the turn announces again", () => {
    const state = run([
      act("a", 1, "busy"),
      act("a", 2, "busy", true),
      act("a", 3, "busy"),
      act("a", 4, "idle"),
    ]);
    assert.deepEqual(state.pending, ["a"]);
  });

  it("stopping an idle worker is not news", () => {
    const state = run([act("a", 1, "idle"), act("a", 2, "stopped")]);
    assert.deepEqual(state.pending, []);
  });

  it("a restart of an idle session is not news", () => {
    // Saving config.toml restarts every idle worker: idle, restarting, idle.
    const state = run([act("a", 1, "idle"), act("a", 2, "restarting"), act("a", 3, "idle")]);
    assert.deepEqual(state.pending, []);
  });

  it("a restart keeps a session that was already waiting", () => {
    const state = run([
      act("a", 1, "busy"),
      act("a", 2, "idle"),
      act("a", 3, "restarting"),
      act("a", 4, "idle"),
    ]);
    assert.deepEqual(state.pending, ["a"]);
  });

  it("a turn cut short by a forced restart is still announced", () => {
    const state = run([act("a", 1, "busy"), act("a", 2, "restarting"), act("a", 3, "idle")]);
    assert.deepEqual(state.pending, ["a"]);
  });

  it("stale or repeated events are ignored", () => {
    const state = run([act("a", 2, "busy"), act("a", 1, "idle"), act("a", 2, "idle")]);
    assert.deepEqual(state.pending, []);
  });

  it("a snapshot is a baseline: idle sessions are not announced", () => {
    const state = applySnapshot(EMPTY_ATTENTION, [act("a", 5, "idle"), act("b", 3, "busy")], null);
    assert.deepEqual(state.pending, []);
    const next = applyActivity(state, act("b", 4, "idle"), null);
    assert.deepEqual(next.pending, ["b"]);
  });

  it("a snapshot announces an unanswered approval, unless it is the one being viewed", () => {
    const sessions = [act("a", 5, "busy", true), act("b", 2, "busy", true)];
    assert.deepEqual(applySnapshot(EMPTY_ATTENTION, sessions, null).pending, ["a", "b"]);
    assert.deepEqual(applySnapshot(EMPTY_ATTENTION, sessions, "b").pending, ["a"]);
  });

  it("a reconnect keeps what is still waiting and drops what runs again", () => {
    const before = run([act("a", 1, "busy"), act("a", 2, "idle"), act("b", 1, "busy"), act("b", 2, "idle")]);
    const after = applySnapshot(before, [act("a", 2, "idle"), act("b", 3, "busy")], null);
    assert.deepEqual(after.pending, ["a"]);
  });

  it("opening a session clears only that one", () => {
    const state = run([act("a", 1, "busy"), act("a", 2, "idle"), act("b", 1, "busy"), act("b", 2, "idle")]);
    assert.deepEqual(markSeen(state, "a").pending, ["b"]);
    assert.equal(markSeen(state, "zzz"), state);
  });
});

describe("the tab title", () => {
  const titles: Record<string, string> = { a: "周报整理" };
  const titleOf = (id: string) => titles[id];

  it("names the one session, counts several, and is null for none", () => {
    assert.equal(attentionTitle(["a"], titleOf, labels), "【需要你查看】周报整理");
    assert.equal(attentionTitle(["x"], titleOf, labels), "【需要你查看】未命名会话");
    assert.equal(attentionTitle(["a", "x"], titleOf, labels), "【2 个会话需要你查看】");
    assert.equal(attentionTitle([], titleOf, labels), null);
  });
});
