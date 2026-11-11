from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable, Iterable
from typing import TYPE_CHECKING, Any, cast

from prompt_toolkit.shortcuts.choice_input import ChoiceInput

from kimo_cli import logger
from kimo_cli.auth.platforms import get_platform_name_for_provider, refresh_managed_models
from kimo_cli.cli import Reload, SwitchToVis, SwitchToWeb
from kimo_cli.config import load_config, save_config
from kimo_cli.exception import ConfigError
from kimo_cli.session import Session
from kimo_cli.soul.kimosoul import KimoSoul
from kimo_cli.ui.shell.console import console
from kimo_cli.ui.shell.mcp_status import render_mcp_console
from kimo_cli.ui.shell.task_browser import TaskBrowserApp
from kimo_cli.utils.changelog import CHANGELOG
from kimo_cli.utils.slashcmd import SlashCommand, SlashCommandRegistry

if TYPE_CHECKING:
    from kimo_cli.ui.shell import Shell

type ShellSlashCmdFunc = Callable[[Shell, str], None | Awaitable[None]]
"""
A function that runs as a Shell-level slash command.

Raises:
    Reload: When the configuration should be reloaded.
"""


registry = SlashCommandRegistry[ShellSlashCmdFunc]()
shell_mode_registry = SlashCommandRegistry[ShellSlashCmdFunc]()


def ensure_kimo_soul(app: Shell) -> KimoSoul | None:
    if not isinstance(app.soul, KimoSoul):
        console.print("[red]KimoSoul required[/red]")
        return None
    return app.soul


@registry.command(aliases=["quit"])
@shell_mode_registry.command(aliases=["quit"])
def exit(app: Shell, args: str):
    """Exit the application"""
    # should be handled by `Shell`
    raise NotImplementedError


SKILL_COMMAND_PREFIX = "skill:"

_KEYBOARD_SHORTCUTS = [
    ("Ctrl-X", "Toggle agent/shell mode"),
    ("Shift-Tab", "Toggle plan mode (read-only research)"),
    ("Ctrl-O", "Edit in external editor ($VISUAL/$EDITOR)"),
    ("Ctrl-J / Alt-Enter", "Insert newline"),
    ("Ctrl-V", "Paste (supports images)"),
    ("Ctrl-D", "Exit"),
    ("Ctrl-C", "Interrupt"),
]


def _unique_commands(commands: Iterable[SlashCommand[Any]]) -> list[SlashCommand[Any]]:
    unique: list[SlashCommand[Any]] = []
    seen: set[str] = set()
    for cmd in commands:
        if cmd.name in seen:
            continue
        unique.append(cmd)
        seen.add(cmd.name)
    return unique


def _expanded_command_items(commands: Iterable[SlashCommand[Any]]) -> list[tuple[str, str]]:
    items: list[tuple[str, str]] = []
    for cmd in sorted(_unique_commands(commands), key=lambda c: c.name):
        seen = {cmd.name}
        items.append((cmd.display_name(cmd.name), cmd.description))
        for alias in cmd.aliases:
            if alias in seen:
                continue
            items.append((cmd.display_name(alias), cmd.description))
            seen.add(alias)
    return items


@registry.command(aliases=["h", "?"])
@shell_mode_registry.command(aliases=["h", "?"])
def help(app: Shell, args: str):
    """Show help information"""
    from rich.console import Group, RenderableType
    from rich.text import Text

    from kimo_cli.utils.rich.columns import BulletColumns

    def section(title: str, items: list[tuple[str, str]], color: str) -> BulletColumns:
        lines: list[RenderableType] = [Text.from_markup(f"[bold]{title}:[/bold]")]
        for name, desc in items:
            lines.append(
                BulletColumns(
                    Text.from_markup(f"[{color}]{name}[/{color}]: [grey50]{desc}[/grey50]"),
                    bullet_style=color,
                )
            )
        return BulletColumns(Group(*lines))

    renderables: list[RenderableType] = []
    renderables.append(
        BulletColumns(
            Group(
                Text.from_markup("[grey50]Help! I need somebody. Help! Not just anybody.[/grey50]"),
                Text.from_markup("[grey50]Help! You know I need someone. Help![/grey50]"),
                Text.from_markup("[grey50]\u2015 The Beatles, [italic]Help![/italic][/grey50]"),
            ),
            bullet_style="grey50",
        )
    )
    renderables.append(
        BulletColumns(
            Text(
                "Sure, OpenKimo is ready to help! "
                "Just send me messages and I will help you get things done!"
            ),
        )
    )

    commands: list[SlashCommand[Any]] = []
    skills: list[SlashCommand[Any]] = []
    for cmd in app.available_slash_commands.values():
        if cmd.name.startswith(SKILL_COMMAND_PREFIX):
            skills.append(cmd)
        else:
            commands.append(cmd)

    renderables.append(section("Keyboard shortcuts", _KEYBOARD_SHORTCUTS, "yellow"))
    renderables.append(
        section(
            "Slash commands",
            _expanded_command_items(commands),
            "blue",
        )
    )
    if skills:
        renderables.append(
            section(
                "Skills",
                _expanded_command_items(skills),
                "cyan",
            )
        )

    with console.pager(styles=True):
        console.print(Group(*renderables))


@registry.command
async def btw(app: Shell, args: str):
    """Ask a side question without interrupting the main conversation"""
    question = args.strip()
    if not question:
        console.print('[yellow]Usage: "/btw <question>"[/yellow]')
        return
    if ensure_kimo_soul(app) is None:
        return
    if app._prompt_session is None:  # pyright: ignore[reportPrivateUsage]
        console.print("[yellow]/btw is only available in interactive shell mode.[/yellow]")
        return
    await app._run_btw_modal(question, app._prompt_session)  # pyright: ignore[reportPrivateUsage]


@registry.command
@shell_mode_registry.command
def version(app: Shell, args: str):
    """Show version information"""
    from kimo_cli.constant import VERSION

    console.print(f"kimo, version {VERSION}")


@registry.command
async def model(app: Shell, args: str):
    """Switch LLM model or thinking mode"""
    from kimo_cli.llm import derive_model_capabilities

    soul = ensure_kimo_soul(app)
    if soul is None:
        return
    config = soul.runtime.config

    await refresh_managed_models(config)

    if not config.models:
        console.print('[yellow]No models configured, send "/login" to login.[/yellow]')
        return

    if not config.is_from_default_location:
        console.print(
            "[yellow]Model switching requires the default config file; "
            "restart without --config/--config-file.[/yellow]"
        )
        return

    # Find current model/thinking from runtime (may be overridden by --model/--thinking)
    curr_model_cfg = soul.runtime.llm.model_config if soul.runtime.llm else None
    curr_model_name: str | None = None
    if curr_model_cfg is not None:
        for name, model_cfg in config.models.items():
            if model_cfg == curr_model_cfg:
                curr_model_name = name
                break
    curr_thinking = soul.thinking

    # Step 1: Select model
    model_choices: list[tuple[str, str]] = []
    for name in sorted(config.models):
        model_cfg = config.models[name]
        provider_label = get_platform_name_for_provider(model_cfg.provider) or model_cfg.provider
        marker = " (current)" if name == curr_model_name else ""
        display = model_cfg.display_name or model_cfg.model
        label = f"{display} ({provider_label}){marker}"
        model_choices.append((name, label))

    try:
        selected_model_name = await ChoiceInput(
            message="Select a model (↑↓ navigate, Enter select, Ctrl+C cancel):",
            options=model_choices,
            default=curr_model_name or model_choices[0][0],
        ).prompt_async()
    except (EOFError, KeyboardInterrupt):
        return

    if not selected_model_name:
        return

    selected_model_cfg = config.models[selected_model_name]
    selected_provider = config.providers.get(selected_model_cfg.provider)
    if selected_provider is None:
        console.print(f"[red]Provider not found: {selected_model_cfg.provider}[/red]")
        return

    # Step 2: Determine thinking mode
    capabilities = derive_model_capabilities(selected_model_cfg)
    new_thinking: bool

    if "always_thinking" in capabilities:
        new_thinking = True
    elif "thinking" in capabilities:
        thinking_choices: list[tuple[str, str]] = [
            ("off", "off" + (" (current)" if not curr_thinking else "")),
            ("on", "on" + (" (current)" if curr_thinking else "")),
        ]
        try:
            thinking_selection = await ChoiceInput(
                message="Enable thinking mode? (↑↓ navigate, Enter select, Ctrl+C cancel):",
                options=thinking_choices,
                default="on" if curr_thinking else "off",
            ).prompt_async()
        except (EOFError, KeyboardInterrupt):
            return

        if not thinking_selection:
            return

        new_thinking = thinking_selection == "on"
    else:
        new_thinking = False

    # Check if anything changed
    model_changed = curr_model_name != selected_model_name
    thinking_changed = curr_thinking != new_thinking
    selected_display = selected_model_cfg.display_name or selected_model_cfg.model

    if not model_changed and not thinking_changed:
        console.print(
            f"[yellow]Already using {selected_display} "
            f"with thinking {'on' if new_thinking else 'off'}.[/yellow]"
        )
        return

    # Save and reload
    prev_model = config.default_model
    prev_thinking = config.default_thinking
    config.default_model = selected_model_name
    config.default_thinking = new_thinking
    try:
        config_for_save = load_config()
        config_for_save.default_model = selected_model_name
        config_for_save.default_thinking = new_thinking
        save_config(config_for_save)
    except (ConfigError, OSError) as exc:
        config.default_model = prev_model
        config.default_thinking = prev_thinking
        console.print(f"[red]Failed to save config: {exc}[/red]")
        return

    from kimo_cli.telemetry import track

    if model_changed:
        track("model_switch", model=selected_model_name)
    if thinking_changed:
        track("thinking_toggle", enabled=new_thinking)
    console.print(
        f"[green]Switched to {selected_display} "
        f"with thinking {'on' if new_thinking else 'off'}. "
        "Reloading...[/green]"
    )
    raise Reload(session_id=soul.runtime.session.id)


@registry.command
@shell_mode_registry.command
async def editor(app: Shell, args: str):
    """Set default external editor for Ctrl-O"""
    from kimo_cli.utils.editor import get_editor_command

    soul = ensure_kimo_soul(app)
    if soul is None:
        return
    config = soul.runtime.config
    config_file = config.source_file
    if config_file is None:
        console.print(
            "[yellow]Editor switching is unavailable with inline --config; "
            "use --config-file to persist this setting.[/yellow]"
        )
        return

    current_editor = config.default_editor

    # If args provided directly, use as editor command
    if args.strip():
        new_editor = args.strip()
    else:
        options: list[tuple[str, str]] = [
            ("code --wait", "VS Code (code --wait)"),
            ("vim", "Vim"),
            ("nano", "Nano"),
            ("", "Auto-detect (use $VISUAL/$EDITOR)"),
        ]
        # Mark current selection
        options = [
            (val, label + (" ← current" if val == current_editor else "")) for val, label in options
        ]

        try:
            choice = cast(
                str | None,
                await ChoiceInput(
                    message="Select an editor (↑↓ navigate, Enter select, Ctrl+C cancel):",
                    options=options,
                    default=(
                        current_editor
                        if current_editor in {v for v, _ in options}
                        else "code --wait"
                    ),
                ).prompt_async(),
            )
        except (EOFError, KeyboardInterrupt):
            return

        if choice is None:
            return
        new_editor = choice

    # Validate the editor binary is available
    if new_editor:
        import shlex
        import shutil

        try:
            parts = shlex.split(new_editor)
        except ValueError:
            console.print(f"[red]Invalid editor command: {new_editor}[/red]")
            return

        binary = parts[0]
        if not shutil.which(binary):
            console.print(
                f"[yellow]Warning: '{binary}' not found in PATH. "
                f"Saving anyway — make sure it's installed before using Ctrl-O.[/yellow]"
            )

    if new_editor == current_editor:
        console.print(f"[yellow]Editor is already set to: {new_editor or 'auto-detect'}[/yellow]")
        return

    # Save to disk
    try:
        config_for_save = load_config(config_file)
        config_for_save.default_editor = new_editor
        save_config(config_for_save, config_file)
    except (ConfigError, OSError) as exc:
        console.print(f"[red]Failed to save config: {exc}[/red]")
        return

    # Sync in-memory config so Ctrl-O picks it up immediately
    config.default_editor = new_editor

    if new_editor:
        console.print(f"[green]Editor set to: {new_editor}[/green]")
    else:
        resolved = get_editor_command()
        label = " ".join(resolved) if resolved else "none"
        console.print(f"[green]Editor set to auto-detect (resolved: {label})[/green]")


@registry.command(aliases=["release-notes"])
@shell_mode_registry.command(aliases=["release-notes"])
def changelog(app: Shell, args: str):
    """Show release notes"""
    from rich.console import Group, RenderableType
    from rich.text import Text

    from kimo_cli.utils.rich.columns import BulletColumns

    renderables: list[RenderableType] = []
    for ver, entry in CHANGELOG.items():
        title = f"[bold]{ver}[/bold]"
        if entry.description:
            title += f": {entry.description}"

        lines: list[RenderableType] = [Text.from_markup(title)]
        for item in entry.entries:
            if item.lower().startswith("lib:"):
                continue
            lines.append(
                BulletColumns(
                    Text.from_markup(f"[grey50]{item}[/grey50]"),
                    bullet_style="grey50",
                ),
            )
        renderables.append(BulletColumns(Group(*lines)))

    with console.pager(styles=True):
        console.print(Group(*renderables))


@registry.command
@shell_mode_registry.command
async def feedback(app: Shell, args: str):
    """Submit feedback to make OpenKimo better"""
    import webbrowser

    ISSUE_URL = "https://github.com/j0x7c4/OpenKimo/issues"

    def _fallback_to_issues():
        if not webbrowser.open(ISSUE_URL):
            console.print(f"Please submit feedback at [underline]{ISSUE_URL}[/underline].")

    # The kimo-code feedback endpoint was removed with the Moonshot
    # decoupling; route feedback to the GitHub issue tracker instead.
    _fallback_to_issues()


async def _do_new_session(app: Shell, args: str) -> None:
    """Shared implementation for /new and /clear."""
    soul = ensure_kimo_soul(app)
    if soul is None:
        return
    current_session = soul.runtime.session
    work_dir = current_session.work_dir
    # Clean up the current session if it has no content, so that chaining
    # /new commands (or switching away before the first message) does not
    # leave orphan empty session directories on disk.
    if current_session.is_empty():
        await current_session.delete()
    session = await Session.create(work_dir)
    from kimo_cli.telemetry import track

    track("session_new")
    console.print("[green]New session created. Switching...[/green]")
    raise Reload(session_id=session.id)


@registry.command(aliases=["reset"])
async def clear(app: Shell, args: str) -> None:
    """Start a new session (alias for /new)"""
    await _do_new_session(app, args)


@registry.command
async def new(app: Shell, args: str) -> None:
    """Start a new session"""
    await _do_new_session(app, args)


@registry.command(name="title", aliases=["rename"])
async def title(app: Shell, args: str):
    """Set or show the session title"""
    soul = ensure_kimo_soul(app)
    if soul is None:
        return
    session = soul.runtime.session
    if not args.strip():
        console.print(f"Session title: [bold]{session.title}[/bold]")
        return

    from kimo_cli.session_state import load_session_state, save_session_state

    new_title = args.strip()[:200]
    # Read-modify-write: load fresh state to avoid overwriting concurrent web changes
    fresh = load_session_state(session.dir)
    fresh.custom_title = new_title
    fresh.title_generated = True
    save_session_state(fresh, session.dir)
    session.state.custom_title = new_title
    session.state.title_generated = True
    session.title = new_title
    console.print(f"[green]Session title set to: {new_title}[/green]")


@registry.command(name="sessions", aliases=["resume"])
async def list_sessions(app: Shell, args: str):
    """List sessions and resume optionally"""
    import shlex

    from kimo_cli.ui.shell.session_picker import SessionPickerApp

    soul = ensure_kimo_soul(app)
    if soul is None:
        return

    current_session = soul.runtime.session
    result = await SessionPickerApp(
        work_dir=current_session.work_dir,
        current_session=current_session,
    ).run()

    if result is None:
        return

    selection, selected_work_dir = result

    if selection == current_session.id:
        console.print("[yellow]You are already in this session.[/yellow]")
        return

    if selected_work_dir != current_session.work_dir:
        cmd = f"kimo --work-dir {shlex.quote(str(selected_work_dir))} --session {selection}"
        console.print(f"[yellow]Session is in a different directory. Run:[/yellow]\n  {cmd}")
        return

    from kimo_cli.telemetry import track

    track("session_resume")
    console.print(f"[green]Switching to session {selection}...[/green]")
    raise Reload(session_id=selection)


@registry.command(name="task")
@shell_mode_registry.command(name="task")
async def task(app: Shell, args: str):
    """Browse and manage background tasks"""
    soul = ensure_kimo_soul(app)
    if soul is None:
        return
    if args.strip():
        console.print('[yellow]Usage: "/task" opens the interactive task browser.[/yellow]')
        return
    if soul.runtime.role != "root":
        console.print("[yellow]Background tasks are only available from the root agent.[/yellow]")
        return

    await TaskBrowserApp(soul).run()


@registry.command
@shell_mode_registry.command
def theme(app: Shell, args: str):
    """Switch terminal color theme (dark/light)"""
    from kimo_cli.ui.theme import get_active_theme

    soul = ensure_kimo_soul(app)
    if soul is None:
        return

    current = get_active_theme()
    arg = args.strip()

    if arg.lower().startswith("load "):
        from kimo_cli.ui.theme import load_custom_theme, set_custom_theme

        path = arg[5:].strip()
        try:
            custom = load_custom_theme(path)
        except (OSError, ValueError) as exc:
            console.print(f"[red]Failed to load custom theme: {exc}[/red]")
            return
        set_custom_theme(custom)
        console.print(f"[green]Loaded custom theme '{custom.name}' (base={custom.base}).[/green]")
        console.print("[grey50]Applies to this session; /theme dark|light to revert.[/grey50]")
        return

    if arg.lower() in ("dark", "light"):
        from kimo_cli.ui.theme import clear_custom_theme

        clear_custom_theme()

    arg = arg.lower()

    if not arg:
        console.print(f"Current theme: [bold]{current}[/bold]")
        console.print("[grey50]Usage: /theme dark | light | load <file.toml>[/grey50]")
        return

    if arg not in ("dark", "light"):
        console.print(f"[red]Unknown theme: {arg}. Use 'dark', 'light', or 'load <file>'.[/red]")
        return

    if arg == current:
        console.print(f"[yellow]Already using {arg} theme.[/yellow]")
        return

    config_file = soul.runtime.config.source_file
    if config_file is None:
        console.print(
            "[yellow]Theme switching requires a config file; "
            "restart without --config to persist this setting.[/yellow]"
        )
        return

    # Persist to disk first — only update in-memory state after success
    try:
        config_for_save = load_config(config_file)
        config_for_save.theme = arg  # type: ignore[assignment]
        save_config(config_for_save, config_file)
    except (ConfigError, OSError) as exc:
        console.print(f"[red]Failed to save config: {exc}[/red]")
        return

    from kimo_cli.telemetry import track

    track("theme_switch", theme=arg)
    console.print(f"[green]Switched to {arg} theme. Reloading...[/green]")
    raise Reload(session_id=soul.runtime.session.id)


@registry.command
@shell_mode_registry.command
def experimental(app: Shell, args: str):
    """List experimental feature flags and how each is enabled"""
    from rich.console import Group
    from rich.table import Table
    from rich.text import Text

    from kimo_cli import flags

    definitions = flags.registry.list()
    if not definitions:
        console.print("[grey50]No experimental flags registered.[/grey50]")
        return

    table = Table(title="Experimental flags", show_lines=False)
    table.add_column("flag", style="cyan")
    table.add_column("state")
    table.add_column("source", style="grey50")
    table.add_column("env", style="grey50")
    for definition in definitions:
        state = flags.explain(definition.id)
        assert state is not None
        state_text = Text("on", style="green") if state.enabled else Text("off", style="grey50")
        table.add_row(definition.id, state_text, state.source, definition.env)
    footer_text = Text("Enable via env or [experimental] <flag>=true in config.", style="grey50")
    console.print(Group(table, footer_text))


@registry.command
@shell_mode_registry.command
def image(app: Shell, args: str):
    """Preview an image inline in the terminal (experimental)"""
    from pathlib import Path

    from kimo_cli import flags
    from kimo_cli.ui.shell.image_preview import display_image

    if not flags.enabled("image_preview"):
        console.print("[yellow]Inline image preview is experimental.[/yellow]")
        console.print(
            "[grey50]Enable with KIMO_CODE_EXPERIMENTAL_IMAGE_PREVIEW=1 "
            "or [experimental] image_preview=true in config.[/grey50]"
        )
        return

    target = args.strip()
    if not target:
        console.print("[grey50]Usage: /image <path-to-image>[/grey50]")
        return
    path = Path(target).expanduser()
    if not path.is_file():
        console.print(f"[red]No such file: {path}[/red]")
        return

    if display_image(path):
        console.print(f"[green]Displayed {path.name} inline.[/green]")
    else:
        console.print(
            "[yellow]This terminal cannot display images inline (need iTerm2/Kitty).[/yellow]"
        )
        console.print(f"[grey50]{path}[/grey50]")


@registry.command
@shell_mode_registry.command
def mermaid(app: Shell, args: str):
    """Render a Mermaid diagram inline (experimental)"""
    from pathlib import Path

    from kimo_cli import flags
    from kimo_cli.ui.shell.mermaid_render import (
        MMDC_INSTALL_HINT,
        mmdc_available,
        render_mermaid_inline,
    )

    if not flags.enabled("mermaid_inline"):
        console.print("[yellow]Inline Mermaid rendering is experimental.[/yellow]")
        console.print(
            "[grey50]Enable with KIMO_CODE_EXPERIMENTAL_MERMAID_INLINE=1 "
            "or [experimental] mermaid_inline=true in config.[/grey50]"
        )
        return

    source = args.strip()
    if not source:
        console.print("[grey50]Usage: /mermaid <mermaid code> or /mermaid <path-to-.mmd>[/grey50]")
        return

    code = source
    candidate = Path(source).expanduser()
    if candidate.is_file():
        code = candidate.read_text(encoding="utf-8")

    if not mmdc_available():
        console.print(f"[yellow]{MMDC_INSTALL_HINT}[/yellow]")
        console.print(code)
        return

    if render_mermaid_inline(code):
        console.print("[green]Rendered diagram inline.[/green]")
    else:
        console.print(
            "[yellow]Could not render inline (need mmdc + image-capable terminal).[/yellow]"
        )
        console.print(code)


@registry.command
def web(app: Shell, args: str):
    """Open OpenKimo Web UI in browser"""
    from kimo_cli.telemetry import track

    track("web_opened")
    soul = ensure_kimo_soul(app)
    session_id = soul.runtime.session.id if soul else None
    raise SwitchToWeb(session_id=session_id)


@registry.command
def vis(app: Shell, args: str):
    """Open OpenKimo Agent Tracing Visualizer in browser"""
    from kimo_cli.telemetry import track

    track("vis_opened")
    soul = ensure_kimo_soul(app)
    session_id = soul.runtime.session.id if soul else None
    raise SwitchToVis(session_id=session_id)


@registry.command
async def mcp(app: Shell, args: str):
    """Show MCP servers and tools"""
    from rich.live import Live

    soul = ensure_kimo_soul(app)
    if soul is None:
        return
    await soul.start_background_mcp_loading()
    snapshot = soul.status.mcp_status
    if snapshot is None:
        console.print("[yellow]No MCP servers configured.[/yellow]")
        return

    if not snapshot.loading:
        console.print(render_mcp_console(snapshot))
        return

    with Live(
        render_mcp_console(snapshot),
        console=console,
        refresh_per_second=8,
        transient=False,
    ) as live:
        while True:
            snapshot = soul.status.mcp_status
            if snapshot is None:
                break
            live.update(render_mcp_console(snapshot), refresh=True)
            if not snapshot.loading:
                break
            await asyncio.sleep(0.125)
        try:
            await soul.wait_for_background_mcp_loading()
        except Exception as e:
            logger.debug("MCP loading completed with error while rendering /mcp: {error}", error=e)
        snapshot = soul.status.mcp_status
        if snapshot is not None:
            live.update(render_mcp_console(snapshot), refresh=True)


@registry.command
@shell_mode_registry.command
def hooks(app: Shell, args: str):
    """List configured hooks"""
    soul = ensure_kimo_soul(app)
    if soul is None:
        return

    engine = soul.hook_engine
    if not engine.summary:
        console.print(
            "[yellow]No hooks configured. "
            "Add [[hooks]] sections to your config.toml to set up hooks.[/yellow]"
        )
        return

    console.print()
    console.print("[bold]Configured Hooks:[/bold]")
    console.print()

    for event, entries in engine.details().items():
        console.print(f"  [cyan]{event}[/cyan]: {len(entries)} hook(s)")
        for entry in entries:
            source_tag = f" [dim]({entry['source']})[/dim]" if entry["source"] == "wire" else ""
            console.print(f"    [dim]{entry['matcher']}[/dim] {entry['command']}{source_tag}")

    console.print()


@registry.command
async def undo(app: Shell, args: str):
    """Undo: fork the session at a previous turn and retry"""
    from kimo_cli.session_fork import enumerate_turns, fork_session
    from kimo_cli.utils.string import shorten

    soul = ensure_kimo_soul(app)
    if soul is None:
        return

    session = soul.runtime.session
    wire_path = session.dir / "wire.jsonl"
    turns = enumerate_turns(wire_path)

    if not turns:
        console.print("[yellow]No turns found in this session.[/yellow]")
        return

    # Build choices: each turn's first line, truncated
    choices: list[tuple[str, str]] = []
    for turn in turns:
        first_line = turn.user_text.split("\n", 1)[0]
        label = shorten(first_line, width=80, placeholder="...")
        choices.append((str(turn.index), f"[{turn.index}] {label}"))

    try:
        selected = await ChoiceInput(
            message="Select a turn to undo (↑↓ navigate, Enter select, Ctrl+C cancel):",
            options=choices,
            default=choices[-1][0],
        ).prompt_async()
    except (EOFError, KeyboardInterrupt):
        return

    if selected is None:
        # Every other ChoiceInput here guards this; the application can exit
        # without a result, and int(None) is a traceback in the user's face.
        return

    turn_index = int(selected)

    # The selected turn is the one we want to redo — fork includes turns *before* it
    selected_turn = turns[turn_index]
    user_text = selected_turn.user_text

    if turn_index == 0:
        # Fork with no history — just the user text
        new_session = await Session.create(session.work_dir)
        new_session_id = new_session.id
        # Set title to match the convention used by fork_session
        from kimo_cli.session_state import load_session_state, save_session_state

        new_state = load_session_state(new_session.dir)
        new_state.custom_title = f"Undo: {session.title}"
        new_state.title_generated = True
        save_session_state(new_state, new_session.dir)
    else:
        # Fork includes turns 0..turn_index-1
        fork_turn_index = turn_index - 1
        new_session_id = await fork_session(
            source_session_dir=session.dir,
            work_dir=session.work_dir,
            turn_index=fork_turn_index,
            title_prefix="Undo",
            source_title=session.title,
        )

    from kimo_cli.telemetry import track

    track("undo")
    console.print(f"[green]Forked at turn {turn_index}. Switching to new session...[/green]")
    raise Reload(session_id=new_session_id, prefill_text=user_text)


@registry.command
async def fork(app: Shell, args: str):
    """Fork the current session (copy all history to a new session)"""
    from kimo_cli.session_fork import fork_session

    soul = ensure_kimo_soul(app)
    if soul is None:
        return

    session = soul.runtime.session
    new_session_id = await fork_session(
        source_session_dir=session.dir,
        work_dir=session.work_dir,
        turn_index=None,
        title_prefix="Fork",
        source_title=session.title,
    )

    from kimo_cli.telemetry import track

    track("session_fork")
    console.print("[green]Session forked. Switching to new session...[/green]")
    raise Reload(session_id=new_session_id)


from . import (  # noqa: E402
    debug,  # noqa: F401 # type: ignore[reportUnusedImport]
    export_import,  # noqa: F401 # type: ignore[reportUnusedImport]
    oauth,  # noqa: F401 # type: ignore[reportUnusedImport]
    setup,  # noqa: F401 # type: ignore[reportUnusedImport]
    usage,  # noqa: F401 # type: ignore[reportUnusedImport]
)
