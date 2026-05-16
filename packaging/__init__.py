"""OpenKimo packaging tools.

Marker so that ``packaging`` is importable as a regular package both in
dev (running ``python -m packaging.app_main_win`` from the repo root) and
in the bundled Windows runtime (``runtime/packaging/`` on PYTHONPATH).

Note: the PyPI package ``packaging`` (used by pip/setuptools for version
parsing) shadows this one if its directory comes first on sys.path. The
Windows launcher sets ``PYTHONPATH=runtime;runtime/site-packages`` so our
``runtime/packaging/`` wins. We never invoke pip from the bundled runtime,
so the shadowed PyPI ``packaging`` is not needed at run time.
"""
