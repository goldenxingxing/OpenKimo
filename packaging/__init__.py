"""OpenKimo packaging tools.

Marker so that ``packaging`` is importable as a regular package both in
dev (running ``python -m packaging.app_main_win`` from the repo root) and
in the bundled Windows runtime (``runtime/packaging/`` on PYTHONPATH).

Note: the PyPI package ``packaging`` (a runtime dependency of fastmcp,
among others) is shadowed by this one whenever this directory comes first
on the path — the Windows launcher sets ``PYTHONPATH=runtime;runtime/site-packages``
so ``runtime/packaging/`` wins, and in dev the repo root is the working
directory so this one wins there. To keep ``packaging.version`` /
``packaging.specifiers`` importable we graft the PyPI distribution's
directory onto this package's ``__path__``: submodule lookups fall through
to it while ``packaging.app_main`` / ``packaging.app_main_win`` still
resolve here.

The bundled runtime keeps its own sibling ``site-packages/`` layout; dev has
no such sibling, so the real distribution is located by scanning the path the
interpreter was started with. Without that, anything importing
``packaging.version`` fails outright when run from the repo root — which is
where a developer naturally runs things.
"""

import os as _os
import sys as _sys


def _pypi_packaging_dirs():
    """Directories holding the real PyPI ``packaging``, most specific first."""
    here = _os.path.dirname(_os.path.abspath(__file__))

    # The bundled runtime layout: runtime/packaging next to runtime/site-packages.
    sibling = _os.path.join(_os.path.dirname(here), "site-packages", "packaging")
    if _os.path.isfile(_os.path.join(sibling, "version.py")):
        yield sibling

    # Dev: wherever this interpreter would have found it had we not shadowed it.
    for entry in _sys.path:
        if not entry:
            continue
        candidate = _os.path.join(entry, "packaging")
        if _os.path.abspath(candidate) == here:
            continue
        if _os.path.isfile(_os.path.join(candidate, "version.py")):
            yield candidate


for _found in _pypi_packaging_dirs():
    if _found not in __path__:
        __path__.append(_found)

del _os, _sys, _found, _pypi_packaging_dirs
