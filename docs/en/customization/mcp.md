# Model Context Protocol

[Model Context Protocol (MCP)](https://modelcontextprotocol.io/) is an open protocol that allows AI models to safely interact with external tools and data sources. Kimo Code CLI supports connecting to MCP servers to extend AI capabilities.

## What is MCP

MCP servers provide "tools" for AI to use. For example, a database MCP server can provide query tools that allow AI to execute SQL queries; a browser MCP server can let AI control browsers for automation tasks.

Kimo Code CLI has built-in tools (file read/write, shell commands, web fetching, etc.). Through MCP, you can add more tools, such as:

- Accessing specific APIs or databases
- Controlling browsers or other applications
- Integrating with third-party services (GitHub, Linear, Notion, etc.)

## MCP server management

Use the [`kimo mcp`](../reference/kimo-mcp.md) command to manage MCP servers, or the web admin panel's **MCP** tab, which edits the same `mcp.json` (see [From the admin panel](#from-the-admin-panel)).

**Add a server**

Add an HTTP server:

```sh
# Basic usage
kimo mcp add --transport http context7 https://mcp.context7.com/mcp

# With headers
kimo mcp add --transport http context7 https://mcp.context7.com/mcp \
  --header "CONTEXT7_API_KEY: your-key"

# Using OAuth authentication
kimo mcp add --transport http --auth oauth linear https://mcp.linear.app/mcp
```

Add a stdio server (local process):

```sh
kimo mcp add --transport stdio chrome-devtools -- npx chrome-devtools-mcp@latest
```

**List servers**

```sh
kimo mcp list
```

While Kimo Code CLI is running, you can also enter `/mcp` to view connected servers and loaded tools.

**Remove a server**

```sh
kimo mcp remove context7
```

**OAuth authorization**

For servers using OAuth, you need to complete authorization first:

```sh
kimo mcp auth linear
```

This will open a browser to complete the OAuth flow. After successful authorization, Kimo Code CLI will save the token for future use.

MCP OAuth tokens are stored in `~/.kimo/mcp-oauth/`. After upgrading from older versions that used FastMCP 2.x, the old token cache is not migrated automatically; if `kimo mcp list` shows that an OAuth server needs authorization, run `kimo mcp auth <name>` again.

**Test a server**

```sh
kimo mcp test context7
```

## MCP configuration file

MCP server configuration is stored in `~/.kimo/mcp.json`, in a format compatible with other MCP clients:

```json
{
  "mcpServers": {
    "context7": {
      "url": "https://mcp.context7.com/mcp",
      "headers": {
        "CONTEXT7_API_KEY": "your-key"
      }
    },
    "chrome-devtools": {
      "command": "npx",
      "args": ["chrome-devtools-mcp@latest"],
      "env": {
        "SOME_VAR": "value"
      }
    }
  }
}
```

**Temporary configuration loading**

Use the `--mcp-config-file` flag to load a configuration file from another location:

```sh
kimo --mcp-config-file /path/to/mcp.json
```

Use the `--mcp-config` flag to pass JSON configuration directly:

```sh
kimo --mcp-config '{"mcpServers": {"test": {"url": "https://..."}}}'
```

## From the admin panel

The web admin panel has an **MCP** tab that reads and writes the same `~/.kimo/mcp.json` the CLI does. It lists the
configured servers, adds, edits and removes them, tests one server's connection and shows the tools it offers, and
gives you the raw JSON to edit when a field the form does not cover is needed.

Stored credentials — header values, `env` values and a bearer `auth` string — are shown masked as `***tail`. Send a
masked value back unchanged and the stored secret is kept; type a new value to replace it. The raw JSON editor is the
exception: it shows the file as it is, credentials included.

Servers connect when a session's worker starts, so saving a change restarts idle sessions. Sessions that are mid-turn
keep the old set of servers and are named in the confirmation; they pick the change up on their next restart.

**stdio servers and shared deployments.** A stdio entry is a command line the host executes, so configuring one from a
browser is code execution by whoever holds an admin account. The desktop app allows it. Any other deployment refuses
unless `KIMO_WEB_ALLOW_STDIO_MCP=1` is set, and a deployment with `KIMO_WEB_RESTRICT_SENSITIVE_APIS` on refuses the MCP
routes entirely, as it does for `config.toml`. `kimo mcp add` on the host is unaffected either way.

## Loading status

MCP servers initialize asynchronously after the shell UI starts, so the interface is usable immediately. The shell
status bar shows live connection progress, automatically switching to a ready state once all servers are connected. In
the web interface, a session shows a placeholder while its servers connect; if a server fails or needs authorization,
the placeholder becomes a line naming it, since its tools are missing for the rest of the session.

If multiple MCP servers are configured, loading may take a moment. The status bar progress indicator keeps you informed while connections are being established.

## Security

MCP tools may access and operate external systems. Be aware of security risks.

**Approval mechanism**

Kimo Code CLI requests user confirmation for sensitive operations (such as file modifications and command execution). MCP tools follow the same approval mechanism, with all MCP tool calls prompting for confirmation.

**Prompt injection risks**

Content returned by MCP tools may contain malicious instructions attempting to trick the AI into performing dangerous operations. Kimo Code CLI marks tool return content to help the AI distinguish between tool output and user instructions, but you should still:

- Only use MCP servers from trusted sources
- Check whether AI-proposed operations are reasonable
- Keep manual approval for high-risk operations

::: warning Note
In YOLO or AFK mode, MCP tool calls will also be automatically approved. Use these modes only when you fully trust the MCP servers.
:::
