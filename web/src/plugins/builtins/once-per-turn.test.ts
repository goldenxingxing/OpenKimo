import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { oncePerTurn } from "./once-per-turn.ts";

describe("the thinking animation", () => {
  it("plays for the first thinking block of a turn, and keeps playing it", () => {
    const gate = oncePerTurn();
    gate.beginTurn();

    assert.equal(gate.blockFor("think_1"), "think_1");
    // a second block while the first still plays: the same animation carries on
    assert.equal(gate.blockFor("think_2"), "think_1");
  });

  it("does not come back for later blocks of the same turn", () => {
    const gate = oncePerTurn();
    gate.beginTurn();
    gate.blockFor("think_1");
    gate.played();

    assert.equal(gate.blockFor("think_2"), null);
    assert.equal(gate.blockFor("think_3"), null);
  });

  it("plays again in the next turn", () => {
    const gate = oncePerTurn();
    gate.beginTurn();
    gate.blockFor("think_1");
    gate.played();

    gate.beginTurn();

    assert.equal(gate.blockFor("think_9"), "think_9");
  });
});
