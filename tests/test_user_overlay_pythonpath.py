"""A package installed into the overlay has to be importable by the server.

PYTHONUSERBASE is only half of it. The CLI wrapper and the Install Package…
flow install with the *runtime* interpreter, which honours user-site; the
server runs the *app-layer* interpreter, whose venv is built with
``ENABLE_USER_SITE = False``. So `openkimo pip install requests` reported
success, `openkimo python -c "import requests"` worked, and the agent inside
the server still answered ModuleNotFoundError.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "packaging"))

from app_main import userenv  # noqa: E402


def _paths(userbase: Path) -> SimpleNamespace:
    """Only the attributes env_overlay reads."""
    runtime = userbase.parent / "runtimes" / "cpython-3.12"
    return SimpleNamespace(
        userbase=userbase,
        userbase_bin=userbase / "bin",
        pip_conf=userbase.parent / "pip.conf",
        bundled_python_root=runtime,
        bundled_python_bin=runtime / "bin",
    )


def test_the_overlay_site_packages_are_named_on_pythonpath(tmp_path) -> None:
    site_packages = tmp_path / "userbase" / "lib" / "python3.12" / "site-packages"
    site_packages.mkdir(parents=True)

    env = userenv.env_overlay(_paths(tmp_path / "userbase"))

    assert str(site_packages) in env["PYTHONPATH"].split(os.pathsep)


def test_an_empty_overlay_adds_no_pythonpath(tmp_path) -> None:
    """Nothing installed yet is not a reason to put an empty entry on the path."""
    (tmp_path / "userbase").mkdir()

    env = userenv.env_overlay(_paths(tmp_path / "userbase"))

    assert "PYTHONPATH" not in env


def test_the_callers_own_pythonpath_is_kept(tmp_path, monkeypatch) -> None:
    site_packages = tmp_path / "userbase" / "lib" / "python3.12" / "site-packages"
    site_packages.mkdir(parents=True)
    monkeypatch.setenv("PYTHONPATH", "/somewhere/else")

    env = userenv.env_overlay(_paths(tmp_path / "userbase"))

    assert env["PYTHONPATH"].split(os.pathsep) == [
        str(site_packages),
        "/somewhere/else",
    ]


def test_another_python_version_in_the_userbase_is_left_out(tmp_path) -> None:
    """A userbase can hold more than one; only the bundled one may be used.

    A dev run with a different interpreter leaves its own directory there, and
    putting 3.14's site-packages on the 3.12 interpreter's path is worse than
    leaving the overlay off — a C extension built for the other version raises
    on import rather than simply being absent.
    """
    userbase = tmp_path / "userbase"
    (userbase / "lib" / "python3.12" / "site-packages").mkdir(parents=True)
    (userbase / "lib" / "python3.14" / "site-packages").mkdir(parents=True)

    env = userenv.env_overlay(_paths(userbase))

    assert env["PYTHONPATH"].split(os.pathsep) == [
        str(userbase / "lib" / "python3.12" / "site-packages")
    ]


def test_the_windows_layout_is_found_when_it_is_unambiguous(tmp_path) -> None:
    """The Windows runtime directory is just `python`, with no version in it."""
    userbase = tmp_path / "userbase"
    site_packages = userbase / "Python312" / "site-packages"
    site_packages.mkdir(parents=True)
    paths = _paths(userbase)
    paths.bundled_python_root = tmp_path / "runtime" / "python"

    env = userenv.env_overlay(paths)

    assert env["PYTHONPATH"].split(os.pathsep) == [str(site_packages)]


def test_an_ambiguous_windows_userbase_is_refused(tmp_path) -> None:
    """Two of them and no version to choose by: guessing is what breaks it."""
    userbase = tmp_path / "userbase"
    (userbase / "Python312" / "site-packages").mkdir(parents=True)
    (userbase / "Python314" / "site-packages").mkdir(parents=True)
    paths = _paths(userbase)
    paths.bundled_python_root = tmp_path / "runtime" / "python"

    env = userenv.env_overlay(paths)

    assert "PYTHONPATH" not in env
