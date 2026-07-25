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
from pathlib import Path

log = logging.getLogger(__name__)


def _pyvenv_home(pyvenv_cfg: Path) -> Path | None:
    try:
        for line in pyvenv_cfg.read_text(encoding="utf-8").splitlines():
            key, _, value = line.partition("=")
            if key.strip() == "home":
                return Path(value.strip())
    except OSError:
        return None
    return None


def _regenerate_layer(layer: Path, script: Path) -> None:
    spec = importlib.util.spec_from_file_location(f"_venvstacks_postinstall_{layer.name}", script)
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {script}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    config = module.load_layer_config(layer)
    venv_config = module.generate_pyvenv_cfg(config["base_python_path"], config["py_version"])
    (layer / "pyvenv.cfg").write_text(venv_config, encoding="utf-8")

    sc_contents = module.generate_sitecustomize(config["pylib_paths"], config["dynlib_paths"])
    if sc_contents is not None:
        (config["site_path"] / "sitecustomize.py").write_text(sc_contents, encoding="utf-8")


def repair_layer_configs(runtimes: Path) -> None:
    """Regenerate pyvenv.cfg/sitecustomize.py for layers whose baked paths are stale.

    Never raises: a failed repair leaves the harmless startup warning in place,
    which beats blocking the app (e.g. when the bundle dir is not writable).
    """
    if not runtimes.is_dir():
        return
    for layer in sorted(runtimes.iterdir()):
        pyvenv_cfg = layer / "pyvenv.cfg"
        script = layer / "postinstall.py"
        if not pyvenv_cfg.is_file() or not script.is_file():
            continue
        home = _pyvenv_home(pyvenv_cfg)
        if home is None or home.is_dir():
            continue
        try:
            _regenerate_layer(layer, script)
            log.info("repaired relocated venvstacks layer: %s", layer.name)
        except Exception:
            log.exception("could not repair venvstacks layer %s", layer.name)
