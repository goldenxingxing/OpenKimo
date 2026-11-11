import assert from "node:assert/strict";
import { describe, it } from "node:test";

import type { McpServer } from "../../lib/api/apis/AdminMcpApi.ts";
import {
  buildServerConfig,
  emptyMcpForm,
  formStateFromServer,
  formatKeyValueLines,
  isMaskedSecret,
  parseKeyValueLines,
  splitCommandLine,
  validateServerName,
} from "./admin-mcp-form.ts";

function server(overrides: Partial<McpServer> = {}): McpServer {
  return {
    name: "context7",
    transport: "http",
    target: "https://mcp.context7.com/mcp",
    config: { url: "https://mcp.context7.com/mcp", transport: "http" },
    uses_oauth: false,
    oauth_authorized: null,
    ...overrides,
  };
}

describe("splitCommandLine", () => {
  it("splits on whitespace", () => {
    assert.deepEqual(splitCommandLine("npx chrome-devtools-mcp@latest"), [
      "npx",
      "chrome-devtools-mcp@latest",
    ]);
  });

  it("keeps a quoted argument in one piece", () => {
    assert.deepEqual(splitCommandLine('node "/My Tools/server.js" --port 1'), [
      "node",
      "/My Tools/server.js",
      "--port",
      "1",
    ]);
  });

  it("keeps an empty quoted argument rather than dropping it", () => {
    assert.deepEqual(splitCommandLine('cmd ""'), ["cmd", ""]);
  });

  it("is empty for blank input", () => {
    assert.deepEqual(splitCommandLine("   "), []);
  });
});

describe("parseKeyValueLines", () => {
  it("reads KEY=VALUE, keeping '=' inside the value", () => {
    const parsed = parseKeyValueLines("TOKEN=abc=def\n\nOTHER=1", "=");
    assert.deepEqual(parsed.values, { TOKEN: "abc=def", OTHER: "1" });
    assert.deepEqual(parsed.invalid, []);
  });

  it("reads headers, keeping a URL's colon in the value", () => {
    const parsed = parseKeyValueLines("Authorization: Bearer x\nX-Base: https://a.example", ":");
    assert.deepEqual(parsed.values, {
      Authorization: "Bearer x",
      "X-Base": "https://a.example",
    });
  });

  it("ignores comments and blank lines", () => {
    const parsed = parseKeyValueLines("# a comment\n\nA=1", "=");
    assert.deepEqual(parsed.values, { A: "1" });
  });

  it("reports a line with no separator instead of silently dropping it", () => {
    const parsed = parseKeyValueLines("NOPE", "=");
    assert.deepEqual(parsed.values, {});
    assert.deepEqual(parsed.invalid, ["NOPE"]);
  });

  it("treats a leading separator as invalid — an empty key is not a key", () => {
    assert.deepEqual(parseKeyValueLines("=value", "=").invalid, ["=value"]);
  });
});

describe("buildServerConfig", () => {
  it("builds a stdio entry, splitting the command from its arguments", () => {
    const result = buildServerConfig({
      ...emptyMcpForm,
      transport: "stdio",
      command: "npx chrome-devtools-mcp@latest",
      args: "--headless",
      env: "TOKEN=secret",
    });

    assert.ok(result.ok);
    assert.deepEqual(result.config, {
      command: "npx",
      args: ["chrome-devtools-mcp@latest", "--headless"],
      env: { TOKEN: "secret" },
    });
  });

  it("omits args and env when empty rather than writing empty containers", () => {
    const result = buildServerConfig({
      ...emptyMcpForm,
      transport: "stdio",
      command: "server",
    });

    assert.ok(result.ok);
    assert.deepEqual(result.config, { command: "server" });
  });

  it("builds a remote entry with headers and keeps the transport it was given", () => {
    const result = buildServerConfig({
      ...emptyMcpForm,
      transport: "sse",
      url: "https://a.example/sse",
      headers: "Authorization: Bearer x",
    });

    assert.ok(result.ok);
    assert.deepEqual(result.config, {
      url: "https://a.example/sse",
      transport: "sse",
      headers: { Authorization: "Bearer x" },
    });
  });

  it("refuses a missing command, a missing URL and a non-HTTP URL", () => {
    const noCommand = buildServerConfig({ ...emptyMcpForm, transport: "stdio" });
    assert.equal(noCommand.ok, false);

    const noUrl = buildServerConfig({ ...emptyMcpForm, transport: "http" });
    assert.equal(noUrl.ok, false);

    const badUrl = buildServerConfig({
      ...emptyMcpForm,
      transport: "http",
      url: "mcp.example.com",
    });
    assert.equal(badUrl.ok, false);
  });

  it("names the offending line when a header is malformed", () => {
    const result = buildServerConfig({
      ...emptyMcpForm,
      transport: "http",
      url: "https://a.example/mcp",
      headers: "Authorization Bearer x",
    });

    assert.equal(result.ok, false);
    if (!result.ok) {
      assert.equal(result.error.code, "headerLine");
      assert.equal(result.error.detail, "Authorization Bearer x");
    }
  });
});

describe("validateServerName", () => {
  it("accepts a plain name", () => {
    assert.equal(validateServerName("context7", []), null);
  });

  it("rejects an empty name, a path-ish name and a duplicate", () => {
    assert.ok(validateServerName("  ", []));
    assert.ok(validateServerName("../escape", []));
    assert.ok(validateServerName("a b", []));
    assert.ok(validateServerName("taken", ["taken"]));
  });
});

describe("formStateFromServer", () => {
  it("round-trips a remote server through the form", () => {
    const original = server({
      config: {
        url: "https://a.example/mcp",
        transport: "http",
        headers: { Authorization: "***f3a2" },
      },
    });

    const state = formStateFromServer(original);
    assert.equal(state.headers, "Authorization: ***f3a2");

    const rebuilt = buildServerConfig(state);
    assert.ok(rebuilt.ok);
    // The mask survives unchanged, which is what tells the API to keep the
    // stored credential instead of overwriting it.
    assert.deepEqual(rebuilt.config, original.config);
  });

  it("round-trips a stdio server, command and args back into one entry", () => {
    const original = server({
      name: "local",
      transport: "stdio",
      target: "npx some-mcp --flag",
      config: { command: "npx", args: ["some-mcp", "--flag"], env: { A: "1" } },
    });

    const rebuilt = buildServerConfig(formStateFromServer(original));
    assert.ok(rebuilt.ok);
    assert.deepEqual(rebuilt.config, original.config);
  });

  it("saves an untouched entry exactly, including what the form cannot show", () => {
    const config = {
      command: "/Users/me/My Tools/srv",
      args: ["--root", "/a b", '{"k":1}'],
      cwd: "/tmp",
      timeout: 30000,
    };
    const original = server({ name: "local", transport: "stdio", target: "", config });

    const rebuilt = buildServerConfig(formStateFromServer(original));
    assert.ok(rebuilt.ok);
    assert.deepEqual(rebuilt.config, config);
  });

  it("re-reads edited args with spaces intact and keeps the other keys", () => {
    const original = server({
      name: "local",
      transport: "stdio",
      target: "",
      config: { command: "/opt/My Tools/srv", args: ["/a b"], cwd: "/tmp" },
    });
    const state = formStateFromServer(original);
    assert.equal(state.command, '"/opt/My Tools/srv"');

    const rebuilt = buildServerConfig({ ...state, args: `${state.args} --verbose` });
    assert.ok(rebuilt.ok);
    assert.deepEqual(rebuilt.config, {
      command: "/opt/My Tools/srv",
      args: ["/a b", "--verbose"],
      cwd: "/tmp",
    });
  });

  it("drops stdio-only keys when a server is switched to a URL", () => {
    const original = server({
      name: "local",
      transport: "stdio",
      target: "",
      config: { command: "npx", cwd: "/tmp" },
    });
    const state = formStateFromServer(original);

    const rebuilt = buildServerConfig({
      ...state,
      transport: "http",
      url: "https://a.example/mcp",
    });
    assert.ok(rebuilt.ok);
    assert.deepEqual(rebuilt.config, { url: "https://a.example/mcp", transport: "http" });
  });
});

describe("masked secrets", () => {
  it("recognizes what the API masked", () => {
    assert.equal(isMaskedSecret("***f3a2"), true);
    assert.equal(isMaskedSecret("ctx7sk-real-key"), false);
  });
});

describe("formatKeyValueLines", () => {
  it("is empty for a missing mapping", () => {
    assert.equal(formatKeyValueLines(undefined, "="), "");
  });
});
