import logging
import re
import subprocess
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

#: Fallback screen size, matching the Xvfb geometry start-sandbox.sh creates
#: (`Xvfb :99 -screen 0 1920x1080x24`). Used when xrandr cannot be read.
DEFAULT_SCREEN_SIZE = (1920, 1080)

#: `xrandr` prints e.g. "Screen 0: minimum 1 x 1, current 1920 x 1080, ...".
_CURRENT_RE = re.compile(r"current\s+(\d+)\s*x\s*(\d+)")


def get_screensize() -> Tuple[int, int]:
    """Screen size of the primary display, or DEFAULT_SCREEN_SIZE.

    Never raises. It used to shell out to `xrandr | grep -oP ... | tr -d ' '`,
    ignore the exit code, and do `tuple(map(int, out.split("x")))` on whatever
    came back — so a headless or Wayland session, a missing xrandr, a BusyBox
    grep without -P, or simply a transient Xvfb blip produced `int("")` and a
    ValueError. Both callers are in browser_guard's launch path, where that
    ValueError aborted the launch and killed a browser that had started fine.
    A screen size is a layout hint; failing to read one is not worth an
    exception.
    """
    try:
        proc = subprocess.run(
            ["xrandr"],
            capture_output=True,
            text=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("xrandr unavailable (%s); using %s", exc, DEFAULT_SCREEN_SIZE)
        return DEFAULT_SCREEN_SIZE

    if proc.returncode != 0:
        logger.warning(
            "xrandr exited %s (%s); using %s",
            proc.returncode,
            (proc.stderr or "").strip()[:200],
            DEFAULT_SCREEN_SIZE,
        )
        return DEFAULT_SCREEN_SIZE

    match = _CURRENT_RE.search(proc.stdout or "")
    if not match:
        logger.warning(
            "no 'current W x H' in xrandr output; using %s", DEFAULT_SCREEN_SIZE
        )
        return DEFAULT_SCREEN_SIZE
    return int(match.group(1)), int(match.group(2))


def run_command(
    command: List[str], timeout: int = 30, pipe_output: bool = True
) -> subprocess.CompletedProcess[str]:
    """Run a shell command and return the output, error and exit code."""
    result = subprocess.run(
        command,
        text=True,
        timeout=timeout,
        stdout=subprocess.PIPE if pipe_output else None,
        stderr=subprocess.PIPE if pipe_output else None,
    )
    return result


def run_command_background(
    command: List[str], log_path: Optional[str] = None
) -> subprocess.Popen:
    """Start a command in the background and return the process.

    Output goes to ``log_path`` when given, otherwise to DEVNULL — never to a
    pipe. Piping both streams and never reading them deadlocks the child as
    soon as it writes past the OS pipe buffer (~64 KiB): it blocks in write()
    forever, which for a browser means hanging mid-startup while every
    liveness probe reports failure. A caller that wants the output should pass
    a path and read the file.
    """
    if log_path is not None:
        sink = open(log_path, "ab", buffering=0)  # noqa: SIM115 - owned by the child
        try:
            return subprocess.Popen(command, stdout=sink, stderr=subprocess.STDOUT)
        except BaseException:
            # The child owns the handle only once it exists. A command that is
            # not on PATH raises here, and the descriptor stays open for the
            # life of the process — this is a relaunch path, so it is opened
            # again on every retry. BaseException rather than OSError on
            # purpose: the handle has no owner on *any* way out of this line,
            # a KeyboardInterrupt during spawn included, and it is re-raised
            # either way.
            sink.close()
            raise
    return subprocess.Popen(
        command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
    )
