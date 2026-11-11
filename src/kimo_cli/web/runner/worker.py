"""Worker module for running KimoCLI in a subprocess.

This module is the entry point for the subprocess that runs KimoCLI in wire mode.
It reads the session configuration from disk and runs KimoCLI.run_wire_stdio().

Usage:
    python -m kimo_cli.web.runner.worker <session_id>
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
import traceback
from pathlib import Path
from typing import Any, cast
from uuid import UUID

from kimo_cli import logger
from kimo_cli.app import KimoCLI, enable_logging
from kimo_cli.cli.mcp import get_global_mcp_config_file
from kimo_cli.exception import MCPConfigError
from kimo_cli.session_lease import claim_session_lease
from kimo_cli.utils.logging import open_original_stderr
from kimo_cli.web.store.sessions import load_session_by_id

#: No attempt limit. Someone is watching a web session, with a stop button:
#: a rate limit or an unwell provider is retried until it clears or they stop
#: it, rather than ending the reply after a fixed count. Errors that cannot
#: clear on their own are not retried at all, so this never spins on them.
WEB_MAX_RETRIES_PER_STEP = 0


def configure_session_environment(session: Any) -> None:
    """Configure paths whose values depend on the selected work directory."""
    output_dir = Path(str(session.work_dir)) / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    os.environ["KIMO_OUTPUT_DIR"] = str(output_dir)


def read_session_overrides(session_dir: Path) -> dict[str, Any]:
    """Read per-session overrides from ``session_config.json``.

    Returns an empty dict when the file is missing, unreadable, or not a
    JSON object. Recognized keys: ``thinking``, ``model``, ``agent_spec_path``.
    """
    cfg_file = session_dir / "session_config.json"
    if not cfg_file.exists():
        return {}
    try:
        cfg: Any = json.loads(cfg_file.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(cfg, dict):
        return {}
    return cast("dict[str, Any]", cfg)


async def run_worker(session_id: UUID) -> None:
    """Run the KimoCLI worker for a session."""
    # Find session by ID using the web store
    joint_session = load_session_by_id(session_id)
    if joint_session is None:
        raise ValueError(f"Session not found: {session_id}")

    # Get the kimo-cli session object
    session = joint_session.kimo_cli_session
    configure_session_environment(session)

    # Load default MCP config file if it exists
    default_mcp_file = get_global_mcp_config_file()
    mcp_configs: list[dict[str, Any]] = []
    if default_mcp_file.exists():
        raw = default_mcp_file.read_text(encoding="utf-8")
        try:
            mcp_configs = [json.loads(raw)]
        except json.JSONDecodeError:
            logger.warning(
                "Invalid JSON in MCP config file: {path}",
                path=default_mcp_file,
            )

    # Detect whether this is a resumed session (has prior state on disk)
    # vs a brand-new session that should honor config.default_plan_mode.
    resumed = (session.dir / "state.json").exists()

    # Claim the session epoch lease. If a previous worker for this session is
    # somehow still alive (gateway restarted without killing it), its next
    # session-file write fails its lease check and it stops recording instead
    # of forking the transcript. Old sessions start at epoch 1 here.
    session_lease = claim_session_lease(session.dir)
    session.wire_file.lease = session_lease

    # Read per-session config (thinking override, model override, agent spec
    # path); None → falls back to global config / default agent.
    _cfg = read_session_overrides(session.dir)
    session_thinking: bool | None = _cfg.get("thinking")
    session_model: str | None = None
    _model = _cfg.get("model")
    if isinstance(_model, str) and _model:
        session_model = _model
    agent_file: Path | None = None
    _agent_spec_path: str | None = _cfg.get("agent_spec_path")
    if _agent_spec_path:
        _path = Path(_agent_spec_path)
        if not _path.is_file():
            raise FileNotFoundError(
                f"Agent spec file recorded in session_config.json no longer exists: {_path}"
            )
        agent_file = _path

    # Create KimoCLI instance with MCP configuration
    try:
        kimo_cli = await KimoCLI.create(
            session,
            mcp_configs=mcp_configs or None,
            resumed=resumed,
            ui_mode="wire",
            model_name=session_model,
            thinking=session_thinking,
            agent_file=agent_file,
            max_retries_per_step=WEB_MAX_RETRIES_PER_STEP,
            session_lease=session_lease,
        )
    except MCPConfigError as exc:
        logger.warning(
            "Invalid MCP config in {path}: {error}. Starting without MCP.",
            path=default_mcp_file,
            error=exc,
        )
        kimo_cli = await KimoCLI.create(
            session,
            mcp_configs=None,
            resumed=resumed,
            ui_mode="wire",
            model_name=session_model,
            thinking=session_thinking,
            agent_file=agent_file,
            max_retries_per_step=WEB_MAX_RETRIES_PER_STEP,
            session_lease=session_lease,
        )

    # Run in wire stdio mode. The worker owns the root runtime and must close
    # its Wiki index even when the stdio transport fails or is cancelled.
    try:
        await kimo_cli.run_wire_stdio()
    finally:
        # Archive before closing, while the soul still has its history.
        #
        # The terminal CLI has done this in its own `finally` since the memory
        # system landed; the worker never did, so in the desktop app a session
        # that did not happen to compact left nothing behind — no summary and
        # no proposal. A real store showed it plainly: nine summaries, every
        # one triggered by compaction, none by a session ending.
        #
        # It costs one summarisation request per session end, which is what the
        # CLI already pays. Fail-open and time-boxed for the same reason it is
        # there: a slow or broken summariser must not keep the process alive.
        with contextlib.suppress(Exception, asyncio.CancelledError):
            from kimo_cli.memory.archivist import archive_on_session_end

            await asyncio.wait_for(archive_on_session_end(kimo_cli.soul), timeout=10)

        await kimo_cli.close()


def report_fatal(text: str) -> None:
    """Get a crash traceback out of the worker before the process dies.

    ``enable_logging`` redirects fd=2 into loguru, drained by a daemon thread
    that the interpreter kills during shutdown — so a traceback printed by the
    default excepthook usually loses everything after its first line, in the
    log as well as on the pipe. Write it ourselves instead, synchronously:
    to the stderr saved before the redirect, which is the pipe ``SessionProcess``
    reads and surfaces in the UI, and to the log for anyone reading it later.
    """
    logger.error("Worker crashed:\n{text}", text=text)
    with open_original_stderr() as stream:
        if stream is not None:
            stream.write(text.encode("utf-8", errors="replace"))
            stream.flush()


def main() -> None:
    """Entry point for the worker subprocess."""
    from kimo_cli.utils.proctitle import set_process_title
    from kimo_cli.utils.proxy import normalize_proxy_env

    normalize_proxy_env()
    set_process_title("kimo-cli-worker")

    if len(sys.argv) < 2:
        print("Usage: python -m kimo_cli.web.runner.worker <session_id>", file=sys.stderr)
        sys.exit(1)

    try:
        session_id = UUID(sys.argv[1])
    except ValueError:
        print(f"Invalid session ID: {sys.argv[1]}", file=sys.stderr)
        sys.exit(1)

    # Enable logging for the subprocess
    enable_logging(debug=False)

    # Run the async worker
    try:
        asyncio.run(run_worker(session_id))
    except Exception:
        report_fatal(traceback.format_exc())
        sys.exit(1)


if __name__ == "__main__":
    main()
