from __future__ import annotations

import asyncio
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, NamedTuple

import acp
from kaos.path import KaosPath

from kimo_cli.acp.kaos import ACPKaos
from kimo_cli.acp.mcp import acp_mcp_servers_to_mcp_config
from kimo_cli.acp.session import ACPSession
from kimo_cli.acp.tools import replace_tools
from kimo_cli.acp.types import ACPContentBlock, MCPServer
from kimo_cli.acp.version import ACPVersionSpec, negotiate_version
from kimo_cli.app import KimoCLI
from kimo_cli.auth.oauth import load_tokens
from kimo_cli.config import LLMModel, load_config, save_config
from kimo_cli.constant import NAME, VERSION
from kimo_cli.llm import create_llm, derive_model_capabilities
from kimo_cli.session import Session
from kimo_cli.soul.slash import registry as soul_slash_registry
from kimo_cli.soul.toolset import KimoToolset
from kimo_cli.utils.logging import logger


class ACPServer:
    def __init__(self) -> None:
        self.client_capabilities: acp.schema.ClientCapabilities | None = None
        self.conn: acp.Client | None = None
        self.sessions: dict[str, tuple[ACPSession, _ModelIDConv]] = {}
        self.negotiated_version: ACPVersionSpec | None = None
        self._auth_methods: list[acp.schema.AuthMethod] = []
        #: Strong references to the notifications nobody awaits; see below.
        self._pending_updates: set[asyncio.Task[None]] = set()

    def _finish_update(self, task: asyncio.Task[None]) -> None:
        self._pending_updates.discard(task)
        if task.cancelled():
            return
        if (error := task.exception()) is not None:
            logger.warning("ACP session update failed: {error}", error=error)

    def on_connect(self, conn: acp.Client) -> None:
        logger.info("ACP client connected")
        self.conn = conn

    async def initialize(
        self,
        protocol_version: int,
        client_capabilities: acp.schema.ClientCapabilities | None = None,
        client_info: acp.schema.Implementation | None = None,
        **kwargs: Any,
    ) -> acp.InitializeResponse:
        self.negotiated_version = negotiate_version(protocol_version)
        logger.info(
            "ACP server initialized with client protocol version: {version}, "
            "negotiated version: {negotiated}, "
            "client capabilities: {capabilities}, client info: {info}",
            version=protocol_version,
            negotiated=self.negotiated_version,
            capabilities=client_capabilities,
            info=client_info,
        )
        self.client_capabilities = client_capabilities

        if client_info is not None:
            from kimo_cli.telemetry import set_client_info

            set_client_info(
                name=client_info.name,
                version=getattr(client_info, "version", None),
            )

        # build the terminal-auth command for the AUTH_REQUIRED payload
        command = sys.argv[0]

        # Build terminal auth data for error response
        terminal_args = ["login"]

        # Build and cache auth methods for reuse in AUTH_REQUIRED errors
        self._auth_methods = [
            acp.schema.AuthMethod(
                id="login",
                name="Login with Kimo account",
                description=(
                    "Run `kimo login` command in the terminal, "
                    "then follow the instructions to finish login."
                ),
                # Store auth data in field_meta for building AUTH_REQUIRED error
                field_meta={
                    "terminal-auth": {
                        "command": command,
                        "args": terminal_args,
                        "label": "Kimo Login",
                        "env": {},
                        "type": "terminal",
                    }
                },
            ),
        ]

        return acp.InitializeResponse(
            protocol_version=self.negotiated_version.protocol_version,
            agent_capabilities=acp.schema.AgentCapabilities(
                load_session=True,
                prompt_capabilities=acp.schema.PromptCapabilities(
                    embedded_context=True, image=True, audio=False
                ),
                mcp_capabilities=acp.schema.McpCapabilities(http=True, sse=False),
                session_capabilities=acp.schema.SessionCapabilities(
                    list=acp.schema.SessionListCapabilities(),
                    resume=acp.schema.SessionResumeCapabilities(),
                ),
            ),
            auth_methods=self._auth_methods,
            agent_info=acp.schema.Implementation(name=NAME, version=VERSION),
        )

    @staticmethod
    def _check_token_usable() -> str | None:
        """Return ``None`` if any configured OAuth token is usable, else a reason string.

        Kimo Code OAuth was removed with the Moonshot decoupling. Authentication now
        follows the configured providers: any provider carrying an ``OAuthRef`` counts,
        and a plain API-key setup has nothing to authenticate.
        """
        config = load_config()
        refs = [p.oauth for p in config.providers.values() if p.oauth is not None]
        if not refs:
            # No OAuth-backed provider configured (e.g. a plain API-key setup).
            return None
        for ref in refs:
            token = load_tokens(ref)
            if token is None or not token.access_token:
                continue
            if token.expires_at and token.expires_at < time.time() and not token.refresh_token:
                continue
            return None
        return "no valid OAuth token found"

    def _auth_methods_payload(self) -> list[dict[str, Any]]:
        """Serialize the terminal auth methods for a JSON-RPC error payload.

        ``self._auth_methods`` holds ``acp.schema.AuthMethod`` models. Putting the
        models themselves into the error ``data`` makes the JSON-RPC encoder raise
        ``TypeError: Object of type AuthMethod is not JSON serializable``, and the
        client then receives no response at all instead of AUTH_REQUIRED.
        """
        payload: list[dict[str, Any]] = []
        for m in self._auth_methods:
            if m.field_meta and "terminal-auth" in m.field_meta:
                terminal_auth = m.field_meta["terminal-auth"]
                payload.append(
                    {
                        "id": m.id,
                        "name": m.name,
                        "description": m.description,
                        "type": terminal_auth.get("type", "terminal"),
                        "args": terminal_auth.get("args", []),
                        "env": terminal_auth.get("env", {}),
                    }
                )
        return payload

    def _check_auth(self) -> None:
        """Check if OAuth authentication is complete. Raise AUTH_REQUIRED if not."""
        reason = self._check_token_usable()
        if reason:
            logger.warning("Authentication required, {reason}", reason=reason)
            raise acp.RequestError.auth_required({"authMethods": self._auth_methods_payload()})

    async def new_session(
        self, cwd: str, mcp_servers: list[MCPServer] | None = None, **kwargs: Any
    ) -> acp.NewSessionResponse:
        logger.info("Creating new session for working directory: {cwd}", cwd=cwd)
        if self.conn is None:
            raise acp.RequestError.internal_error({"reason": "ACP client not connected"})
        if self.client_capabilities is None:
            raise acp.RequestError.internal_error({"reason": "ACP connection not initialized"})

        # Check authentication before creating session
        self._check_auth()

        session = await Session.create(KaosPath.unsafe_from_local_path(Path(cwd)))

        mcp_config = acp_mcp_servers_to_mcp_config(mcp_servers or [])
        cli_instance = await KimoCLI.create(
            session,
            mcp_configs=[mcp_config],
            ui_mode="acp",
        )
        config = cli_instance.soul.runtime.config
        acp_session: ACPSession | None = None
        try:
            acp_kaos = ACPKaos(self.conn, session.id, self.client_capabilities)
            acp_session = ACPSession(session.id, cli_instance, self.conn, kaos=acp_kaos)
            model_id_conv = _ModelIDConv(config.default_model, config.default_thinking)

            if isinstance(cli_instance.soul.agent.toolset, KimoToolset):
                replace_tools(
                    self.client_capabilities,
                    self.conn,
                    session.id,
                    cli_instance.soul.agent.toolset,
                    cli_instance.soul.runtime,
                )
        except BaseException:
            # Do not leave a half-configured entry behind: ``ACPSession.__init__``
            # already started the follow-up loop, so it must be closed either way.
            if acp_session is not None:
                await acp_session.close()
            else:
                await cli_instance.close()
            raise

        self.sessions[session.id] = (acp_session, model_id_conv)

        available_commands = [
            acp.schema.AvailableCommand(name=cmd.name, description=cmd.description)
            for cmd in soul_slash_registry.list_commands()
        ]
        # Held, and its failure reported. asyncio keeps only a weak reference,
        # so a task nobody awaits can be collected before it runs — the client
        # then never receives its command list — and an exception inside it
        # goes nowhere at all.
        update_task = asyncio.create_task(
            self.conn.session_update(
                session_id=session.id,
                update=acp.schema.AvailableCommandsUpdate(
                    session_update="available_commands_update",
                    available_commands=available_commands,
                ),
            )
        )
        self._pending_updates.add(update_task)
        update_task.add_done_callback(self._finish_update)
        return acp.NewSessionResponse(
            session_id=session.id,
            modes=acp.schema.SessionModeState(
                available_modes=[
                    acp.schema.SessionMode(
                        id="default",
                        name="Default",
                        description="The default mode.",
                    ),
                ],
                current_mode_id="default",
            ),
            models=acp.schema.SessionModelState(
                available_models=_expand_llm_models(config.models),
                current_model_id=model_id_conv.to_acp_model_id(),
            ),
        )

    async def _setup_session(
        self,
        cwd: str,
        session_id: str,
        mcp_servers: list[MCPServer] | None = None,
    ) -> tuple[ACPSession, _ModelIDConv]:
        """Load or resume a session. Shared by load_session and resume_session."""
        if self.conn is None:
            raise acp.RequestError.internal_error({"reason": "ACP client not connected"})
        if self.client_capabilities is None:
            raise acp.RequestError.internal_error({"reason": "ACP connection not initialized"})

        work_dir = KaosPath.unsafe_from_local_path(Path(cwd))
        session = await Session.find(work_dir, session_id)
        if session is None:
            logger.error(
                "Session not found: {id} for working directory: {cwd}", id=session_id, cwd=cwd
            )
            raise acp.RequestError.invalid_params({"session_id": "Session not found"})

        mcp_config = acp_mcp_servers_to_mcp_config(mcp_servers or [])
        cli_instance = await KimoCLI.create(
            session,
            mcp_configs=[mcp_config],
            resumed=True,  # _setup_session loads existing sessions
            ui_mode="acp",
        )
        acp_session: ACPSession | None = None
        try:
            config = cli_instance.soul.runtime.config
            acp_kaos = ACPKaos(self.conn, session.id, self.client_capabilities)
            acp_session = ACPSession(session.id, cli_instance, self.conn, kaos=acp_kaos)
            model_id_conv = _ModelIDConv(config.default_model, config.default_thinking)

            if isinstance(cli_instance.soul.agent.toolset, KimoToolset):
                replace_tools(
                    self.client_capabilities,
                    self.conn,
                    session.id,
                    cli_instance.soul.agent.toolset,
                    cli_instance.soul.runtime,
                )
        except BaseException:
            if acp_session is not None:
                await acp_session.close()
            else:
                await cli_instance.close()
            raise

        previous = self.sessions.get(session.id)
        self.sessions[session.id] = (acp_session, model_id_conv)
        if previous is not None:
            await previous[0].close()
        return acp_session, model_id_conv

    async def load_session(
        self, cwd: str, session_id: str, mcp_servers: list[MCPServer] | None = None, **kwargs: Any
    ) -> None:
        logger.info("Loading session: {id} for working directory: {cwd}", id=session_id, cwd=cwd)

        if session_id in self.sessions:
            logger.warning("Session already loaded: {id}", id=session_id)
            return

        # Check authentication before loading session
        self._check_auth()

        acp_session, _ = await self._setup_session(cwd, session_id, mcp_servers)
        try:
            await acp_session.replay_history(acp_session.cli.soul.runtime.session.wire_file)
        except BaseException:
            # ``BaseException`` so a cancelled replay also releases the CLI; the
            # exception is re-raised unconditionally. Closing must not mask it,
            # hence the nested guard around ``close()`` itself.
            self.sessions.pop(session_id, None)
            try:
                await acp_session.close()
            except Exception as exc:
                logger.warning(
                    "Failed to close session {id} after replay error: {error}",
                    id=session_id,
                    error=exc,
                )
            raise

    async def resume_session(
        self, cwd: str, session_id: str, mcp_servers: list[MCPServer] | None = None, **kwargs: Any
    ) -> acp.schema.ResumeSessionResponse:
        logger.info("Resuming session: {id} for working directory: {cwd}", id=session_id, cwd=cwd)

        # Same gate as new_session/load_session: resuming a not-yet-loaded session
        # creates a full CLI, so it must not bypass the AUTH_REQUIRED guidance.
        self._check_auth()

        if session_id not in self.sessions:
            await self._setup_session(cwd, session_id, mcp_servers)

        acp_session, model_id_conv = self.sessions[session_id]
        config = acp_session.cli.soul.runtime.config
        return acp.schema.ResumeSessionResponse(
            modes=acp.schema.SessionModeState(
                available_modes=[
                    acp.schema.SessionMode(
                        id="default",
                        name="Default",
                        description="The default mode.",
                    ),
                ],
                current_mode_id="default",
            ),
            models=acp.schema.SessionModelState(
                available_models=_expand_llm_models(config.models),
                current_model_id=model_id_conv.to_acp_model_id(),
            ),
        )

    async def fork_session(
        self, cwd: str, session_id: str, mcp_servers: list[MCPServer] | None = None, **kwargs: Any
    ) -> acp.schema.ForkSessionResponse:
        raise NotImplementedError

    async def list_sessions(
        self, cursor: str | None = None, cwd: str | None = None, **kwargs: Any
    ) -> acp.schema.ListSessionsResponse:
        logger.info("Listing sessions for working directory: {cwd}", cwd=cwd)
        if cwd is None:
            return acp.schema.ListSessionsResponse(sessions=[], next_cursor=None)
        work_dir = KaosPath.unsafe_from_local_path(Path(cwd))
        sessions = {s.id: s for s in await Session.list(work_dir)}
        for session_id, (acp_session, _) in self.sessions.items():
            active_session = acp_session.cli.session
            if active_session.work_dir.canonical() != work_dir.canonical():
                continue
            await active_session.refresh()
            sessions[session_id] = active_session
        return acp.schema.ListSessionsResponse(
            sessions=[
                acp.schema.SessionInfo(
                    # ``cwd`` must be absolute per the ACP schema; echoing the
                    # client's raw argument breaks that for relative inputs.
                    cwd=str(s.work_dir.canonical()),
                    session_id=s.id,
                    title=s.title,
                    updated_at=datetime.fromtimestamp(s.updated_at).astimezone().isoformat(),
                )
                for s in sorted(sessions.values(), key=lambda s: s.updated_at, reverse=True)
            ],
            next_cursor=None,
        )

    async def set_session_mode(self, mode_id: str, session_id: str, **kwargs: Any) -> None:
        if mode_id != "default":
            logger.error("Unsupported session mode: {mode_id}", mode_id=mode_id)
            raise acp.RequestError.invalid_params({"mode_id": "Only default mode is supported"})

    async def set_session_model(self, model_id: str, session_id: str, **kwargs: Any) -> None:
        logger.info(
            "Setting session model to {model_id} for session: {id}",
            model_id=model_id,
            id=session_id,
        )
        if session_id not in self.sessions:
            logger.error("Session not found: {id}", id=session_id)
            raise acp.RequestError.invalid_params({"session_id": "Session not found"})

        acp_session, current_model_id = self.sessions[session_id]
        cli_instance = acp_session.cli
        model_id_conv = _ModelIDConv.from_acp_model_id(model_id)
        if model_id_conv == current_model_id:
            return

        config = cli_instance.soul.runtime.config
        new_model = config.models.get(model_id_conv.model_key)
        if new_model is None:
            logger.error("Model not found: {model_key}", model_key=model_id_conv.model_key)
            raise acp.RequestError.invalid_params({"model_id": "Model not found"})
        new_provider = config.providers.get(new_model.provider)
        if new_provider is None:
            logger.error(
                "Provider not found: {provider} for model: {model_key}",
                provider=new_model.provider,
                model_key=model_id_conv.model_key,
            )
            raise acp.RequestError.invalid_params({"model_id": "Model's provider not found"})

        new_llm = create_llm(
            new_provider,
            new_model,
            session_id=acp_session.id,
            thinking=model_id_conv.thinking,
            oauth=cli_instance.soul.runtime.oauth,
        )
        cli_instance.soul.runtime.llm = new_llm

        config.default_model = model_id_conv.model_key
        config.default_thinking = model_id_conv.thinking
        if not config.is_from_default_location:
            logger.warning(
                "Refusing to persist model selection: config is not from the default location"
            )
            return
        config_for_save = load_config()
        config_for_save.default_model = model_id_conv.model_key
        config_for_save.default_thinking = model_id_conv.thinking
        save_config(config_for_save)

    async def authenticate(self, method_id: str, **kwargs: Any) -> acp.AuthenticateResponse | None:
        """
        For Terminal Auth, this method is typically not called directly
        (user completes auth in terminal). Implement for completeness.
        """
        if method_id == "login":
            reason = self._check_token_usable()
            if reason is None:
                logger.info("Authentication successful for method: {id}", id=method_id)
                return acp.AuthenticateResponse()
            else:
                logger.warning(
                    "Authentication not complete for method: {id} ({reason})",
                    id=method_id,
                    reason=reason,
                )
                raise acp.RequestError.auth_required(
                    {
                        "message": "Please complete login in terminal first",
                        "authMethods": self._auth_methods_payload(),
                    }
                )

        logger.error("Unknown auth method: {method_id}", method_id=method_id)
        raise acp.RequestError.invalid_params({"method_id": "Unknown auth method"})

    async def prompt(
        self, prompt: list[ACPContentBlock], session_id: str, **kwargs: Any
    ) -> acp.PromptResponse:
        logger.info("Received prompt request for session: {id}", id=session_id)
        if session_id not in self.sessions:
            logger.error("Session not found: {id}", id=session_id)
            raise acp.RequestError.invalid_params({"session_id": "Session not found"})
        acp_session, *_ = self.sessions[session_id]
        return await acp_session.prompt(prompt)

    async def cancel(self, session_id: str, **kwargs: Any) -> None:
        logger.info("Received cancel request for session: {id}", id=session_id)
        if session_id not in self.sessions:
            logger.error("Session not found: {id}", id=session_id)
            raise acp.RequestError.invalid_params({"session_id": "Session not found"})
        acp_session, *_ = self.sessions[session_id]
        await acp_session.cancel()

    async def ext_method(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        raise NotImplementedError

    async def ext_notification(self, method: str, params: dict[str, Any]) -> None:
        raise NotImplementedError


class _ModelIDConv(NamedTuple):
    model_key: str
    thinking: bool

    @classmethod
    def from_acp_model_id(cls, model_id: str) -> _ModelIDConv:
        if model_id.endswith(",thinking"):
            return _ModelIDConv(model_id[: -len(",thinking")], True)
        return _ModelIDConv(model_id, False)

    def to_acp_model_id(self) -> str:
        if self.thinking:
            return f"{self.model_key},thinking"
        return self.model_key


def _expand_llm_models(models: dict[str, LLMModel]) -> list[acp.schema.ModelInfo]:
    expanded_models: list[acp.schema.ModelInfo] = []
    for model_key, model in models.items():
        capabilities = derive_model_capabilities(model)
        if "always_thinking" in capabilities:
            # single source of truth: ``derive_model_capabilities`` already folds
            # in the case-insensitive name heuristic used elsewhere (llm.py).
            expanded_models.append(
                acp.schema.ModelInfo(
                    model_id=_ModelIDConv(model_key, True).to_acp_model_id(),
                    name=f"{model.model}",
                )
            )
        else:
            expanded_models.append(
                acp.schema.ModelInfo(
                    model_id=model_key,
                    name=model.model,
                )
            )
            if "thinking" in capabilities:
                # add thinking variant
                expanded_models.append(
                    acp.schema.ModelInfo(
                        model_id=_ModelIDConv(model_key, True).to_acp_model_id(),
                        name=f"{model.model} (thinking)",
                    )
                )
    return expanded_models
