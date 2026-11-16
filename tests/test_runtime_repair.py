"""Tests for app_main.runtime_repair (relocated venvstacks config healing)."""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "packaging"))

from app_main import runtime_repair  # noqa: E402

# Minimal stand-in for the venvstacks-generated postinstall.py: same public
# functions runtime_repair calls, deriving paths from the layer location.
_POSTINSTALL_STUB = """
from pathlib import Path

def load_layer_config(layer_path):
    base = layer_path.parent / "cpython-3.12"
    return {
        "base_python_path": base / "bin" / "python",
        "py_version": "3.12.13",
        "pylib_paths": [base / "lib" / "python3.12" / "site-packages"],
        "dynlib_paths": [],
        "site_path": layer_path / "lib" / "python3.12" / "site-packages",
    }

def generate_pyvenv_cfg(base_python_path, py_version):
    home = base_python_path.parent
    return f"home = {home}\\nversion = {py_version}\\n"

def generate_sitecustomize(pylib_paths, dynlib_paths, *, include_missing_dynlib_paths=False):
    lines = ["from site import addsitedir"]
    lines += [f"addsitedir({str(p)!r})" for p in pylib_paths]
    return "\\n".join(lines) + "\\n"
"""


def _make_layer(runtimes: Path, name: str, home: Path) -> Path:
    layer = runtimes / name
    (layer / "lib" / "python3.12" / "site-packages").mkdir(parents=True)
    (layer / "postinstall.py").write_text(_POSTINSTALL_STUB, encoding="utf-8")
    (layer / "pyvenv.cfg").write_text(f"home = {home}\nversion = 3.12.13\n", encoding="utf-8")
    return layer


def test_stale_home_is_repaired(tmp_path):
    runtimes = tmp_path / "runtimes"
    (runtimes / "cpython-3.12" / "bin").mkdir(parents=True)
    layer = _make_layer(runtimes, "app-app-test", tmp_path / "no" / "such" / "dir")

    runtime_repair.repair_layer_configs(runtimes)

    cfg = (layer / "pyvenv.cfg").read_text(encoding="utf-8")
    assert str(runtimes / "cpython-3.12" / "bin") in cfg
    sc = layer / "lib" / "python3.12" / "site-packages" / "sitecustomize.py"
    assert "addsitedir" in sc.read_text(encoding="utf-8")


def test_valid_home_is_untouched(tmp_path):
    runtimes = tmp_path / "runtimes"
    valid_home = runtimes / "cpython-3.12" / "bin"
    valid_home.mkdir(parents=True)
    layer = _make_layer(runtimes, "app-app-test", valid_home)
    before = (layer / "pyvenv.cfg").read_text(encoding="utf-8")

    runtime_repair.repair_layer_configs(runtimes)

    assert (layer / "pyvenv.cfg").read_text(encoding="utf-8") == before
    assert not (layer / "lib" / "python3.12" / "site-packages" / "sitecustomize.py").exists()


def test_broken_postinstall_does_not_raise(tmp_path):
    runtimes = tmp_path / "runtimes"
    layer = _make_layer(runtimes, "app-app-test", tmp_path / "gone")
    (layer / "postinstall.py").write_text("raise RuntimeError('boom')", encoding="utf-8")

    runtime_repair.repair_layer_configs(runtimes)  # must not raise

    # Original (stale) config left in place.
    assert "gone" in (layer / "pyvenv.cfg").read_text(encoding="utf-8")


def test_missing_runtimes_dir_is_noop(tmp_path):
    runtime_repair.repair_layer_configs(tmp_path / "does-not-exist")  # must not raise
