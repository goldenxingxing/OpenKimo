from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

from kimo_cli.auth.platforms import is_managed_provider_key, parse_managed_provider_key
from kimo_cli.cli import Reload
from kimo_cli.config import save_config
from kimo_cli.soul.kimosoul import KimoSoul
from kimo_cli.ui.shell.console import console
from kimo_cli.ui.shell.setup import select_platform, setup_platform
from kimo_cli.ui.shell.slash import ensure_kimo_soul, registry

if TYPE_CHECKING:
    from kimo_cli.ui.shell import Shell


def current_model_key(soul: KimoSoul) -> str | None:
    config = soul.runtime.config
    curr_model_cfg = soul.runtime.llm.model_config if soul.runtime.llm else None
    if curr_model_cfg is not None:
        for name, model_cfg in config.models.items():
            if model_cfg == curr_model_cfg:
                return name
    return config.default_model or None


@registry.command(aliases=["setup"])
async def login(app: Shell, args: str) -> None:
    """Login or setup a platform."""
    soul = ensure_kimo_soul(app)
    if soul is None:
        return
    platform = await select_platform()
    if platform is None:
        return
    ok = await setup_platform(platform)
    if not ok:
        return
    from kimo_cli.telemetry import track

    track("login", provider=platform.id)
    await asyncio.sleep(1)
    console.clear()
    raise Reload


@registry.command
async def logout(app: Shell, args: str) -> None:
    """Logout from the current platform."""
    soul = ensure_kimo_soul(app)
    if soul is None:
        return
    config = soul.runtime.config
    if not config.is_from_default_location:
        console.print(
            "[red]Logout requires the default config file; "
            "restart without --config/--config-file.[/red]"
        )
        return
    model_key = current_model_key(soul)
    if not model_key:
        console.print("[yellow]No model selected; nothing to logout.[/yellow]")
        return
    model_cfg = config.models.get(model_key)
    if model_cfg is None:
        console.print("[yellow]Current model not found; nothing to logout.[/yellow]")
        return
    provider_key = model_cfg.provider
    if not is_managed_provider_key(provider_key):
        console.print("[yellow]Current provider is not managed; nothing to logout.[/yellow]")
        return
    platform_id = parse_managed_provider_key(provider_key)
    if not platform_id:
        console.print("[yellow]Current provider is not managed; nothing to logout.[/yellow]")
        return

    if provider_key in config.providers:
        del config.providers[provider_key]
    removed_default = False
    for key, model in list(config.models.items()):
        if model.provider != provider_key:
            continue
        del config.models[key]
        if config.default_model == key:
            removed_default = True
    if removed_default:
        config.default_model = ""
    save_config(config)
    console.print("[green]✓[/green] Logged out successfully.")

    from kimo_cli.telemetry import track

    track("logout")
    await asyncio.sleep(1)
    console.clear()
    raise Reload
