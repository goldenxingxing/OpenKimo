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
    """Only the four attributes env_overlay reads."""
    return SimpleNamespace(
        userbase=userbase,
        userbase_bin=userbase / "bin",
        pip_conf=userbase.parent / "pip.conf",
        bundled_python_bin=userbase.parent / "runtimes" / "cpython-3.12" / "bin",
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
