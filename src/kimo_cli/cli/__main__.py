from __future__ import annotations

import sys
from collections.abc import Sequence

from kimo_cli.cli import cli


def main(argv: Sequence[str] | None = None) -> int | str | None:
    from kimo_cli.telemetry.crash import install_crash_handlers, set_phase
    from kimo_cli.utils.environment import GitBashNotFoundError
    from kimo_cli.utils.proxy import normalize_proxy_env

    # Same entry treatment as kimo_cli.__main__: install excepthook before
    # anything else so startup-phase crashes in subcommand subprocesses
    # (background-task-worker, __web-worker, acp via toad) are captured.
    install_crash_handlers()
    normalize_proxy_env()

    try:
        result = cli() if argv is None else cli(args=list(argv))
    except SystemExit as exc:
        result = exc.code
    except GitBashNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        result = 1
    except BaseException:
        # Leave the phase untouched: the crash hook must report the bucket the
        # failure happened in, not "shutdown".
        raise
    # ``shutdown`` means "the entry point finished". Setting it from a
    # ``finally`` labelled escaping exceptions as shutdown-phase, which is how
    # this bit was losing the startup/runtime bucket.
    set_phase("shutdown")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
