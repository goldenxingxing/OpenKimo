# Kimo Code CLI

## Quick commands (use uv)

- `make prepare` (sync deps for all workspace packages and install git hooks)
- `make format`
- `make check`
- `make test`
- `make ai-test`
- `make build` / `make build-bin`

If running tools directly, use `uv run ...`.

## Project overview

Kimo Code CLI is a Python CLI agent for software engineering workflows. It supports an interactive
shell UI, ACP server mode for IDE integrations, and MCP tool loading.

## Tech stack

- Python 3.12+ (tooling configured for 3.14)
- CLI framework: Typer
- Async runtime: asyncio
- LLM framework: kosong
- MCP integration: fastmcp
- Logging: loguru
- Package management/build: uv + uv_build; PyInstaller for binaries
- Tests: pytest + pytest-asyncio; lint/format: ruff; types: pyright + ty

## Architecture overview

- **CLI entry**: `src/kimo_cli/cli/__init__.py` (Typer) parses flags (UI mode, agent spec, config, MCP)
  and routes into `KimoCLI` in `src/kimo_cli/app.py`.
- **App/runtime setup**: `KimoCLI.create` loads config (`src/kimo_cli/config.py`), chooses a
  model/provider (`src/kimo_cli/llm.py`), builds a `Runtime` (`src/kimo_cli/soul/agent.py`),
  loads an agent spec, restores `Context`, then constructs `KimoSoul`.
- **Agent specs**: YAML under `src/kimo_cli/agents/` loaded by `src/kimo_cli/agentspec.py`.
  Specs can `extend` base agents, select tools by import path, and register builtin subagent
  types via the `subagents` field. Subagent instances are persisted separately under the session
  directory and can be resumed by `agent_id`. System prompts live alongside specs; builtin args
  include `KIMO_NOW`, `KIMO_WORK_DIR`, `KIMO_WORK_DIR_LS`, `KIMO_AGENTS_MD`, `KIMO_SKILLS`, `KIMO_OS`, `KIMO_SHELL`
  (this file is injected via `KIMO_AGENTS_MD`).
- **Tooling**: `src/kimo_cli/soul/toolset.py` loads tools by import path, injects dependencies,
  and runs tool calls. Built-in tools live in `src/kimo_cli/tools/` (agent, shell, file, web,
  todo, background, dmail, think, plan). MCP tools are loaded via `fastmcp`; CLI management is
  in `src/kimo_cli/mcp.py` and stored in the share dir.
- **Subagents**: `LaborMarket` in `src/kimo_cli/soul/agent.py` registers builtin subagent types.
  The `Agent` tool (`src/kimo_cli/tools/agent/`) creates or resumes subagent instances, while
  `SubagentStore` persists instance metadata, prompts, wire logs, and context under
  `session/subagents/<agent_id>/`.
- **Core loop**: `src/kimo_cli/soul/kimosoul.py` is the main agent loop. It accepts user input,
  handles slash commands (`src/kimo_cli/soul/slash.py`), appends to `Context`
  (`src/kimo_cli/soul/context.py`), calls the LLM (kosong), runs tools, and performs compaction
  (`src/kimo_cli/soul/compaction.py`) when needed.
- **Approvals**: `src/kimo_cli/soul/approval.py` is the tool-facing facade. `ApprovalRuntime`
  in `src/kimo_cli/approval_runtime/` is the session-level source of truth for pending approvals,
  and approval requests are projected onto the root wire stream for Shell/Web style UIs.
- **UI/Wire**: `src/kimo_cli/soul/run_soul` connects `KimoSoul` to a `Wire`
  (`src/kimo_cli/wire/`) so UI loops can stream events. UIs live in `src/kimo_cli/ui/`
  (shell/print/acp/wire).
- **Shell UI**: `src/kimo_cli/ui/shell/` handles interactive TUI input, shell command mode,
  and slash command autocomplete; it is the default interactive experience.
- **Slash commands**: Soul-level commands live in `src/kimo_cli/soul/slash.py`; shell-level
  commands live in `src/kimo_cli/ui/shell/slash.py`. The shell UI exposes both and dispatches
  based on the registry. Standard skills register `/skill:<skill-name>` and load `SKILL.md`
  as a user prompt; flow skills register `/flow:<skill-name>` and execute the embedded flow.

## Major modules and interfaces

- `src/kimo_cli/app.py`: `KimoCLI.create(...)` and `KimoCLI.run(...)` are the main programmatic
  entrypoints; this is what UI layers use.
- `src/kimo_cli/soul/agent.py`: `Runtime` (config, session, builtins), `Agent` (system prompt +
  toolset), and `LaborMarket` (builtin subagent type registry).
- `src/kimo_cli/soul/kimosoul.py`: `KimoSoul.run(...)` is the loop boundary; it emits Wire
  messages and executes tools via `KimoToolset`.
- `src/kimo_cli/soul/context.py`: conversation history + checkpoints; used by DMail for
  checkpointed replies.
- `src/kimo_cli/soul/toolset.py`: load tools, run tool calls, bridge to MCP tools.
- `src/kimo_cli/ui/*`: shell/print/acp frontends; they consume `Wire` messages.
- `src/kimo_cli/wire/*`: event types and transport used between soul and UI.

## Repo map

- `src/kimo_cli/agents/`: built-in agent YAML specs and prompts
- `src/kimo_cli/prompts/`: shared prompt templates
- `src/kimo_cli/soul/`: core runtime/loop, context, compaction, approvals
- `src/kimo_cli/tools/`: built-in tools
- `src/kimo_cli/ui/`: UI frontends (shell/print/acp/wire)
- `src/kimo_cli/acp/`: ACP server components
- `packages/kosong/`, `packages/kaos/`: workspace deps
  + Kosong is an LLM abstraction layer designed for modern AI agent applications.
    It unifies message structures, asynchronous tool orchestration, and pluggable
    chat providers so you can build agents with ease and avoid vendor lock-in.
  + PyKAOS is a lightweight Python library providing an abstraction layer for agents
    to interact with operating systems. File operations and command executions via KAOS
    can be easily switched between local environment and remote systems over SSH.
- `tests/`, `tests_ai/`: test suites
- `klips`: Kimo Code CLI Improvement Proposals

## Conventions and quality

- Python >=3.12 (ty config uses 3.14); line length 100.
- Ruff handles lint + format (rules: E, F, UP, B, SIM, I); pyright + ty for type checks.
- Tests use pytest + pytest-asyncio; files are `tests/test_*.py`.
- CLI entry points: `kimo` / `kimo-cli` -> `src/kimo_cli/__main__.py` (routes to `src/kimo_cli/cli/__init__.py`).
- User config: `~/.kimo/config.toml`; logs, sessions, and MCP config live in `~/.kimo/`.

## Git commit messages

Conventional Commits format:

```
<type>(<scope>): <subject>
```

Allowed types:
`feat`, `fix`, `test`, `refactor`, `chore`, `style`, `docs`, `perf`, `build`, `ci`, `revert`.

## Versioning

The project follows a **minor-bump-only** versioning scheme (`MAJOR.MINOR.PATCH`):

- **Patch** version is always `0`. Never bump it.
- **Minor** version is bumped for any change: new features, improvements, bug fixes, etc.
- **Major** version is only changed by explicit manual decision; it stays unchanged during
  normal development.

Examples: `0.68.0` → `0.69.0` → `0.70.0`; never `0.68.1`.

This rule applies to all packages in the repo (root, `packages/*`, `sdks/*`) as well as release
and skill workflows.

## Release workflow

For the full procedure, follow the `release` skill (`.agents/skills/release/SKILL.md`). The summary:

1. Ensure `main` is up to date (pull latest).
2. Create a release branch, e.g. `bump-0.68` or `bump-pykaos-0.5.3`.
3. Update `CHANGELOG.md`: add a new `## 0.68 (YYYY-MM-DD)` section below `## Unreleased` (do not rename `## Unreleased`).
4. Update `pyproject.toml` version.
5. Run `uv sync` to align `uv.lock`.
6. Commit the branch and open a PR.
7. Merge the PR, then switch back to `main` and pull latest.
8. Tag and push:
   - `git tag 0.68` or `git tag pykaos-0.5.3`
   - `git push --tags`
9. GitHub Actions handles the release after tags are pushed.
