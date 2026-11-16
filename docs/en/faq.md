# FAQ

## Installation and authentication

### Invalid API key

Possible reasons for an invalid API key:

- **Key input error**: Check for extra spaces or missing characters.
- **Key expired or revoked**: Confirm the key status in the platform console.
- **Environment variable override**: Check if `OPENAI_API_KEY` environment variable is overriding the key in the config file. You can run `echo $OPENAI_API_KEY` to check.

### Membership expired or quota exhausted

You can check your current quota and usage status with the `/usage` command.

## Interaction issues

### `cd` command doesn't work in shell mode

Executing the `cd` command in shell mode won't change Kimo Code CLI's working directory. This is because each shell command executes in an independent subprocess, and directory changes only take effect within that process.

If you need to change working directory:

- **Exit and restart**: Run the `kimo` command again in the target directory.
- **Use `--work-dir` flag**: Specify working directory at startup, like `kimo --work-dir /path/to/project`.
- **Use absolute paths in commands**: Execute commands with absolute paths directly, like `ls /path/to/dir`.

### Image paste fails

When using `Ctrl-V` to paste an image, if you see "Current model does not support image input", it means the current model doesn't support image input.

Solutions:

- **Switch to an image-capable model**: Use a model that supports the `image_in` capability.
- **Check clipboard content**: Make sure the clipboard contains actual image data, not just a file path to an image.

### Working directory deleted or removed

If the working directory becomes inaccessible during a session (external drive unplugged, directory deleted, or filesystem unmounted), Kimo Code CLI detects the situation and displays a crash report containing the session ID and work directory path, then exits cleanly. You can recover the session with `kimo -r <session-id>` from the correct directory.

## ACP issues

### IDE cannot connect to Kimo Code CLI

If your IDE (like Zed or JetBrains IDEs) cannot connect to Kimo Code CLI, check the following:

- **Confirm Kimo Code CLI is installed**: Run `kimo --version` to confirm successful installation.
- **Check configuration path**: Ensure the Kimo Code CLI path in IDE configuration is correct. You can typically use `kimo acp` as the command.
- **Check uv path**: If installed via uv, ensure `~/.local/bin` is in PATH. You can use an absolute path like `/Users/yourname/.local/bin/kimo acp`.
- **Check logs**: Examine error messages in `~/.kimo/logs/kimo.log`.

## MCP issues

### MCP server startup fails

After adding an MCP server, if tools aren't loaded or there are errors, it may be due to:

- **Command doesn't exist**: For stdio type servers, ensure the command (like `npx`) is in PATH. You can configure with an absolute path.
- **Configuration format error**: Check if `~/.kimo/mcp.json` is valid JSON. Run `kimo mcp list` to view current configuration.

Debugging steps:

```sh
# View configured servers
kimo mcp list

# Test if server is working
kimo mcp test <server-name>
```

### OAuth authorization fails

For MCP servers that require OAuth authorization (like Linear), if authorization fails:

- **Check network connection**: Ensure you can access the authorization server.
- **Re-authorize**: Run `kimo mcp auth <server-name>` to authorize again.
- **Reset authorization**: If authorization info is corrupted, run `kimo mcp reset-auth <server-name>` to clear it and retry.

### Header format error

When adding HTTP type MCP servers, header format should be `KEY: VALUE` (with a space after the colon). For example:

```sh
# Correct
kimo mcp add --transport http context7 https://mcp.context7.com/mcp --header "CONTEXT7_API_KEY: your-key"

# Wrong (missing space or using equals sign)
kimo mcp add --transport http context7 https://mcp.context7.com/mcp --header "CONTEXT7_API_KEY=your-key"
```

## Print/Wire mode issues

### Invalid JSONL input format

When using `--input-format stream-json`, input must be valid JSONL (one JSON object per line). Common issues:

- **JSON format error**: Ensure each line is a complete JSON object without syntax errors.
- **Encoding issues**: Ensure input uses UTF-8 encoding.
- **Line ending issues**: Windows users should check if line endings are `\n` rather than `\r\n`.

Correct input format example:

```json
{"role": "user", "content": "Hello"}
```

### No output in print mode

If there's no output in `--print` mode, it may be:

- **No input provided**: You need to provide input via `--prompt` (or `--command`) or stdin. For example: `kimo --print --prompt "Hello"`.
- **Output is buffered**: Try using `--output-format stream-json` for streaming output.
- **Configuration incomplete**: Ensure API key and model are configured (see [Providers](./configuration/providers.md)).

## Updates and upgrades

### macOS slow first run

macOS's Gatekeeper security mechanism checks new programs on first run, causing slow startup. Solutions:

- **Wait for check to complete**: Be patient on first run; subsequent launches will return to normal.
- **Add to Developer Tools**: Add your terminal application in "System Settings → Privacy & Security → Developer Tools".

### How to upgrade Kimo Code CLI

Use uv to upgrade to the latest version:

```sh
uv tool upgrade kimo-cli --no-cache
```

Adding `--no-cache` ensures you get the latest version.

### Update prompt on startup

When a newer version is detected by the background check, Kimo Code CLI shows a blocking update prompt before the shell loads, displaying the current and latest version information. You can choose an action with the following keys:

- `Enter`: Upgrade to the latest version immediately
- `q`: Skip for now; you will be reminded on next startup
- `s`: Skip this version and suppress future reminders (until a newer version is released)

### How to disable update reminders

If you don't want Kimo Code CLI to check for updates or show update prompts on startup, set the environment variable:

```sh
export KIMO_CLI_NO_AUTO_UPDATE=1
```

This disables background update checks, the blocking update gate on startup, and the version hint in the welcome panel. You can add this line to your shell configuration file (like `~/.zshrc` or `~/.bashrc`).
