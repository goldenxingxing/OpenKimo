from __future__ import annotations

import json
import os
import re
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Literal, Self

import tomlkit
from pydantic import (
    AliasChoices,
    BaseModel,
    Field,
    SecretStr,
    ValidationError,
    field_serializer,
    field_validator,
    model_validator,
)
from tomlkit.exceptions import TOMLKitError

from kimo_cli.exception import ConfigError
from kimo_cli.hooks.config import HookDef
from kimo_cli.llm import ModelCapability, ProviderType
from kimo_cli.share import get_share_dir
from kimo_cli.skill import DEFAULT_SKILL_BRANDS
from kimo_cli.utils.logging import logger

_SKILL_BRAND_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
"""A brand name must be a safe single path segment. Anchored via ``fullmatch``,
and the leading-character class rules out ``.``, ``..`` and dotfile names."""


class OAuthRef(BaseModel):
    """Reference to OAuth credentials stored outside the config file."""

    storage: Literal["keyring", "file"] = "file"
    """Credential storage backend."""
    key: str
    """Storage key to locate OAuth credentials."""


class LLMProvider(BaseModel):
    """LLM provider configuration."""

    type: ProviderType
    """Provider type"""
    base_url: str
    """API base URL"""
    api_key: SecretStr
    """API key"""
    env: dict[str, str] | None = None
    """Environment variables to set before creating the provider instance"""
    custom_headers: dict[str, str] | None = None
    """Custom headers to include in API requests"""
    reasoning_key: str | None = None
    """Message field name carrying reasoning content for OpenAI-compatible APIs.
    Applies to provider type ``openai_legacy``. Defaults to ``reasoning_content``
    when unset. Use an empty string to disable reasoning round-tripping."""
    oauth: OAuthRef | None = None
    """OAuth credential reference (do not store tokens here)."""

    @field_serializer("api_key", when_used="json")
    def dump_secret(self, v: SecretStr):
        return v.get_secret_value()


class LLMModel(BaseModel):
    """LLM model configuration."""

    provider: str
    """Provider name"""
    model: str
    """Model name"""
    max_context_size: int
    """Maximum context size (unit: tokens)"""
    capabilities: set[ModelCapability] | None = None
    """Model capabilities"""
    display_name: str | None = None
    """Human-readable model name (sourced from the provider's models API when available)"""


class LoopControl(BaseModel):
    """Agent loop control configuration."""

    max_steps_per_turn: int = Field(
        default=1000,
        ge=1,
        validation_alias=AliasChoices("max_steps_per_turn", "max_steps_per_run"),
    )
    """Maximum number of steps in one turn"""
    max_retries_per_step: int = Field(default=5, ge=0)
    """Maximum number of LLM attempts in one step, including the first one.
    ``0`` means no limit: a retryable failure is retried until it succeeds or
    the turn is cancelled. Only errors that can clear on their own are retried
    at all; a request the provider rejects outright still fails at once.

    Attempts are spaced by exponential backoff (or by the server's
    ``Retry-After`` when it sends one). Each attempt is one call into the
    provider SDK, which may itself retry a failed request up to twice."""
    finalize_on_max_steps: bool = Field(default=True)
    """When the step budget is exhausted, run one final no-tools round asking the
    model to summarize completed work, what remains, and give a final answer, so
    the turn ends with a real response instead of ``MaxStepsReached``. If the
    finalization round itself fails (error, timeout, or the model still tries to
    call tools), the turn falls back to the historical ``MaxStepsReached`` raise."""
    finalization_timeout_s: float = Field(default=120.0, ge=1.0)
    """Wall-clock budget for the finalization round. Unlike regular steps it does
    not go through the retry loop (the step budget is already exhausted), so this
    timeout is the only guard against a hung provider."""
    max_ralph_iterations: int = Field(default=0, ge=-1)
    """Extra iterations after the first turn in Ralph mode. Use -1 for unlimited."""
    reserved_context_size: int = Field(default=50_000, ge=1000)
    """Reserved token count for LLM response generation. Auto-compaction triggers when
    either context_tokens + reserved_context_size >= max_context_size or
    context_tokens >= max_context_size * compaction_trigger_ratio. Default is 50000."""
    compaction_trigger_ratio: float = Field(default=0.95, ge=0.5, le=0.99)
    """Context usage ratio threshold for auto-compaction. Default is 0.95 (95%).
    Auto-compaction triggers when context_tokens >= max_context_size * compaction_trigger_ratio
    or when context_tokens + reserved_context_size >= max_context_size."""
    max_request_bytes: int = Field(default=1_800_000, ge=100_000)
    """Approximate request-body byte budget for a single LLM request. The provider
    enforces a hard request-body limit (2 MiB on Moonshot); when the estimated
    serialized size of system prompt + history exceeds this threshold, auto-compaction
    triggers even if the token-based thresholds are not reached. Default is 1800000,
    leaving ~300 KiB headroom for tool schemas and generation parameters."""
    prune_tool_outputs: bool = True
    """Clear old tool outputs from requests (the history keeps them). They are cleared
    in batches so the request prefix, and with it the provider's prompt cache, stays
    stable between batches."""
    prune_protect_tokens: int = Field(default=40_000, ge=0)
    """The most recent tool output, in tokens, that is never cleared. Default 40000."""
    prune_minimum_tokens: int = Field(default=20_000, ge=1)
    """Older tool output, in tokens, that has to build up before a batch is cleared.
    Default 20000."""


class MemoryConfig(BaseModel):
    """Cross-session memory behaviour."""

    propose_candidates: bool = Field(
        default=True,
        description=(
            "Notice facts worth keeping and queue them for approval. Without "
            "this, persistent memory only ever contains what the agent thought "
            "to record with a tool call at the moment it came up, so anything "
            "it did not think of is lost. Proposals are never written to memory "
            "on their own — they wait for the same approval an explicit add "
            "requires. Costs one extra model call where summaries are already "
            "produced: at compaction and session end."
        ),
    )


class BackgroundConfig(BaseModel):
    """Background task runtime configuration."""

    max_running_tasks: int = Field(default=50, ge=1)
    read_max_bytes: int = Field(default=30_000, ge=1024)
    notification_tail_lines: int = Field(default=20, ge=1)
    notification_tail_chars: int = Field(default=3_000, ge=256)
    wait_poll_interval_ms: int = Field(default=500, ge=50)
    worker_heartbeat_interval_ms: int = Field(default=5_000, ge=100)
    worker_stale_after_ms: int = Field(default=15_000, ge=1000)
    kill_grace_period_ms: int = Field(default=2_000, ge=100)
    auto_followup: bool = Field(
        default=True,
        description=(
            "Report background results without waiting to be asked. When a background "
            "task or subagent finishes while the session is idle, start a turn so the "
            "agent reads the result and says something. The interactive terminal has "
            "always done this; without it a client that only runs turns on request "
            "leaves the answer sitting unread until the user happens to type again."
        ),
    )
    auto_followup_coalesce_ms: int = Field(
        default=1_500,
        ge=0,
        description=(
            "How long to keep collecting completions before following up, so a batch "
            "of subagents finishing together produces one report rather than five."
        ),
    )
    keep_alive_on_exit: bool = Field(
        default=False,
        description="Keep background tasks alive when CLI exits. Default: kill on exit.",
    )
    agent_task_timeout_s: int = Field(default=900, ge=60)
    """Maximum runtime in seconds for a background agent task. Default: 900 (15 min)."""
    print_wait_ceiling_s: int = Field(default=3600, ge=1)
    """Hard ceiling for how long ``--print`` mode waits for background tasks before
    killing them and exiting. The effective wait is
    ``min(max(active_task.timeout_s or agent_task_timeout_s), print_wait_ceiling_s)``.
    Default: 3600 (1 hour)."""


class NotificationConfig(BaseModel):
    """Notification runtime configuration."""

    claim_stale_after_ms: int = Field(default=15_000, ge=1000)


class MCPClientConfig(BaseModel):
    """MCP client configuration."""

    tool_call_timeout_ms: int = 60000
    """Timeout for tool calls in milliseconds."""


class MCPConfig(BaseModel):
    """MCP configuration."""

    client: MCPClientConfig = Field(
        default_factory=MCPClientConfig, description="MCP client configuration"
    )


class Config(BaseModel):
    """Main configuration structure."""

    is_from_default_location: bool = Field(
        default=False,
        description="Whether the config was loaded from the default location",
        exclude=True,
    )
    source_file: Path | None = Field(
        default=None,
        description="Path to the loaded config file. None when loaded from --config text.",
        exclude=True,
    )
    default_model: str = Field(default="", description="Default model to use")
    default_thinking: bool = Field(default=False, description="Default thinking mode")
    default_yolo: bool = Field(default=False, description="Default yolo (auto-approve) mode")
    default_self_evolve: bool = Field(
        default=False,
        description=(
            "Master switch for self-evolution. When off (default), only execution "
            "evidence is collected (always on, no extra tokens); token-consuming "
            "evolution operations (kimo evolve propose/verify/adopt) are refused "
            "and no adopted improvements are mounted. When on, the full "
            "improvement chain is authorised, auto-evolve may propose and verify "
            "automatically at turn end, and adopted harness improvements "
            "(system prompt appends, skills) are mounted into new sessions."
        ),
    )
    evolution_auto_min_evidence: int = Field(
        default=3,
        description=(
            "Minimum number of completed turns with a task text in the library "
            "before auto-evolve will spend tokens on a propose call. Guards "
            "against burning editor tokens on thin or empty evidence libraries."
        ),
    )
    evolution_auto_cooldown_s: int = Field(
        default=1800,
        description=(
            "Wall-clock seconds between auto-evolve cycles. Prevents a bursty "
            "conversation from triggering back-to-back propose+verify pipelines, "
            "each of which can cost tens of MB of tokens. "
            "0 disables the cooldown."
        ),
    )
    skip_afk_prompt_injection: bool = Field(
        default=False,
        description=(
            "If true, suppress the afk-mode system reminder. "
            "Yolo mode does not inject a system reminder."
        ),
    )
    default_plan_mode: bool = Field(default=False, description="Default plan mode for new sessions")
    default_editor: str = Field(
        default="",
        description="Default external editor command (e.g. 'vim', 'code --wait')",
    )
    theme: Literal["dark", "light"] = Field(
        default="dark",
        description="Terminal color theme. Use 'light' for light terminal backgrounds.",
    )
    experimental: dict[str, bool] = Field(
        default_factory=dict,
        description=(
            "Experimental feature flags, e.g. ``[experimental]`` then "
            "``image_preview = true``. Each flag can also be toggled via "
            "``KIMO_CODE_EXPERIMENTAL_<NAME>`` (per-flag, wins over this table) "
            "or enabled all at once via ``KIMO_CODE_EXPERIMENTAL_FLAG``."
        ),
    )
    show_thinking_stream: bool = Field(
        default=True,
        description=(
            "If true, stream the raw reasoning text in the live area as a "
            "6-line scrolling preview and commit the full reasoning markdown "
            "to history when the block ends. Default true. Set to false to "
            "show only the compact 'Thinking ...' indicator and a one-line "
            "trace summary."
        ),
    )
    models: dict[str, LLMModel] = Field(default_factory=dict, description="List of LLM models")
    providers: dict[str, LLMProvider] = Field(
        default_factory=dict, description="List of LLM providers"
    )
    loop_control: LoopControl = Field(default_factory=LoopControl, description="Agent loop control")
    background: BackgroundConfig = Field(
        default_factory=BackgroundConfig, description="Background task configuration"
    )
    notifications: NotificationConfig = Field(
        default_factory=NotificationConfig, description="Notification configuration"
    )
    mcp: MCPConfig = Field(default_factory=MCPConfig, description="MCP configuration")
    hooks: list[HookDef] = Field(default_factory=list, description="Hook definitions")  # pyright: ignore[reportUnknownVariableType]
    skill_brands: list[str] = Field(
        default_factory=lambda: list(DEFAULT_SKILL_BRANDS),
        description=(
            "Which ``.<brand>/skills`` directories auto-discovery may read, in "
            "priority order, at both user (``~/.<brand>/skills``) and project "
            "(``<project>/.<brand>/skills``) level. Defaults to kimo-cli's own "
            "directory only: another agent's catalogue is never inherited "
            "silently, because every discovered skill costs context on every "
            "request whether or not it is ever used. Set "
            '``["kimo", "claude", "codex"]`` to restore the pre-2.0 behaviour of '
            "reading other agents' skills. Each entry must be a safe single "
            "path segment; invalid entries are rejected at load time."
        ),
    )
    merge_all_available_skills: bool = Field(
        default=True,
        description=(
            "Merge skills from all existing brand directories named in "
            "``skill_brands`` instead of using only the first one found. "
            "Defaults to true so users who keep skills in several brand "
            "directories see everything; set to false to restore the "
            "first-match-only behaviour. Has no effect when ``skill_brands`` "
            "names a single brand, which is the default."
        ),
    )
    extra_skill_dirs: list[str] = Field(
        default_factory=list,
        description=(
            "Extra directories to discover skills from, added on top of the "
            "built-in / user / project locations. Each entry may be an absolute "
            "path, ``~``-prefixed (expanded against $HOME), or relative to the "
            "project root (the nearest ``.git`` directory above the work dir). "
            "Missing paths are silently skipped."
        ),
    )
    memory: MemoryConfig = Field(
        default_factory=lambda: MemoryConfig(), description="Memory configuration"
    )
    skip_skill_dirs: list[str] = Field(
        default_factory=list,
        description=(
            "Skills directories to leave alone, matched against the resolved "
            "auto-discovery roots (user and project locations). Use this to keep "
            "another agent's catalogue — ``~/.claude/skills`` is the common one — "
            "out of the prompt: every discovered skill costs a few hundred "
            "characters of context on every request, whether or not it is ever "
            "used. Accepts absolute or ``~``-prefixed paths. Entries that match "
            "nothing are ignored. Directories named in ``extra_skill_dirs`` are "
            "not filtered: asking for a directory and skipping it is a "
            "contradiction, and the explicit request wins."
        ),
    )
    telemetry: bool = Field(
        default=False,
        description=(
            "Send anonymous usage events to Moonshot AI's Kimo Code telemetry service "
            "(telemetry-logs.kimo.com). Off by default; set to true to opt in."
        ),
    )

    @field_validator("skill_brands")
    @classmethod
    def validate_skill_brands(cls, brands: list[str]) -> list[str]:
        """Reject brand names that would not be a safe single path segment.

        Each entry becomes a ``.<brand>/skills`` directory, so a name carrying
        a separator or a ``..`` segment would let config escape the home or
        project root it is meant to be scoped to. Duplicates collapse, order
        preserved: the list is a priority order, and a repeat is noise.
        """
        seen: set[str] = set()
        out: list[str] = []
        for raw in brands:
            brand = raw.strip()
            if not _SKILL_BRAND_RE.fullmatch(brand):
                raise ValueError(
                    f"Invalid skill_brands entry {raw!r}: a brand must be a safe "
                    "single path segment matching [A-Za-z0-9][A-Za-z0-9._-]*"
                )
            if brand in seen:
                continue
            seen.add(brand)
            out.append(brand)
        return out

    @model_validator(mode="after")
    def validate_model(self) -> Self:
        if self.default_model and self.default_model not in self.models:
            raise ValueError(f"Default model {self.default_model} not found in models")
        for model in self.models.values():
            if model.provider not in self.providers:
                raise ValueError(f"Provider {model.provider} not found in providers")
        return self


def get_config_file() -> Path:
    """Get the configuration file path."""
    return get_share_dir() / "config.toml"


def get_default_config() -> Config:
    """Get the default configuration."""
    import os

    default_thinking = os.environ.get("LLM_THINKING", "false").lower() == "true"
    return Config(
        default_model="",
        default_thinking=default_thinking,
        models={},
        providers={},
    )


def load_config(config_file: Path | None = None) -> Config:
    """
    Load configuration from config file.
    If the config file does not exist, create it with default configuration.

    Args:
        config_file (Path | None): Path to the configuration file. If None, use default path.

    Returns:
        Validated Config object.

    Raises:
        ConfigError: If the configuration file is invalid.
    """
    default_config_file = get_config_file().expanduser().resolve(strict=False)
    if config_file is None:
        config_file = default_config_file
    config_file = config_file.expanduser().resolve(strict=False)
    is_default_config_file = config_file == default_config_file
    logger.debug("Loading config from file: {file}", file=config_file)

    # If the user hasn't provided an explicit config path, migrate legacy JSON config once.
    if is_default_config_file and not config_file.exists():
        _migrate_json_config_to_toml()

    if not config_file.exists():
        config = get_default_config()
        logger.debug("No config file found, creating default config: {config}", config=config)
        save_config(config, config_file)
        config.is_from_default_location = is_default_config_file
        config.source_file = config_file
        return config

    try:
        config_text = config_file.read_text(encoding="utf-8")
        if config_file.suffix.lower() == ".json":
            data = json.loads(config_text)
        else:
            data = tomlkit.loads(config_text)
        config = Config.model_validate(data)
    except json.JSONDecodeError as e:
        raise ConfigError(f"Invalid JSON in configuration file {config_file}: {e}") from e
    except UnicodeDecodeError as e:
        # Not a ValueError subclass handled above, and raised by read_text itself.
        raise ConfigError(f"Configuration file {config_file} is not valid UTF-8: {e}") from e
    except TOMLKitError as e:
        raise ConfigError(f"Invalid TOML in configuration file {config_file}: {e}") from e
    except ValidationError as e:
        raise ConfigError(f"Invalid configuration file {config_file}: {e}") from e
    config.is_from_default_location = is_default_config_file
    config.source_file = config_file
    return config


def load_config_from_string(config_string: str) -> Config:
    """
    Load configuration from a TOML or JSON string.

    Args:
        config_string (str): TOML or JSON configuration text.

    Returns:
        Validated Config object.

    Raises:
        ConfigError: If the configuration text is invalid.
    """
    if not config_string.strip():
        raise ConfigError("Configuration text cannot be empty")

    json_error: json.JSONDecodeError | None = None
    try:
        data = json.loads(config_string)
    except json.JSONDecodeError as exc:
        json_error = exc
        data = None

    if json_error is not None:
        try:
            data = tomlkit.loads(config_string)
        except TOMLKitError as toml_error:
            raise ConfigError(
                f"Invalid configuration text: {json_error}; {toml_error}"
            ) from toml_error

    try:
        config = Config.model_validate(data)
    except ValidationError as e:
        raise ConfigError(f"Invalid configuration text: {e}") from e
    config.is_from_default_location = False
    config.source_file = None
    return config


def save_config(config: Config, config_file: Path | None = None):
    """
    Save configuration to config file.

    Args:
        config (Config): Config object to save.
        config_file (Path | None): Path to the configuration file. If None, use default path.
    """
    config_file = config_file or get_config_file()
    logger.debug("Saving config to file: {file}", file=config_file)
    config_file.parent.mkdir(parents=True, exist_ok=True)
    config_data = config.model_dump(mode="json", exclude_none=True)
    if config_file.suffix.lower() == ".json":
        text = json.dumps(config_data, ensure_ascii=False, indent=2)
    else:
        text = tomlkit.dumps(config_data)  # type: ignore[reportUnknownMemberType]
    _write_text_atomic(config_file, text)


def _write_text_atomic(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` without ever leaving a truncated file behind.

    Opening the target with mode ``"w"`` truncates it before a single byte of the
    new document exists, so a disk-full error (or a serialization failure) leaves
    an empty or half-written ``config.toml``. Write to a sibling temp file, fsync
    it, then ``os.replace`` so readers only ever see the old or the new document.
    """
    descriptor, temp_name = tempfile.mkstemp(
        prefix=f".{path.name}-", suffix=".tmp", dir=path.parent
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            stream.write(text)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        with suppress(FileNotFoundError):
            os.unlink(temp_name)


def _migrate_json_config_to_toml() -> None:
    old_json_config_file = get_share_dir() / "config.json"
    new_toml_config_file = get_share_dir() / "config.toml"

    if not old_json_config_file.exists():
        return
    if new_toml_config_file.exists():
        return

    logger.info(
        "Migrating legacy config file from {old} to {new}",
        old=old_json_config_file,
        new=new_toml_config_file,
    )

    try:
        with open(old_json_config_file, encoding="utf-8") as f:
            data = json.load(f)
        config = Config.model_validate(data)
    except (json.JSONDecodeError, UnicodeDecodeError) as e:
        raise ConfigError(f"Invalid JSON in legacy configuration file: {e}") from e
    except ValidationError as e:
        raise ConfigError(f"Invalid legacy configuration file: {e}") from e

    # Write new TOML config, then keep a backup of the original JSON file.
    save_config(config, new_toml_config_file)
    backup_path = old_json_config_file.with_name("config.json.bak")
    try:
        old_json_config_file.replace(backup_path)
    except OSError as exc:
        # The TOML file is already in place and wins from now on, and the next
        # start skips migration. Failing the whole startup because a backup could
        # not be made would be worse than leaving the legacy JSON untouched.
        logger.warning(
            "Cannot back up legacy config {old}: {error}", old=old_json_config_file, error=exc
        )
        return
    logger.info("Legacy config backed up to {file}", file=backup_path)
