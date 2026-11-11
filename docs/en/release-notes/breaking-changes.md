# Breaking changes and migration

This page documents breaking changes in Kimo Code CLI releases and provides migration guidance.

## Unreleased

### Other agents' skills directories are no longer read by default

Auto-discovery previously walked a fixed brand chain — `.kimo/skills`, then `.claude/skills`, then `.codex/skills` — at both user and project level, and `merge_all_available_skills` (default `true`) merged every one that existed. A user with no `~/.kimo/skills` therefore had their entire Claude Code or Codex catalogue loaded into every kimo-cli request, whether or not any of it was ever used.

Which brands are read is now configurable through `skill_brands`, and it defaults to `["kimo"]`.

- **Affected**: Anyone relying on kimo-cli picking up skills from `~/.claude/skills/`, `~/.codex/skills/`, or the project-level `.claude/skills/` and `.codex/skills/` equivalents
- **Migration**: Name the brands you want in `config.toml`:

  ```toml
  skill_brands = ["kimo", "claude", "codex"]
  ```

  Or move the skills you actually use into `~/.kimo/skills/`, which needs no configuration. `extra_skill_dirs` remains available for directories that do not follow the brand layout at all.

## 1.49.0

### Kimo no longer sends legacy `reasoning_effort` automatically

Kimo thinking configuration now uses `thinking.type` exclusively. `Kimo.with_thinking(...)` no longer adds the legacy `reasoning_effort` parameter to requests.

- **Affected**: Applications using Kosong's Kimo provider with older Kimo-compatible endpoints that require `reasoning_effort`
- **Migration**: Update the endpoint to support `thinking.type`. If an older endpoint still requires the legacy parameter, pass it explicitly with `with_generation_kwargs(reasoning_effort="...")`

## 1.43.0

### MCP OAuth token cache moved to `~/.kimo/mcp-oauth/`

Kimo Code CLI now uses FastMCP 3's persistent OAuth storage API for MCP servers and stores MCP OAuth tokens under `~/.kimo/mcp-oauth/`. Tokens from the old FastMCP 2.x cache location are not migrated automatically.

- **Affected**: Users who authorized OAuth MCP servers before upgrading
- **Migration**: If `kimo mcp list` shows an OAuth server as requiring authorization, run `kimo mcp auth <name>` once to create a new token in `~/.kimo/mcp-oauth/`. Use `kimo mcp reset-auth <name>` before re-authorizing if the stored token becomes invalid or corrupted

## 1.42.0

### Windows shell backend changed from PowerShell to Git Bash

The Shell tool on Windows now runs commands through `bash.exe` (POSIX semantics) instead of `powershell.exe`. Windows users gain the same Unix-style command syntax as Linux/macOS, but must have Git for Windows installed.

- **Affected**: All Windows users; integrations, agent specs, or saved snippets that rely on PowerShell-specific syntax (`Get-ChildItem`, `Where-Object`, `cmdlet -Foo Bar` argument style, `;`-only command chaining, `NUL` redirects, etc.) reaching the Shell tool
- **Migration**:
  1. Install [Git for Windows](https://git-scm.com/downloads/win) if not already installed; the bundled `bash.exe` (typically `C:\Program Files\Git\bin\bash.exe`) is auto-discovered via `where.exe git` or the standard install location
  2. If `bash.exe` lives in a non-standard location, set the `KIMO_CLI_GIT_BASH_PATH` environment variable to its absolute path before launching kimo-cli
  3. Update any custom prompts, agent specs, or snippets that hard-code PowerShell syntax to use Unix shell syntax instead (forward slashes inside Shell commands, `/dev/null` instead of `NUL`, `&&` and `||` for control flow, `grep`/`sed`/`awk` instead of PowerShell cmdlets)
  4. Note that `python.exe`, `node.exe`, and other native Windows binaries called from inside bash still need native Windows paths (e.g. `python C:\path\to\script.py`); only POSIX-aware tools (cat, ls, grep, etc.) understand the `/c/path/...` form
  5. If kimo-cli cannot find `bash.exe`, it does not fall back to PowerShell. Since 1.50.0 it no longer exits at startup either: the session runs normally and only the Shell tool reports the missing shell, with the install hint. Set `KIMO_CLI_SHELL_PATH` to any POSIX shell, or place a portable one at `%USERPROFILE%\.kimo\bin\sh.exe`, to get commands working without Git for Windows

## 1.40.0

### `--print` now uses runtime AFK semantics instead of YOLO semantics

Print mode still runs non-interactively and handles approvals automatically, but it now sets an invocation-only AFK overlay instead of enabling YOLO. This means `--print` treats the user as unavailable and auto-dismisses `AskUserQuestion`, while later interactive resumes do not inherit AFK solely because of a previous print run.

- **Affected**: Scripts, wrappers, or custom integrations that inferred print-mode behavior from the explicit YOLO flag
- **Migration**: Treat `--print` / `--quiet` as non-interactive AFK runs. Use `--yolo` only when you want to bypass permission approvals while a user remains reachable

### `skip_yolo_prompt_injection` replaced by `skip_afk_prompt_injection`

YOLO no longer injects model guidance, so the old `skip_yolo_prompt_injection` config key is ignored. The remaining non-interactive reminder belongs to AFK mode and can be disabled with `skip_afk_prompt_injection`.

- **Affected**: Config files or embedded applications that set `skip_yolo_prompt_injection`
- **Migration**: Replace `skip_yolo_prompt_injection = true` with `skip_afk_prompt_injection = true` if you need to suppress AFK mode reminders

## 1.39.0

### `merge_all_available_skills` default flipped to `true`

The `merge_all_available_skills` config option default has changed from `false` to `true`. kimo-cli now merges all existing user- and project-level brand skill directories (`.kimo/skills`, `.claude/skills`, `.codex/skills`) by default instead of only using the first one found. Users who keep skills in multiple brand directories — for example both `~/.kimo/skills` and `~/.claude/skills` — will see every skill out of the box after upgrading.

- **Affected**: Users who maintain multiple brand skill directories and relied on the first-match behavior to hide duplicates
- **Migration**: Set `merge_all_available_skills = false` in your config to restore the previous first-match behavior

## 1.25.0

### Wire protocol 1.6 — subagent and approval field changes

The `SubagentEvent` field `task_tool_call_id` has been renamed to `parent_tool_call_id`, and new optional fields (`agent_id`, `subagent_type`) have been added. `ApprovalRequest` gains `source_kind`, `source_id`, `agent_id`, `subagent_type`, and `source_description` fields. `ApprovalResponse` gains a `feedback` field.

- **Affected**: Wire mode clients that parse `SubagentEvent` or `ApprovalRequest`/`ApprovalResponse` payloads
- **Migration**: Rename `task_tool_call_id` to `parent_tool_call_id` in your event handlers; handle the new optional fields as needed

### `CreateSubagent` and `Task` (multiagent) tools removed

The `CreateSubagent` and `Task` tools under `kimo_cli.tools.multiagent` have been removed. Use the new `Agent` tool instead.

- **Affected**: Custom agent configurations referencing `kimo_cli.tools.multiagent:Task` or `kimo_cli.tools.multiagent:CreateSubagent`
- **Migration**: Replace with `kimo_cli.tools.agent:Agent` in your agent YAML `allowed_tools`

### `TaskOutput` `block` parameter default changed

The `block` parameter of the `TaskOutput` tool now defaults to `false` (previously `true`). `TaskOutput` returns a non-blocking status/output snapshot by default.

- **Affected**: Custom agents or Wire mode clients relying on `TaskOutput` blocking by default
- **Migration**: Explicitly pass `block=true` if you need to wait for task completion

## 0.81 - Prompt Flow replaced by Flow Skills

### `--prompt-flow` option removed

The `--prompt-flow` CLI option has been removed. Use flow skills instead.

- **Affected**: Scripts and automation using `--prompt-flow` to load Mermaid/D2 flowcharts
- **Migration**: Create a flow skill with embedded Agent Flow in `SKILL.md` and invoke via `/flow:<skill-name>`

### `/begin` command replaced

The `/begin` slash command has been replaced with `/flow:<skill-name>` commands.

- **Affected**: Users who used `/begin` to start a loaded Prompt Flow
- **Migration**: Use `/flow:<skill-name>` to invoke flow skills directly

## 0.77 - Thinking mode and CLI option changes

### Thinking mode setting migration change

After upgrading from `0.76`, the thinking mode setting is no longer automatically preserved. The previous `thinking` state stored in `~/.kimo/kimo.json` is no longer used; instead, thinking mode is now managed via the `default_thinking` configuration option in `~/.kimo/config.toml`, but values are not automatically migrated from legacy `metadata`.

- **Affected**: Users who previously had thinking mode enabled
- **Migration**: Reconfigure thinking mode after upgrading:
  - Use the `/model` command to select model and set thinking mode (interactive)
  - Or manually add to `~/.kimo/config.toml`:

    ```toml
    default_thinking = true  # Set to true if you want thinking mode enabled by default
    ```

### `--query` option removed

The `--query` (`-q`) option has been removed. Use `--prompt` as the primary option, with `--command` as an alias.

- **Affected**: Scripts and automation using `--query` or `-q`
- **Migration**:
  - `--query` / `-q` → `--prompt` / `-p`
  - Or continue using `--command` / `-c`

## 0.74 - ACP command change

### `--acp` option deprecated

The `--acp` option has been deprecated. Use the `kimo acp` subcommand instead.

- **Affected**: Scripts and IDE configurations using `kimo --acp`
- **Migration**: `kimo --acp` → `kimo acp`

## 0.66 - Config file and provider type

### Config file format migration

The config file format has been migrated from JSON to TOML.

- **Affected**: Users with `~/.kimo/config.json`
- **Migration**: Kimo Code CLI will automatically read the old JSON config, but manual migration to TOML is recommended
- **New location**: `~/.kimo/config.toml`

JSON config example:

```json
{
  "default_model": "kimi-k2-0711",
  "providers": {
    "kimo": {
      "type": "kimo",
      "base_url": "https://api.kimo.com/coding/v1",
      "api_key": "your-key"
    }
  }
}
```

Equivalent TOML config:

```toml
default_model = "kimi-k2-0711"

[providers.kimo]
type = "kimo"
base_url = "https://api.kimo.com/coding/v1"
api_key = "your-key"
```

### `google_genai` provider type renamed

The provider type for Gemini Developer API has been renamed from `google_genai` to `gemini`.

- **Affected**: Users with `type = "google_genai"` in their config
- **Migration**: Change the `type` value to `"gemini"`
- **Compatibility**: `google_genai` still works but updating is recommended

## 0.57 - Tool changes

### `Shell` tool

The `Bash` tool (or `CMD` on Windows) has been unified and renamed to `Shell`.

- **Affected**: Agent files referencing `Bash` or `CMD` tools
- **Migration**: Change tool references to `Shell`

### `Task` tool moved to `multiagent` module

The `Task` tool has been moved from `kimo_cli.tools.task` to `kimo_cli.tools.multiagent`.

- **Affected**: Custom tools importing the `Task` tool
- **Migration**: Change import path to `from kimo_cli.tools.multiagent import Task`

### `PatchFile` tool removed

The `PatchFile` tool has been removed.

- **Affected**: Agent configs using the `PatchFile` tool
- **Alternative**: Use `StrReplaceFile` tool for file modifications

## 0.52 - CLI option changes

### `--ui` option removed

The `--ui` option has been removed in favor of separate flags.

- **Affected**: Scripts using `--ui print`, `--ui acp`, or `--ui wire`
- **Migration**:
  - `--ui print` → `--print`
  - `--ui acp` → `kimo acp`
  - `--ui wire` → `--wire`

## 0.42 - Keyboard shortcut changes

### Mode switch shortcut

The agent/shell mode toggle shortcut has changed from `Ctrl-K` to `Ctrl-X`.

- **Affected**: Users accustomed to using `Ctrl-K` for mode switching
- **Migration**: Use `Ctrl-X` to toggle modes

## 0.27 - CLI option rename

### `--agent` option renamed

The `--agent` option has been renamed to `--agent-file`.

- **Affected**: Scripts using `--agent` to specify custom agent files
- **Migration**: Change `--agent` to `--agent-file`
- **Note**: `--agent` is now used to specify built-in agents (e.g., `default`, `okabe`)

## 0.25 - Package name change

### Package renamed from `ensoul` to `kimo-cli`

- **Affected**: Code or scripts using the `ensoul` package name
- **Migration**:
  - Installation: `pip install ensoul` → `pip install kimo-cli` or `uv tool install kimo-cli`
  - Command: `ensoul` → `kimo`

### `ENSOUL_*` parameter prefix changed

The system prompt built-in parameter prefix has changed from `ENSOUL_*` to `KIMO_*`.

- **Affected**: Custom agent files using `ENSOUL_*` parameters
- **Migration**: Change parameter prefix to `KIMO_*` (e.g., `ENSOUL_NOW` → `KIMO_NOW`)
