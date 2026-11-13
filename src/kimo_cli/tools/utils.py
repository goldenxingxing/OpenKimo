import re
from pathlib import Path

from jinja2 import Environment, Undefined
from kosong.tooling import BriefDisplayBlock, DisplayBlock, ToolError, ToolReturnValue
from kosong.utils.typing import JsonType

from kimo_cli.soul.offload import OffloadSink, ToolOutputOffloader, current_offloader


class _KeepPlaceholderUndefined(Undefined):
    def __str__(self) -> str:
        if self._undefined_name is None:
            return ""
        return f"${{{self._undefined_name}}}"

    __repr__ = __str__


def load_desc(path: Path, context: dict[str, object] | None = None) -> str:
    """Load a tool description from a file, rendered via Jinja2."""
    description = path.read_text(encoding="utf-8")
    env = Environment(
        keep_trailing_newline=True,
        lstrip_blocks=True,
        trim_blocks=True,
        variable_start_string="${",
        variable_end_string="}",
        undefined=_KeepPlaceholderUndefined,
    )
    template = env.from_string(description)
    return template.render(context or {})


def truncate_line(line: str, max_length: int, marker: str = "...") -> str:
    """
    Truncate a line if it exceeds `max_length`, preserving the beginning and the line break.
    """
    return _truncate_line(line, max_length, marker)[0]


def _truncate_line(line: str, max_length: int, marker: str = "...") -> tuple[str, int]:
    """Truncate a line and report how many leading characters of it were kept.

    Callers that need the dropped tail (the offload path) get the cut position
    directly instead of re-deriving it from the marker/length layout.
    """
    if len(line) <= max_length:
        return line, len(line)

    # Find line breaks at the end of the line
    m = re.search(r"[\r\n]+$", line)
    linebreak = m.group(0) if m else ""
    content_length = max_length - len(marker) - len(linebreak)
    if content_length < 0:
        return line[:max_length], max_length
    return line[:content_length] + marker + linebreak, content_length


# Default output limits
DEFAULT_MAX_CHARS = 50_000
DEFAULT_MAX_LINE_LENGTH = 2000


class ToolResultBuilder:
    """
    Builder for tool results with character and line limits.

    When an ``offloader`` is given, output past the character budget is
    streamed to disk instead of being dropped: the sink is seeded with what
    already made it into the buffer, so the file holds the full output and
    the final message points at it.
    """

    def __init__(
        self,
        max_chars: int = DEFAULT_MAX_CHARS,
        max_line_length: int | None = DEFAULT_MAX_LINE_LENGTH,
        offloader: ToolOutputOffloader | None = None,
        tool_name: str = "",
    ):
        self.max_chars = max_chars
        self.max_line_length = max_line_length
        self._marker = "[...truncated]"
        if max_line_length is not None and max_line_length <= len(self._marker):
            # Not an ``assert``: this runs for real callers, and ``-O`` would
            # strip the check while truncate_line silently misbehaves.
            raise ValueError("max_line_length must exceed the truncation marker length")
        self._buffer: list[str] = []
        self._offload_seed: list[str] = []
        self._n_chars = 0
        self._n_lines = 0
        self._truncation_happened = False
        self._display: list[DisplayBlock] = []
        self._extras: dict[str, JsonType] | None = None
        # An explicit offloader wins; otherwise fall back to the one the
        # toolset bound to the in-flight tool call (contextvar), so builtin
        # tools offload without changing their construction sites.
        self._offloader = offloader if offloader is not None else current_offloader.get()
        self._tool_name = tool_name
        self._sink: OffloadSink | None = None
        self._sink_path: Path | None = None

    @property
    def is_full(self) -> bool:
        """Check if output buffer is full due to character limit."""
        return self._n_chars >= self.max_chars

    @property
    def n_chars(self) -> int:
        """Get current character count."""
        return self._n_chars

    @property
    def n_lines(self) -> int:
        """Get current line count."""
        return self._n_lines

    def write(self, text: str) -> int:
        """
        Write text to the output buffer.

        Returns:
            int: Number of characters actually written
        """
        if self.is_full:
            self._offload(text)
            return 0

        lines = text.splitlines(keepends=True)
        if not lines:
            return 0

        chars_written = 0
        overflow: list[str] = []

        for line in lines:
            if self.is_full:
                overflow.append(line)
                continue

            original_line = line
            remaining_chars = self.max_chars - self._n_chars
            limit = (
                min(remaining_chars, self.max_line_length)
                if self.max_line_length is not None
                else remaining_chars
            )
            line, kept = _truncate_line(line, limit, self._marker)
            if kept < len(original_line):
                self._truncation_happened = True
                # The tail truncate_line dropped also goes to the offload file,
                # so the on-disk copy is the full output, not a holed one.
                if self._offloader is not None:
                    overflow.append(original_line[kept:])

            self._offload_seed.append(original_line[:kept])
            self._buffer.append(line)
            chars_written += len(line)
            self._n_chars += len(line)
            if line.endswith("\n"):
                self._n_lines += 1

        if overflow:
            self._offload("".join(overflow))
        return chars_written

    def _offload(self, text: str) -> None:
        """Stream overflow to disk: the sink is seeded with the buffered part
        on first use so the file holds the full output, not just the tail."""
        if not text:
            return
        self._truncation_happened = True
        if self._offloader is None:
            return
        if self._sink is None:
            self._sink = self._offloader.new_sink(tool_name=self._tool_name)
            self._sink.write("".join(self._offload_seed))
            self._offload_seed.clear()
        self._sink.write(text)

    def _close_sink(self) -> None:
        if self._sink is not None:
            self._sink_path = self._sink.close()
            self._sink = None

    def tail(self, max_lines: int = 5, max_line_len: int = 200) -> str:
        """Return the last non-empty lines from the buffer, joined with newlines.

        Useful for surfacing actionable error context (stderr) in tool result briefs.
        """
        collected: list[str] = []
        for chunk in reversed(self._buffer):
            for line in reversed(chunk.splitlines()):
                stripped = line.rstrip()
                if not stripped.strip():
                    continue
                if len(stripped) > max_line_len:
                    stripped = stripped[:max_line_len] + "..."
                collected.append(stripped)
                if len(collected) >= max_lines:
                    break
            if len(collected) >= max_lines:
                break
        return "\n".join(reversed(collected))

    def display(self, *blocks: DisplayBlock) -> None:
        """Add display blocks to the tool result."""
        self._display.extend(blocks)

    def extras(self, **extras: JsonType) -> None:
        """Add extra data to the tool result."""
        if self._extras is None:
            self._extras = {}
        self._extras.update(extras)

    def ok(self, message: str = "", *, brief: str = "") -> ToolReturnValue:
        """Create a ToolReturnValue with is_error=False and the current output."""
        self._close_sink()
        output = "".join(self._buffer)

        final_message = message
        if final_message and not final_message.endswith("."):
            final_message += "."
        truncation_msg = "Output is truncated to fit in the message."
        if self._sink_path is not None:
            truncation_msg += (
                f" Full output saved to: {self._sink_path}"
                " (Read the file if you need the truncated parts)."
            )
        if self._truncation_happened:
            if final_message:
                final_message += f" {truncation_msg}"
            else:
                final_message = truncation_msg
        return ToolReturnValue(
            is_error=False,
            output=output,
            message=final_message,
            display=([BriefDisplayBlock(text=brief)] if brief else []) + self._display,
            extras=self._extras,
        )

    def error(self, message: str, *, brief: str) -> ToolReturnValue:
        """Create a ToolReturnValue with is_error=True and the current output."""
        self._close_sink()
        output = "".join(self._buffer)

        final_message = message
        if self._truncation_happened:
            truncation_msg = "Output is truncated to fit in the message."
            if self._sink_path is not None:
                truncation_msg += (
                    f" Full output saved to: {self._sink_path}"
                    " (Read the file if you need the truncated parts)."
                )
            if final_message:
                final_message += f" {truncation_msg}"
            else:
                final_message = truncation_msg

        return ToolReturnValue(
            is_error=True,
            output=output,
            message=final_message,
            display=([BriefDisplayBlock(text=brief)] if brief else []) + self._display,
            extras=self._extras,
        )


class ToolRejectedError(ToolError):
    has_feedback: bool = False
    #: Whether this refusal ends the turn.
    #:
    #: True for the actions the agent was asked to perform: declining ``rm -rf``
    #: means the plan is wrong, and carrying on would be carrying on without
    #: the user. False for the ones it decided to perform *while* doing
    #: something else — recording a memory, writing a wiki page. Declining one
    #: of those means "do not save that", not "abandon the task", and ending
    #: the turn there leaves the actual work half-finished.
    stops_turn: bool = True

    def __init__(
        self,
        message: str | None = None,
        brief: str = "Rejected by user",
        has_feedback: bool = False,
        stops_turn: bool = True,
    ):
        super().__init__(
            message=message
            or (
                "The tool call is rejected by the user. "
                "Stop what you are doing and wait for the user to tell you how to proceed."
            ),
            brief=brief,
        )
        self.has_feedback = has_feedback
        self.stops_turn = stops_turn
