"""Experimental feature flags.

A small, centralized gate for not-yet-stable capabilities. Each flag resolves
with the following precedence (highest wins):

1. Per-flag environment variable ``KIMO_CODE_EXPERIMENTAL_<NAME>``
2. The ``[experimental]`` table in the config file (``<name> = true``)
3. Master switch ``KIMO_CODE_EXPERIMENTAL_FLAG`` (enables every flag)
4. The flag's declared default (off unless stated otherwise)

This mirrors kimo-cli's ``IFlagService`` semantics: an explicit per-flag env or
config value beats the master switch, and the master switch only *enables*
flags (it never forces one off). Flag ids are snake_case and map to the env var
by upper-casing (``image_preview`` -> ``KIMO_CODE_EXPERIMENTAL_IMAGE_PREVIEW``).

Flags are declared in the module that owns the feature via :func:`register_flag`
at import time, so there is no central catalog to keep in sync. Read state with
:func:`enabled` / :func:`explain`; enumerate with :func:`snapshot` /
:func:`registry`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Literal

ENV_PREFIX = "KIMO_CODE_EXPERIMENTAL_"
MASTER_ENV = "KIMO_CODE_EXPERIMENTAL_FLAG"

FlagSource = Literal["env", "config", "master-env", "default"]

_TRUE = {"1", "true", "yes", "on", "enable", "enabled"}
_FALSE = {"0", "false", "no", "off", "disable", "disabled"}


def _parse_bool(raw: str | None) -> bool | None:
    """Parse a boolean env string. Returns ``None`` when unset/unrecognized."""
    if raw is None:
        return None
    value = raw.strip().lower()
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    return None


@dataclass(frozen=True, slots=True)
class FlagDefinition:
    """Static description of an experimental flag."""

    id: str
    description: str = ""
    default: bool = False

    @property
    def env(self) -> str:
        return ENV_PREFIX + self.id.upper()


@dataclass(frozen=True, slots=True)
class FlagState:
    """Resolved state of a flag, including which source decided it."""

    id: str
    enabled: bool
    source: FlagSource
    default: bool


class FlagRegistry:
    """Process-local registry of experimental flag definitions."""

    def __init__(self) -> None:
        self._defs: dict[str, FlagDefinition] = {}

    def register(self, definition: FlagDefinition) -> FlagDefinition:
        self._defs[definition.id] = definition
        return definition

    def get(self, flag_id: str) -> FlagDefinition | None:
        return self._defs.get(flag_id)

    def list(self) -> list[FlagDefinition]:
        return [self._defs[key] for key in sorted(self._defs)]


registry = FlagRegistry()


def register_flag(
    flag_id: str,
    *,
    description: str = "",
    default: bool = False,
) -> FlagDefinition:
    """Declare an experimental flag in the owning module.

    Re-registering the same id replaces the definition (last registration wins),
    so feature modules can safely declare their flags at import time.
    """
    return registry.register(FlagDefinition(id=flag_id, description=description, default=default))


# ---------------------------------------------------------------------------
# Config overrides (the [experimental] table), injected or lazily loaded
# ---------------------------------------------------------------------------

_config_overrides: dict[str, bool] | None = None


def set_config_overrides(overrides: dict[str, bool] | None) -> None:
    """Inject config overrides, e.g. from an already-loaded ``Config``.

    Pass ``None`` to fall back to lazy loading from the config file. Tests use
    this to avoid touching the real config file.
    """
    global _config_overrides
    _config_overrides = dict(overrides) if overrides is not None else None


def reset_cache() -> None:
    """Clear the cached config overrides (forces a reload on next read)."""
    global _config_overrides
    _config_overrides = None


def _get_config_overrides() -> dict[str, bool]:
    global _config_overrides
    if _config_overrides is None:
        _config_overrides = _load_overrides_from_disk()
    return _config_overrides


def _load_overrides_from_disk() -> dict[str, bool]:
    # Imported lazily to avoid a heavy/circular import at module load time.
    try:
        from kimo_cli.config import load_config

        return dict(load_config().experimental)
    except Exception as exc:
        from kimo_cli.utils.logging import logger

        logger.warning("Failed to load experimental flag overrides: {error}", error=exc)
        return {}


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def explain(flag_id: str) -> FlagState | None:
    """Resolve a flag to its :class:`FlagState`, or ``None`` if unknown."""
    definition = registry.get(flag_id)
    if definition is None:
        return None

    env_value = _parse_bool(os.environ.get(definition.env))
    if env_value is not None:
        return FlagState(flag_id, env_value, "env", definition.default)

    config_value = _get_config_overrides().get(flag_id)
    if config_value is not None:
        return FlagState(flag_id, bool(config_value), "config", definition.default)

    if _parse_bool(os.environ.get(MASTER_ENV)) is True:
        return FlagState(flag_id, True, "master-env", definition.default)

    return FlagState(flag_id, definition.default, "default", definition.default)


def enabled(flag_id: str) -> bool:
    """Whether the flag is on. Unknown flags resolve to ``False``."""
    state = explain(flag_id)
    return state.enabled if state is not None else False


def snapshot() -> dict[str, bool]:
    """Resolved on/off state for every registered flag."""
    return {definition.id: enabled(definition.id) for definition in registry.list()}
