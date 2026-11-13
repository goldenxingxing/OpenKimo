from typing import Literal, get_args

from pydantic import BaseModel, Field

HookEventType = Literal[
    "PreToolUse",
    "PostToolUse",
    "PostToolUseFailure",
    "UserPromptSubmit",
    "Stop",
    "StopFailure",
    "SessionStart",
    "SessionEnd",
    "SubagentStart",
    "SubagentStop",
    "PreCompact",
    "PostCompact",
    "Notification",
]

HOOK_EVENT_TYPES: list[str] = list(get_args(HookEventType))


class HookDef(BaseModel):
    """A single hook definition in config.toml."""

    event: HookEventType
    """Which lifecycle event triggers this hook."""
    command: str
    """Shell command to execute. Receives JSON on stdin."""
    matcher: str = ""
    """Regex pattern to filter. Empty matches everything."""
    timeout: int = Field(default=30, ge=1, le=600)
    """Timeout in seconds. Fail-open on timeout."""
