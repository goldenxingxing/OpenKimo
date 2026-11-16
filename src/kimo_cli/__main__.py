from __future__ import annotations

import sys
from collections.abc import Sequence
from pathlib import Path


def _prog_name() -> str:
    return Path(sys.argv[0]).name or "kimo"


def main(argv: Sequence[str] | None = None) -> int | str | None:
    from kimo_cli.telemetry.crash import install_crash_handlers, set_phase
    from kimo_cli.utils.proxy import normalize_proxy_env

    # Install excepthook before anything else so startup-phase crashes are captured.
    install_crash_handlers()
    normalize_proxy_env()

    args = list(sys.argv[1:] if argv is None else argv)

    if len(args) == 1 and args[0] in {"--version", "-V"}:
        from kimo_cli.constant import get_version

        print(f"kimo, version {get_version()}")
        return 0

    from kimo_cli.cli import cli
    from kimo_cli.utils.environment import GitBashNotFoundError

    try:
        result = cli(args=args, prog_name=_prog_name())
    except SystemExit as exc:
        result = exc.code
    except GitBashNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        result = 1
    except BaseException:
        # Same reasoning as ``kimo_cli/cli/__main__.py``: an escaping exception
        # must keep the phase it failed in, so the crash hook reports the truth.
        raise
    set_phase("shutdown")
    return result


if __name__ == "__main__":
    raise SystemExit(main())
