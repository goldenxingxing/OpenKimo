from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass
from typing import Any, Literal

from kimo_cli import logger


def _as_dict(value: Any) -> dict[str, Any]:
    """Return `value` if it is a mapping, else an empty one.

    Hook stdout is third-party data: `hookSpecificOutput` may be absent, null,
    a string or a list.
    """
    return value if isinstance(value, dict) else {}


@dataclass
class HookResult:
    """Result of a single hook execution."""

    action: Literal["allow", "block"] = "allow"
    reason: str = ""
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    timed_out: bool = False
    additional_context: str = ""
    """``hookSpecificOutput.additionalContext``: text for the model, not a decision."""


async def run_hook(
    command: str,
    input_data: dict[str, Any],
    *,
    timeout: int = 30,
    cwd: str | None = None,
) -> HookResult:
    """Execute a single hook command. Fail-open: errors/timeouts -> allow."""
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=cwd,
        )
        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                proc.communicate(input=json.dumps(input_data).encode()),
                timeout=timeout,
            )
        except TimeoutError:
            logger.warning("Hook timed out after {}s: {}", timeout, command)
            return HookResult(action="allow", timed_out=True)
        finally:
            # Reap the child on *every* exit path, not only on timeout/cancel:
            # an encoding or pipe error used to leave the hook running with open
            # pipes. ``communicate`` on the success path has already reaped it.
            if proc.returncode is None:
                with contextlib.suppress(ProcessLookupError):
                    proc.kill()
                with contextlib.suppress(asyncio.CancelledError):
                    await proc.wait()
    except Exception as e:
        logger.warning("Hook failed: {}: {}", command, e)
        return HookResult(action="allow", stderr=str(e))

    stdout = stdout_bytes.decode(errors="replace")
    stderr = stderr_bytes.decode(errors="replace")
    exit_code = proc.returncode or 0

    # Exit 2 = block
    if exit_code == 2:
        return HookResult(
            action="block",
            reason=stderr.strip(),
            stdout=stdout,
            stderr=stderr,
            exit_code=2,
        )

    # Exit 0 + JSON stdout = structured decision
    if exit_code == 0 and stdout.strip():
        try:
            raw = json.loads(stdout)
            if isinstance(raw, dict):
                parsed: dict[str, Any] = raw
                # A hook may emit `"hookSpecificOutput": null` (or any other
                # non-object). `cast()` is a type hint only, so trusting it here
                # used to raise AttributeError on `.get()` — outside the
                # fail-open guard above — and break the caller instead of
                # allowing the tool call.
                hook_output: dict[str, Any] = _as_dict(parsed.get("hookSpecificOutput"))
                if hook_output.get("permissionDecision") == "deny":
                    return HookResult(
                        action="block",
                        reason=str(hook_output.get("permissionDecisionReason", "")),
                        stdout=stdout,
                        stderr=stderr,
                        exit_code=0,
                    )
                if parsed.get("decision") == "block":
                    return HookResult(
                        action="block",
                        reason=str(parsed.get("reason", "")),
                        stdout=stdout,
                        stderr=stderr,
                        exit_code=0,
                    )
                additional_context = hook_output.get("additionalContext")
                if isinstance(additional_context, str) and additional_context.strip():
                    return HookResult(
                        action="allow",
                        stdout=stdout,
                        stderr=stderr,
                        exit_code=0,
                        additional_context=additional_context.strip(),
                    )
        except (json.JSONDecodeError, TypeError):
            pass

    return HookResult(action="allow", stdout=stdout, stderr=stderr, exit_code=exit_code)
