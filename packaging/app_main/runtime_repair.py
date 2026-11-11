"""Self-heal venvstacks layer configs after the bundle is relocated.

build.py runs each layer's postinstall.py at build time, which bakes the
build machine's absolute paths into pyvenv.cfg and sitecustomize.py. Once
the DMG is installed on another machine (or the bundle is moved), those
paths are stale: the app still works because the C launcher rebuilds
PYTHONPATH at runtime, but every interpreter start prints
"Could not find platform dependent libraries <exec_prefix>" to stderr
because pyvenv.cfg's `home` no longer exists.

Detect the stale `home` and re-run the regeneration logic from each
layer's own venvstacks-generated postinstall.py (skipping its slow
bytecode-precompile step) so the configs match the installed location.
"""

from __future__ import annotations

import importlib.util
import logging
import os
from pathlib import Path

log = logging.getLogger(__name__)


def _pyvenv_home(pyvenv_cfg: Path) -> Path | None:
    try:
        # UnicodeDecodeError too: a pyvenv.cfg that is not valid UTF-8 (a
        # hand-edit, a truncated write) raised straight out of main() at
        # __main__.py's repair call — before the menu-bar item exists, so the
        # app died at launch with nothing on screen.
        for line in pyvenv_cfg.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "home":
                return Path(value.strip())
    except (OSError, UnicodeDecodeError):
        return None
    return None


def _write_atomic(target: Path, contents: str) -> None:
    """Write via a temporary file and os.replace.

    write_text() truncates first, so a crash or a full disk between truncate
    and write leaves a half-written pyvenv.cfg or sitecustomize.py — and those
    two files are what the interpreter reads at startup. Either the repair
    lands whole or the stale-but-working original stays.
    """
    tmp = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(contents, encoding="utf-8")
        os.replace(tmp, target)
    finally:
        tmp.unlink(missing_ok=True)


def _regenerate_layer(layer: Path, script: Path) -> None:
    spec = importlib.util.spec_from_file_location(f"_venvstacks_postinstall_{layer.name}", script)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {script}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    config = module.load_layer_config(layer)
    # Generate both before writing either. These two files are read together at
    # interpreter startup, so a failure between them leaves a new pyvenv.cfg
    # against a stale sitecustomize.py — a mixture that was never shipped and
    # is worse than the stale pair the repair was trying to fix. Generation is
    # the part that can fail on its own; the writes are atomic replaces.
    venv_config = module.generate_pyvenv_cfg(config["base_python_path"], config["py_version"])
    sc_contents = module.generate_sitecustomize(config["pylib_paths"], config["dynlib_paths"])

    _write_atomic(layer / "pyvenv.cfg", venv_config)
    if sc_contents is not None:
        _write_atomic(config["site_path"] / "sitecustomize.py", sc_contents)


def repair_layer_configs(runtimes: Path) -> None:
    """Regenerate pyvenv.cfg/sitecustomize.py for layers whose baked paths are stale.

    Never raises: a failed repair leaves the harmless startup warning in place,
    which beats blocking the app (e.g. when the bundle dir is not writable).
    """
    # The whole walk is guarded, not just _regenerate_layer. iterdir() can
    # raise PermissionError and the is_file()/is_dir() probes can raise OSError
    # on a broken mount — both from outside the old try, so they escaped a
    # function whose docstring promises it never raises, at a point in launch
    # before any UI exists.
    try:
        if not runtimes.is_dir():
            return
        layers = sorted(runtimes.iterdir())
    except OSError:
        log.exception("could not enumerate venvstacks layers under %s", runtimes)
        return
    for layer in layers:
        try:
            pyvenv_cfg = layer / "pyvenv.cfg"
            script = layer / "postinstall.py"
            if not pyvenv_cfg.is_file() or not script.is_file():
                continue
            home = _pyvenv_home(pyvenv_cfg)
            if home is None or home.is_dir():
                continue
            _regenerate_layer(layer, script)
            log.info("repaired relocated venvstacks layer: %s", layer.name)
        except Exception:
            log.exception("could not repair venvstacks layer %s", layer.name)
