"""The installed app must not depend on the tree it was built in.

venvstacks' postinstall writes absolute paths into ``pyvenv.cfg`` and
``sitecustomize.py``. They are right where the build ran and wrong the moment
the .app is copied to /Applications, which is every install: the installed app
kept loading its framework layer out of the build directory, so deleting or
moving the repo left it unable to import ``acp`` at all. The failure was
invisible for as long as the build tree happened to survive.
"""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BUILD_PY = ROOT / "packaging" / "build.py"


def _build_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("packaging_build_reloc", BUILD_PY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["packaging_build_reloc"] = module
    spec.loader.exec_module(module)
    return module


def _layers(root: Path, build_prefix: Path) -> Path:
    """A miniature two-layer export, with the absolute paths venvstacks writes."""
    runtimes = root / "runtimes"
    base = runtimes / "cpython-3.12"
    app = runtimes / "app-app-demo"
    for layer in (base, app):
        (layer / "lib/python3.12/site-packages").mkdir(parents=True)
        (layer / "share/venv/metadata").mkdir(parents=True)
        (layer / "bin").mkdir(parents=True)
    (base / "bin" / "python").write_text("#!/bin/sh\n")

    (app / "pyvenv.cfg").write_text(
        f"home = {build_prefix}/runtimes/cpython-3.12/bin\n"
        "include-system-site-packages = false\n"
        "version = 3.12.13\n"
        f"executable = {build_prefix}/runtimes/cpython-3.12/bin/python\n"
    )
    (app / "share/venv/metadata/venvstacks_layer.json").write_text(
        json.dumps(
            {
                "python": "bin/python",
                "py_version": "3.12.13",
                "base_python": "../cpython-3.12/bin/python",
                "site_dir": "lib/python3.12/site-packages",
                "pylib_dirs": ["../cpython-3.12/lib/python3.12/site-packages"],
                "dynlib_dirs": [],
            }
        )
    )
    (app / "lib/python3.12/site-packages/sitecustomize.py").write_text(
        "from site import addsitedir\n"
        f"addsitedir('{build_prefix}/runtimes/cpython-3.12/lib/python3.12/site-packages')\n"
    )
    return runtimes


def test_pyvenv_cfg_stops_naming_the_build_tree(tmp_path: Path) -> None:
    build = _build_module()
    build_tree = tmp_path / "build"
    runtimes = _layers(build_tree, build_tree)

    build.make_layers_relocatable(runtimes)

    cfg = (runtimes / "app-app-demo" / "pyvenv.cfg").read_text()
    assert str(build_tree) not in cfg
    assert f"home = {os.path.join('..', 'cpython-3.12', 'bin')}" in cfg
    assert f"executable = {os.path.join('..', 'cpython-3.12', 'bin', 'python')}" in cfg
    # Untouched keys survive: a venv without `version` is not a venv.
    assert "version = 3.12.13" in cfg


def test_sitecustomize_follows_the_bundle_when_it_moves(tmp_path: Path) -> None:
    """The point of the whole exercise: resolve against __file__, not the build."""
    build = _build_module()
    build_tree = tmp_path / "build"
    runtimes = _layers(build_tree, build_tree)

    build.make_layers_relocatable(runtimes)

    moved = tmp_path / "Applications" / "OpenKimo.app" / "Contents" / "Resources"
    moved.parent.mkdir(parents=True)
    (tmp_path / "build" / "runtimes").rename(moved.parent / "runtimes")
    sitecustomize = (
        moved.parent / "runtimes/app-app-demo/lib/python3.12/site-packages/sitecustomize.py"
    )

    added = subprocess.run(
        [
            sys.executable,
            "-c",
            "import site, runpy, sys; site.addsitedir = lambda p, *a: print(p); "
            f"runpy.run_path({str(sitecustomize)!r})",
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    expected = moved.parent / "runtimes/cpython-3.12/lib/python3.12/site-packages"
    assert str(expected.resolve()) in added
    assert str(build_tree) not in added


def test_the_guard_catches_a_layer_that_was_missed(tmp_path: Path) -> None:
    """A silent failure needs a loud check, or the next change reintroduces it."""
    build = _build_module()
    bundle = tmp_path / "OpenKimo.app"
    layer = bundle / "Contents/Resources/runtimes/app-app-demo"
    layer.mkdir(parents=True)
    (layer / "pyvenv.cfg").write_text(
        f"home = {bundle.resolve()}/Contents/Resources/runtimes/x/bin\n"
    )

    with pytest.raises(RuntimeError, match="pin the bundle to the build tree"):
        build.verify_relocatable(bundle)


def test_the_guard_catches_a_path_back_into_the_repo(tmp_path: Path) -> None:
    """The shipped bug's exact shape: an installed bundle reaching into the repo."""
    build = _build_module()
    bundle = tmp_path / "OpenKimo.app"
    layer = bundle / "Contents/Resources/runtimes/app-app-demo"
    layer.mkdir(parents=True)
    (layer / "sitecustomize.py").write_text(
        f"addsitedir('{ROOT}/dist/OpenKimo.app/Contents/Resources/runtimes/x')\n"
    )

    with pytest.raises(RuntimeError, match="pin the bundle to the build tree"):
        build.verify_relocatable(bundle)


def test_wheel_provenance_metadata_is_not_treated_as_a_break(tmp_path: Path) -> None:
    """`direct_url.json` names the build machine by definition, and no import
    ever reads it. Failing the build over it would only teach people to skip
    the check."""
    build = _build_module()
    bundle = tmp_path / "OpenKimo.app"
    info = bundle / "Contents/Resources/runtimes/f/lib/python3.12/site-packages/x-1.0.dist-info"
    info.mkdir(parents=True)
    (info / "direct_url.json").write_text(
        json.dumps({"url": f"file://{ROOT}/packaging/wheels/x-1.0-py3-none-any.whl"})
    )

    build.verify_relocatable(bundle)  # does not raise


def test_a_clean_bundle_passes_the_guard(tmp_path: Path) -> None:
    build = _build_module()
    bundle = tmp_path / "OpenKimo.app"
    layer = bundle / "Contents/Resources/runtimes/app-app-demo"
    layer.mkdir(parents=True)
    (layer / "pyvenv.cfg").write_text("home = ../cpython-3.12/bin\n")

    build.verify_relocatable(bundle)  # does not raise


def test_the_build_checks_before_it_signs() -> None:
    """Signing a bundle that still points at the build tree just ships the bug.

    Anchored inside main(), and on calls that are actually there. The first
    version compared the position of `make_layers_relocatable(runtimes)` —
    which appears only inside copy_runtimes(), several hundred lines *above*
    `def main(` — against positions searched from main() onwards. `make_at <
    verify_at` was therefore true no matter how main() was ordered, so the
    assertion could not fail for the reason it names.
    """
    source = BUILD_PY.read_text(encoding="utf-8")
    main_at = source.index("def main(")
    main_body = source[main_at:]

    # Not create_dmg: main() has an early-return branch that builds a DMG from
    # an existing bundle and returns, so it appears twice and the first
    # occurrence is before the main build path.
    create_at = main_body.index("create_app_bundle(cfg, export_dir)")
    verify_at = main_body.index("verify_relocatable(bundle)")
    sign_at = main_body.index("sign_app(cfg, bundle)")

    assert create_at < verify_at < sign_at

    # And the relocation itself still happens on the way in, inside
    # copy_runtimes — the step create_app_bundle depends on. Checked with the
    # AST, not a substring: `pass  # make_layers_relocatable(runtimes)` still
    # contains the text, so a substring test cannot tell a call from a comment.
    import ast

    tree = ast.parse(source)
    copy_runtimes = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "copy_runtimes"
    )
    called = {
        node.func.id
        for node in ast.walk(copy_runtimes)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "make_layers_relocatable" in called
