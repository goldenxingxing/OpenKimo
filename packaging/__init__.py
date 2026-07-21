"""OpenKimo packaging tools.

Marker so that ``packaging`` is importable as a regular package both in
dev (running ``python -m packaging.app_main_win`` from the repo root) and
in the bundled Windows runtime (``runtime/packaging/`` on PYTHONPATH).

Note: the PyPI package ``packaging`` (a runtime dependency of fastmcp,
among others) is shadowed by this one because the Windows launcher sets
``PYTHONPATH=runtime;runtime/site-packages`` and our ``runtime/packaging/``
wins. To keep ``packaging.version`` / ``packaging.specifiers`` importable,
we graft the PyPI distribution's directory onto this package's ``__path__``:
submodule lookups fall through to ``site-packages/packaging/`` while
``packaging.app_main`` / ``packaging.app_main_win`` still resolve here.
In dev there is no sibling ``site-packages/`` so this is a no-op.
"""

import os as _os

_pypi_packaging = _os.path.join(
    _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))),
    "site-packages",
    "packaging",
)
if _os.path.isdir(_pypi_packaging):
    __path__.append(_pypi_packaging)
del _os, _pypi_packaging
