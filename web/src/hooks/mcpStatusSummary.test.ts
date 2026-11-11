import assert from "node:assert/strict";
import { describe, it } from "node:test";

import { summarizeMcpStatus } from "./mcpStatusSummary.ts";
import type { MCPServerSnapshot, MCPStatusSnapshot } from "./wireTypes.ts";

function server(
  name: string,
  status: MCPServerSnapshot["status"],
  tools: string[] = [],
): MCPServerSnapshot {
  return { name, status, tools };
}

function snapshot(servers: MCPServerSnapshot[]): MCPStatusSnapshot {
  return {
    loading: false,
    connected: servers.filter((s) => s.status === "connected").length,
    total: servers.length,
    tools: servers.reduce((sum, s) => sum + s.tools.length, 0),
    servers,
  };
}

describe("summarizeMcpStatus", () => {
  it("says nothing when there are no servers", () => {
    assert.equal(summarizeMcpStatus(null), null);
    assert.equal(summarizeMcpStatus(snapshot([])), null);
  });

  it("says nothing when every server connected", () => {
    const status = snapshot([
      server("a", "connected", ["x"]),
      server("b", "connected", ["y", "z"]),
    ]);
    assert.equal(summarizeMcpStatus(status), null);
  });

  it("names the servers that failed", () => {
    const summary = summarizeMcpStatus(
      snapshot([server("a", "connected", ["x"]), server("broken", "failed")]),
    );

    assert.ok(summary);
    assert.equal(summary.problem, true);
    assert.ok(summary.text.includes("1/2 servers connected"));
    assert.ok(summary.text.includes("broken failed"));
  });

  it("points an unauthorized server at the command that fixes it", () => {
    const summary = summarizeMcpStatus(
      snapshot([server("linear", "unauthorized")]),
    );

    assert.ok(summary);
    assert.ok(summary.text.includes("linear needs authorization"));
    assert.ok(summary.text.includes("`kimo mcp auth linear`"));
  });

  it("reports both kinds of trouble at once", () => {
    const summary = summarizeMcpStatus(
      snapshot([
        server("ok", "connected", ["t"]),
        server("broken", "failed"),
        server("linear", "unauthorized"),
      ]),
    );

    assert.ok(summary);
    assert.ok(summary.text.includes("broken failed"));
    assert.ok(summary.text.includes("linear needs authorization"));
  });
});
