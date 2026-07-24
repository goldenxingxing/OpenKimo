#!/usr/bin/env python3
"""Build OpenKimo (or a white-label rebrand) into a Windows installer (.exe).

Pipeline:
  1. Parse CLI flags (defaults from ``packaging/brand.toml``).
  2. ``npm install`` + ``npm run build`` under ``kimi-cli/web/`` so the
     bundled UI matches the current kimi-cli. Skip with ``--skip-frontend``.
  3. Build the kimi-cli wheel via ``uv build --wheel`` (cached under
     ``packaging/.cache/wheels/``).
  4. Stage the runtime layout under ``build/runtime-staging/``:
       runtime/
         python/                  — extracted python-build-standalone
         site-packages/           — pip-installed deps (kimi-cli wheel + win deps)
         kimi_cli/                — kimi_cli source (so ``import kimi_cli`` works)
         packaging/               — packaging/__init__.py + app_main + app_main_win
         playwright-browsers/     — chromium snapshot
         OpenKimo.ico             — generated from packaging/icon.png
       OpenKimo.exe               — PyInstaller-built thin launcher
       brand.json                 — brand metadata read by paths.py
       TrayIcon.ico               — same as runtime/OpenKimo.ico, surfaced
                                    at install_root for tray loading
  5. Build the launcher .exe (PyInstaller, Windows-only).
  6. Run Inno Setup to produce ``dist-win/<AppName>Setup-<version>.exe``
     (Windows-only).

Import-resolution approach (the ``packaging.*`` problem)
-------------------------------------------------------
The Windows entry package (``packaging/app_main_win/``) ships thin shims
that do ``from packaging.app_main.X import ...``. For this to work, the
``packaging`` directory itself has to be importable as a regular package.
We do the simplest thing:

  * Drop a one-line ``packaging/__init__.py`` in the repo (already
    created by this changeset).
  * Copy the **whole** ``packaging/`` directory (just ``__init__.py``,
    ``app_main/``, ``app_main_win/`` — no caches, no build scripts) into
    ``runtime/packaging/``.
  * The launcher .exe sets ``PYTHONPATH=runtime;runtime/site-packages``
    so our ``runtime/packaging/`` resolves before any other ``packaging``
    on the path. The PyPI distribution named ``packaging`` (pulled in
    transitively by pip itself, possibly by playwright) ends up under
    ``runtime/site-packages/packaging/`` and is *shadowed* by ours — that
    is fine because nothing in the runtime imports the PyPI ``packaging``;
    pip is only run at build time.

Alternative approaches we considered and rejected:
  * Renaming our package to avoid the collision: would mean rewriting
    every ``from packaging.app_main.X import ...`` shim, which the macOS
    build is supposed to be bit-identical to.
  * Editable / .pth file trickery: more brittle; harder to debug from
    a frozen .exe.

Usage examples are in ``packaging/README.md``.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import hashlib
import json
import os
import platform
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import tomllib
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
KIMI_CLI = REPO / "kimi-cli"

# Pinned python-build-standalone release. Bump these together when picking
# a newer CPython. Validate with:
#   curl -I https://github.com/astral-sh/python-build-standalone/releases/download/<TAG>/cpython-<VER>+<TAG>-x86_64-pc-windows-msvc-install_only.tar.gz
PY_STANDALONE_TAG = "20260510"
PY_STANDALONE_VERSION = "3.12.13"
PY_STANDALONE_TARGET = "x86_64-pc-windows-msvc-install_only"
PY_STANDALONE_URL = (
    f"https://github.com/astral-sh/python-build-standalone/releases/download/"
    f"{PY_STANDALONE_TAG}/cpython-{PY_STANDALONE_VERSION}+{PY_STANDALONE_TAG}"
    f"-{PY_STANDALONE_TARGET}.tar.gz"
)
LOGO_MAX_BYTES = 512 * 1024
FAVICON_MAX_BYTES = 256 * 1024
LEGACY_BUILTIN_ASSET_SHA256 = [
    "dbd00e2ad61ea8832ef0b024662a4a8a5d1b66f0599d5d42e1c9688b9d4cfdf6"
]


# ---- config ---------------------------------------------------------------

@dataclass
class BuildConfig:
    app_name: str
    slug: str
    version: str
    build_number: str
    copyright: str
    py_version: str
    icon: Path
    logo: Path
    favicon: Path
    brand_name: str
    page_title: str
    output_dir: Path
    skip_frontend: bool = False
    skip_python_runtime: bool = False
    skip_installer: bool = False
    extras: dict = field(default_factory=dict)


def _slugify(name: str) -> str:
    s = name.lower().replace(" ", "-")
    s = re.sub(r"[^a-z0-9-]", "", s)
    return s or "openkimo"


def _read_toml(p: Path) -> dict:
    if not p.exists():
        return {}
    return tomllib.loads(p.read_text())


def _kimi_version() -> str:
    pyproject = _read_toml(KIMI_CLI / "pyproject.toml")
    return pyproject.get("project", {}).get("version", "0.0.0")


def parse_args(argv: list[str]) -> BuildConfig:
    brand = _read_toml(ROOT / "brand.toml")
    bapp = brand.get("app", {})
    bbrand = brand.get("branding", {})
    bpy = brand.get("python", {})

    parser = argparse.ArgumentParser(
        description="Build OpenKimo Windows installer (.exe).",
    )
    parser.add_argument("--app-name", default=bapp.get("name", "OpenKimo"))
    parser.add_argument("--version", default=None,
                        help="Marketing version; defaults to kimi-cli pyproject.")
    parser.add_argument("--build-number", default=None,
                        help="Build number; defaults to today's date + serial.")
    parser.add_argument("--copyright",
                        default=bapp.get("copyright",
                                         "© 2026 OpenKimo Contributors. Apache License 2.0."))
    parser.add_argument("--icon", default=bapp.get("icon", "icon.png"))
    parser.add_argument("--logo", default=bapp.get("logo", bapp.get("icon", "icon.png")))
    parser.add_argument(
        "--favicon", default=bapp.get("favicon", bapp.get("icon", "icon.png"))
    )
    parser.add_argument("--brand-name", default=bbrand.get("brand_name"))
    parser.add_argument("--page-title", default=bbrand.get("page_title"))
    parser.add_argument("--py-version", default=bpy.get("version", "3.12"))
    parser.add_argument("--output-dir", default="../dist-win",
                        help="Where the .exe installer is written.")
    parser.add_argument("--skip-frontend", action="store_true",
                        help="Skip the npm install + npm run build step.")
    parser.add_argument("--skip-python-runtime", action="store_true",
                        help="Reuse the already-downloaded/extracted python "
                             "standalone runtime under runtime-staging/python/.")
    parser.add_argument("--skip-installer", action="store_true",
                        help="Skip the Inno Setup invocation; just stage the "
                             "runtime tree and (on Windows) build the launcher.")
    args = parser.parse_args(argv)

    app_name = args.app_name.strip()
    slug = _slugify(app_name)
    version = args.version or _kimi_version()
    if args.build_number:
        build_number = args.build_number
    else:
        today = dt.datetime.now().strftime("%Y.%m.%d")
        build_number = f"{today}.1"

    icon = (ROOT / args.icon).resolve()
    logo = (ROOT / args.logo).resolve()
    favicon = (ROOT / args.favicon).resolve()

    return BuildConfig(
        app_name=app_name,
        slug=slug,
        version=version,
        build_number=build_number,
        copyright=args.copyright,
        py_version=args.py_version,
        icon=icon,
        logo=logo,
        favicon=favicon,
        brand_name=args.brand_name or app_name,
        page_title=args.page_title or app_name,
        output_dir=(ROOT / args.output_dir).resolve(),
        skip_frontend=args.skip_frontend,
        skip_python_runtime=args.skip_python_runtime,
        skip_installer=args.skip_installer,
    )


# ---- shell helpers --------------------------------------------------------

def _resolve_cmd(cmd: list[str]) -> list[str]:
    # Windows CreateProcess does not consult PATHEXT, so bare names like
    # "npm" fail to launch the actual "npm.cmd" shim that setup-node installs.
    # shutil.which walks PATHEXT, so resolving cmd[0] up front lets us invoke
    # .cmd/.bat shims (npm, npx) and plain .exe binaries (uv) uniformly.
    if sys.platform != "win32" or not cmd:
        return cmd
    resolved = shutil.which(cmd[0])
    return [resolved, *cmd[1:]] if resolved else cmd


def run(cmd: list[str], **kwargs) -> None:
    print(f"$ {shlex.join(cmd)}", flush=True)
    subprocess.run(_resolve_cmd(cmd), check=True, **kwargs)


def run_ok(cmd: list[str], **kwargs) -> int:
    print(f"$ {shlex.join(cmd)}", flush=True)
    return subprocess.run(_resolve_cmd(cmd), check=False, **kwargs).returncode


def section(title: str) -> None:
    bar = "─" * (len(title) + 4)
    print(f"\n{bar}\n  {title}\n{bar}", flush=True)


def is_windows() -> bool:
    return sys.platform == "win32"


def warn_not_windows(what: str) -> None:
    print(
        f"  ! WARNING: {what} requires Windows; current platform is "
        f"{platform.system()} ({sys.platform}). Skipping; the produced "
        f"staging layout is still inspectable.",
        flush=True,
    )


# ---- 1. frontend (parity with build.py:build_frontend) -------------------

def build_frontend(cfg: BuildConfig) -> None:
    section("Building web frontend (npm run build)")
    web_dir = KIMI_CLI / "web"
    if not web_dir.exists():
        raise RuntimeError(
            f"frontend source directory {web_dir} does not exist; cannot "
            "build the web UI. Pass --skip-frontend to bypass."
        )
    if shutil.which("node") is None or shutil.which("npm") is None:
        raise RuntimeError(
            "node and/or npm not found on PATH. Install Node.js (LTS), or "
            "pass --skip-frontend to reuse an existing build."
        )

    node_modules = web_dir / "node_modules"
    package_json = web_dir / "package.json"
    install_marker = node_modules / ".package-lock.json"
    needs_install = (
        not node_modules.exists()
        or not install_marker.exists()
        or package_json.stat().st_mtime > install_marker.stat().st_mtime
    )
    if needs_install:
        run(["npm", "install"], cwd=str(web_dir))
    else:
        print(f"  node_modules in {web_dir} looks fresh; skipping npm install.")
    run(["npm", "run", "build"], cwd=str(web_dir))

    dist = web_dir / "dist"
    if not dist.exists() or not any(dist.iterdir()):
        raise RuntimeError(
            f"npm run build completed but {dist} is empty; check Vite output."
        )


# ---- 2. wheels ------------------------------------------------------------

def build_kimi_wheel(cache_dir: Path) -> Path:
    """Build the kimi-cli wheel locally via ``uv build``.

    Returns the path to the produced wheel. Cached: if a wheel matching
    the current kimi-cli version is already in ``cache_dir``, we reuse it.
    """
    section("Building kimi-cli wheel (uv build)")
    cache_dir.mkdir(parents=True, exist_ok=True)
    version = _kimi_version()
    # Wheel name normalises hyphen -> underscore.
    existing = sorted(cache_dir.glob(f"kimi_cli-{version}*.whl"))
    if existing:
        wheel = existing[-1]
        print(f"  reusing cached wheel: {wheel.name}")
        return wheel

    if shutil.which("uv") is None:
        raise RuntimeError(
            "uv not found on PATH. Install via https://github.com/astral-sh/uv "
            "or `brew install uv`."
        )
    run(["uv", "build", "--wheel",
         "--out-dir", str(cache_dir),
         str(KIMI_CLI)])
    produced = sorted(cache_dir.glob(f"kimi_cli-{version}*.whl"))
    if not produced:
        raise RuntimeError(
            f"uv build completed but no kimi_cli-{version} wheel landed in "
            f"{cache_dir}. Check the output above."
        )
    return produced[-1]


# ---- 3. python-build-standalone -----------------------------------------

def _download_with_progress(url: str, dest: Path) -> None:
    print(f"  downloading {url}")
    print(f"      -> {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    if tmp.exists():
        tmp.unlink()
    req = urllib.request.Request(url, headers={"User-Agent": "openkimo-build/1.0"})
    with urllib.request.urlopen(req) as resp, open(tmp, "wb") as out:
        total = int(resp.headers.get("Content-Length", 0))
        copied = 0
        last_pct = -1
        while True:
            chunk = resp.read(1 << 16)
            if not chunk:
                break
            out.write(chunk)
            copied += len(chunk)
            if total:
                pct = int(copied * 100 / total)
                if pct != last_pct and pct % 5 == 0:
                    print(f"    {pct}% ({copied // (1 << 20)} MiB)", flush=True)
                    last_pct = pct
    tmp.replace(dest)


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def fetch_python_runtime(cache_dir: Path, dest_root: Path) -> Path:
    """Download (with cache) and extract python-build-standalone into
    ``dest_root/python/``. Returns the path to the extracted ``python/`` dir.

    The archive root is already named ``python/``, so we extract straight
    into ``dest_root`` and the directory falls into place.
    """
    section(f"Staging python-build-standalone ({PY_STANDALONE_VERSION}+{PY_STANDALONE_TAG})")
    cache_dir.mkdir(parents=True, exist_ok=True)
    tar_name = (
        f"cpython-{PY_STANDALONE_VERSION}+{PY_STANDALONE_TAG}"
        f"-{PY_STANDALONE_TARGET}.tar.gz"
    )
    tar_path = cache_dir / tar_name
    if not tar_path.exists():
        _download_with_progress(PY_STANDALONE_URL, tar_path)
    else:
        print(f"  cached: {tar_path} ({tar_path.stat().st_size / (1 << 20):.1f} MiB)")

    print(f"  sha256({tar_name}) = {_sha256(tar_path)}")

    python_dir = dest_root / "python"
    if python_dir.exists():
        shutil.rmtree(python_dir)
    dest_root.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar_path, "r:gz") as tar:
        # python-build-standalone archives root at "python/"; extract straight.
        tar.extractall(dest_root)
    if not python_dir.exists():
        raise RuntimeError(
            f"expected {python_dir} after extraction; archive layout may have "
            f"changed. Inspect {tar_path}."
        )
    return python_dir


# ---- 4. site-packages ----------------------------------------------------

def install_site_packages(
    python_exe: Path,
    site_packages: Path,
    requirements_file: Path,
    kimi_wheel: Path,
    pip_log: Path,
) -> None:
    """``pip install --target=site_packages`` for the win deps + kimi wheel.

    On macOS we can't actually invoke the Windows ``python.exe``. To still
    produce a meaningful layout for inspection, we fall back to the host's
    python3 + ``--platform`` / ``--only-binary :all:`` filters when not on
    Windows. The resulting site-packages won't run on Windows verbatim
    (any compiled extension would be wrong-platform), but the layout is
    correct enough for diffing.
    """
    section("Installing runtime site-packages (pip --target)")
    site_packages.mkdir(parents=True, exist_ok=True)

    pip_cmd_base: list[str]
    if is_windows():
        pip_cmd_base = [str(python_exe), "-m", "pip"]
    else:
        # Dev fallback: use the host Python. We can't bootstrap pip inside the
        # Windows python.exe from macOS, but we can simulate the install layout
        # using the macOS python by passing --target.
        host_py = sys.executable
        print(
            f"  ! Not on Windows; using host {host_py} for layout-only install. "
            f"Compiled extensions in the produced site-packages will be "
            f"wrong-platform — Windows install must be performed on Windows."
        )
        pip_cmd_base = [host_py, "-m", "pip"]

    common_flags = [
        "install",
        "--target", str(site_packages),
        "--no-warn-script-location",
        "--upgrade",
        "--log", str(pip_log),
    ]
    # When on non-Windows, ask pip to fetch Windows wheels where possible.
    # This may still fail for packages that have no Windows wheels published
    # for the target Python version; in that case we tolerate the error and
    # print a warning so the rest of the staging layout still gets built.
    if not is_windows():
        common_flags += [
            "--only-binary", ":all:",
            "--platform", "win_amd64",
            "--python-version", PY_STANDALONE_VERSION.rsplit(".", 1)[0],
            "--implementation", "cp",
            "--abi", "cp" + PY_STANDALONE_VERSION.split(".", 2)[0]
                       + PY_STANDALONE_VERSION.split(".", 2)[1],
        ]

    # 1) Install the kimi-cli wheel (no-deps first; we'll resolve deps via
    #    the requirements file so version pins stay consistent).
    rc = run_ok(pip_cmd_base + common_flags + [str(kimi_wheel)])
    if rc != 0:
        print(
            f"  ! pip install of kimi-cli wheel returned {rc}; continuing "
            f"so the rest of the layout is inspectable. Inspect {pip_log}."
        )

    # 2) Install windows-specific deps from requirements.
    if requirements_file.exists():
        rc = run_ok(pip_cmd_base + common_flags + ["-r", str(requirements_file)])
        if rc != 0:
            print(
                f"  ! pip install of {requirements_file.name} returned {rc}; "
                f"continuing. Inspect {pip_log}."
            )
    else:
        print(f"  ! {requirements_file} missing; skipping deps install.")


# ---- 5. playwright browsers ---------------------------------------------

def install_playwright_browsers(
    python_exe: Path,
    site_packages: Path,
    browsers_dir: Path,
) -> None:
    """Run ``playwright install chromium`` with ``PLAYWRIGHT_BROWSERS_PATH``
    pointing straight into the staging dir, so chromium lands there directly.
    """
    section("Installing Playwright chromium browser")
    browsers_dir.mkdir(parents=True, exist_ok=True)

    if not is_windows():
        warn_not_windows("Playwright chromium install (Windows-only browser binary)")
        return

    env = os.environ.copy()
    # Prepend the just-installed site-packages so the Windows python.exe
    # can import playwright without a separate pip install in the runtime.
    env["PYTHONPATH"] = (
        f"{site_packages}{os.pathsep}{env.get('PYTHONPATH', '')}"
    )
    env["PLAYWRIGHT_BROWSERS_PATH"] = str(browsers_dir)
    rc = run_ok([str(python_exe), "-m", "playwright", "install", "chromium"], env=env)
    if rc != 0:
        print(
            f"  ! playwright install returned {rc}. The runtime will not have "
            f"a bundled chromium; the agent will need to download it on first run."
        )


# ---- 6. icon (PIL) -------------------------------------------------------

def generate_ico(src_png: Path, dest_ico: Path) -> None:
    section("Generating OpenKimo.ico (PIL)")
    try:
        from PIL import Image  # type: ignore
    except ImportError as e:
        raise RuntimeError(
            "Pillow is required to generate the .ico. Install with "
            "`pip install pillow`."
        ) from e
    if not src_png.exists():
        raise RuntimeError(f"icon source {src_png} does not exist.")
    dest_ico.parent.mkdir(parents=True, exist_ok=True)
    img = Image.open(src_png)
    if img.mode != "RGBA":
        img = img.convert("RGBA")
    sizes = [(16, 16), (32, 32), (48, 48), (256, 256)]
    img.save(dest_ico, format="ICO", sizes=sizes)
    print(f"  wrote {dest_ico}")


# ---- 7. packaging/ + kimi_cli/ copy --------------------------------------

_IGNORE_PATTERNS = shutil.ignore_patterns(
    "__pycache__", "*.pyc", ".DS_Store",
)


def copy_packaging_into_runtime(runtime: Path) -> None:
    """Copy ``packaging/__init__.py``, ``app_main/``, ``app_main_win/`` into
    ``runtime/packaging/`` so the shims resolve at runtime.
    """
    section("Copying packaging/ tree into runtime/packaging/")
    dest = runtime / "packaging"
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)

    init_src = ROOT / "__init__.py"
    if not init_src.exists():
        raise RuntimeError(
            f"{init_src} does not exist. It must be created so `packaging` is "
            f"importable. (This script's caller should have created it.)"
        )
    shutil.copy2(init_src, dest / "__init__.py")
    shutil.copytree(ROOT / "app_main", dest / "app_main", ignore=_IGNORE_PATTERNS)
    shutil.copytree(ROOT / "app_main_win", dest / "app_main_win", ignore=_IGNORE_PATTERNS)


def copy_kimi_cli_source(runtime: Path) -> None:
    """Mirror macOS build.py:copy_kimi_cli_source.

    Lays out ``runtime/kimi_cli/kimi_cli/...`` so paths.py's
    ``kimi_cli = runtime / "kimi_cli" / "kimi_cli"`` resolves.
    """
    section("Copying kimi_cli source into runtime/kimi_cli/")
    src = KIMI_CLI / "src" / "kimi_cli"
    if not src.exists():
        raise RuntimeError(f"kimi-cli source not found at {src}")
    outer = runtime / "kimi_cli"
    if outer.exists():
        shutil.rmtree(outer)
    outer.mkdir(parents=True)
    shutil.copytree(src, outer / "kimi_cli", ignore=_IGNORE_PATTERNS)


def overlay_fresh_frontend(runtime: Path) -> None:
    """A freshly built ``kimi-cli/web/dist/`` overrides the committed
    pre-built static artefact (same rule as macOS build.py)."""
    static_src = KIMI_CLI / "web" / "dist"
    static_alt = KIMI_CLI / "src" / "kimi_cli" / "web" / "static"
    static_dest = runtime / "kimi_cli" / "kimi_cli" / "web" / "static"
    if static_src.exists() and any(static_src.iterdir()):
        if static_dest.exists():
            shutil.rmtree(static_dest)
        static_dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copytree(static_src, static_dest)
        print(f"  overlayed fresh Vite dist -> {static_dest}")
    elif static_alt.exists() and any(static_alt.iterdir()):
        print("  no fresh Vite dist; using committed kimi_cli/web/static.")
    else:
        print(
            "  ! frontend not built; KIMI_WEB_STATIC_DIR will be empty.\n"
            "    Run `cd kimi-cli/web && npm install && npm run build` "
            "before packaging, or omit --skip-frontend."
        )


# ---- 8. brand.json -------------------------------------------------------

def _data_url(path: Path, max_bytes: int) -> str:
    raw = path.read_bytes()
    if len(raw) > max_bytes:
        raise ValueError(f"{path} exceeds {max_bytes} bytes")
    return "data:image/png;base64," + base64.b64encode(raw).decode("ascii")


def write_brand_json(cfg: BuildConfig, dest: Path) -> None:
    """Drop the brand.json at install_root (read by paths.py at runtime)."""
    data = {
        "app_name": cfg.app_name,
        "slug": cfg.slug,
        "display_name": cfg.app_name,
        "copyright": cfg.copyright,
        "version": cfg.version,
        "build_number": cfg.build_number,
        "branding_seed": {
            "brand_name": cfg.brand_name,
            "page_title": cfg.page_title,
            "version": cfg.version,
            "logo": _data_url(cfg.logo, LOGO_MAX_BYTES),
            "favicon": _data_url(cfg.favicon, FAVICON_MAX_BYTES),
        },
        "branding_legacy_asset_sha256": LEGACY_BUILTIN_ASSET_SHA256,
    }
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_text(json.dumps(data, indent=2), encoding="utf-8")
    print(f"  wrote {dest}")


# ---- 9. launcher .exe (PyInstaller) --------------------------------------

_LAUNCHER_PY = '''\
"""OpenKimo Windows launcher (frozen via PyInstaller).

Resolves the install root from sys.executable, then spawns
``runtime\\python\\pythonw.exe -m packaging.app_main_win`` with the right
PYTHONPATH so our packaging shims resolve. Runs detached so the .exe
returns immediately; the tray supervisor lives in pythonw.exe.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path


def main() -> int:
    here = Path(sys.executable).resolve().parent
    runtime = here / "runtime"
    python = runtime / "python" / "pythonw.exe"
    if not python.exists():
        # Fallback to python.exe if pythonw.exe is missing for any reason.
        python = runtime / "python" / "python.exe"
    if not python.exists():
        # Print to a log file because there's no console.
        log = here / "launcher-error.log"
        log.write_text(f"runtime python not found under {runtime}\\n", encoding="utf-8")
        return 1

    env = os.environ.copy()
    # runtime/ first so our packaging shim (runtime/packaging/, providing
    # packaging.app_main_win) wins the top-level `packaging` name. If any
    # future dep ever lands a PyPI `packaging/` in site-packages, this
    # ordering keeps the launcher importable. The shim has no other
    # top-level collisions with site-packages today, so site-packages
    # comes second.
    # pywin32 (needed by mcp on Windows) normally relies on pywin32.pth to
    # expose win32/, win32/lib/ and Pythonwin/; .pth files are processed
    # only for real site dirs, never for PYTHONPATH entries, so list those
    # directories explicitly. pywintypes locates its DLLs by scanning
    # sys.path for pywin32_system32/, which resolves via site-packages.
    sp = runtime / "site-packages"
    pythonpath_parts = [
        str(runtime),
        str(sp),
        str(sp / "win32"),
        str(sp / "win32" / "lib"),
        str(sp / "Pythonwin"),
    ]
    if env.get("PYTHONPATH"):
        pythonpath_parts.append(env["PYTHONPATH"])
    env["PYTHONPATH"] = os.pathsep.join(pythonpath_parts)
    env["KIMI_OPENKIMO_WIN"] = "1"

    # Detach: the .exe should return immediately and the tray runs in pythonw.
    DETACHED_PROCESS = 0x00000008
    CREATE_NEW_PROCESS_GROUP = 0x00000200
    flags = DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP

    subprocess.Popen(
        [str(python), "-m", "packaging.app_main_win"],
        env=env,
        cwd=str(here),
        close_fds=True,
        creationflags=flags,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
'''


def build_launcher_exe(
    cfg: BuildConfig,
    staging: Path,
    work_dir: Path,
    icon_ico: Path,
) -> Path | None:
    """PyInstaller-build OpenKimo.exe. Windows-only.

    Returns the path to the produced .exe (placed into ``staging/``).
    On non-Windows hosts, writes a placeholder file so the staging layout
    is still complete-looking, and returns that placeholder path.
    """
    section("Building launcher .exe (PyInstaller)")
    launcher_py = work_dir / "launcher.py"
    work_dir.mkdir(parents=True, exist_ok=True)
    launcher_py.write_text(_LAUNCHER_PY, encoding="utf-8")

    final = staging / "OpenKimo.exe"

    if not is_windows():
        warn_not_windows("PyInstaller (produces a Windows .exe)")
        # Drop a readable placeholder so reviewers know what's missing.
        final.write_text(
            "OpenKimo.exe placeholder — built on Windows from "
            "build_windows.py via PyInstaller.\n",
            encoding="utf-8",
        )
        return final

    if shutil.which("pyinstaller") is None:
        print("  ! pyinstaller not on PATH. `pip install pyinstaller` first.")
        return None

    pyinst_dist = work_dir / "launcher-dist"
    pyinst_work = work_dir / "launcher-work"
    pyinst_spec = work_dir
    for p in (pyinst_dist, pyinst_work):
        if p.exists():
            shutil.rmtree(p)

    cmd = [
        "pyinstaller",
        "--onefile",
        "--noconsole",
        "--name", "OpenKimo",
        "--distpath", str(pyinst_dist),
        "--workpath", str(pyinst_work),
        "--specpath", str(pyinst_spec),
        # The launcher is a thin script; PyInstaller's static analysis won't
        # follow transitive imports done by the spawned pythonw runtime
        # (pystray, PIL, packaging shim). Pin them explicitly so the frozen
        # .exe doesn't ModuleNotFoundError on first run.
        "--hidden-import", "pystray",
        "--hidden-import", "pystray._win32",
        "--hidden-import", "PIL.Image",
        "--hidden-import", "PIL.ImageDraw",
        "--collect-submodules", "pystray",
    ]
    if icon_ico.exists():
        cmd += ["--icon", str(icon_ico)]
    cmd.append(str(launcher_py))
    run(cmd)

    produced = pyinst_dist / "OpenKimo.exe"
    if not produced.exists():
        raise RuntimeError(f"PyInstaller did not produce {produced}.")
    shutil.copy2(produced, final)
    print(f"  staged {final}")
    return final


# ---- 10. Inno Setup ------------------------------------------------------

def find_iscc() -> Path | None:
    candidate = shutil.which("iscc")
    if candidate:
        return Path(candidate)
    candidate = shutil.which("ISCC")
    if candidate:
        return Path(candidate)
    # Common install location.
    for p in (
        Path(r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe"),
        Path(r"C:\Program Files\Inno Setup 6\ISCC.exe"),
    ):
        if p.exists():
            return p
    return None


def run_inno_setup(
    cfg: BuildConfig,
    staging: Path,
    icon_ico: Path,
    iss_path: Path,
    extra_defines: dict[str, str] | None = None,
) -> Path | None:
    """Compile installer.iss into the final .exe installer.

    ``extra_defines`` lets callers pass additional ``/D<name>=<value>``
    preprocessor defines to iscc — e.g. ``{"AppId": "{{...}}"}`` for
    white-label rebuilds. When omitted, the script's ``#ifndef`` defaults
    apply (including the hardcoded OpenKimo AppId GUID).

    Returns the produced installer path, or None on skip/failure.
    """
    section("Compiling installer (Inno Setup)")
    cfg.output_dir.mkdir(parents=True, exist_ok=True)

    if not is_windows():
        warn_not_windows("Inno Setup compiler (iscc.exe is Windows-only)")
        return None

    iscc = find_iscc()
    if iscc is None:
        print(
            "  ! Inno Setup not found. Install it from "
            "https://jrsoftware.org/isdl.php (version 6) and re-run."
        )
        return None

    cmd = [
        str(iscc),
        f"/DAppName={cfg.app_name}",
        f"/DAppVersion={cfg.version}",
        f"/DStagingDir={staging}",
        f"/DIconFile={icon_ico}",
        f"/DOutputDir={cfg.output_dir}",
    ]
    if extra_defines:
        for name, value in extra_defines.items():
            cmd.append(f"/D{name}={value}")
    cmd.append(str(iss_path))
    rc = run_ok(cmd)
    if rc != 0:
        print(f"  ! iscc returned {rc}.")
        return None

    expected = cfg.output_dir / f"{cfg.app_name}Setup-{cfg.version}.exe"
    if expected.exists():
        return expected
    # iscc may have produced a different name; list the dir.
    print(f"  installer expected at {expected}; check {cfg.output_dir} contents.")
    return None


# ---- summary -------------------------------------------------------------

def _du(path: Path) -> str:
    """Human-readable size of a file or directory tree."""
    if path.is_file():
        n = path.stat().st_size
    else:
        n = 0
        for p in path.rglob("*"):
            if p.is_file():
                try:
                    n += p.stat().st_size
                except OSError:
                    pass
    units = ("B", "KiB", "MiB", "GiB")
    i = 0
    f = float(n)
    while f >= 1024 and i < len(units) - 1:
        f /= 1024
        i += 1
    return f"{f:.1f} {units[i]}"


def print_summary(
    cfg: BuildConfig,
    staging: Path,
    launcher_exe: Path | None,
    installer_exe: Path | None,
    started_at: float,
) -> None:
    section("Build summary")
    print(f"  app:            {cfg.app_name} ({cfg.slug})")
    print(f"  version:        {cfg.version} ({cfg.build_number})")
    print(f"  staging:        {staging}  [{_du(staging)}]")
    runtime = staging / "runtime"
    if runtime.exists():
        for sub in ("python", "site-packages", "kimi_cli", "packaging",
                    "playwright-browsers"):
            p = runtime / sub
            if p.exists():
                print(f"    runtime/{sub:<22} {_du(p)}")
    if launcher_exe and launcher_exe.exists():
        print(f"  launcher .exe:  {launcher_exe}  [{_du(launcher_exe)}]")
    if installer_exe and installer_exe.exists():
        print(f"  installer .exe: {installer_exe}  [{_du(installer_exe)}]")
    print(f"  elapsed:        {time.time() - started_at:.1f} s")


# ---- main ----------------------------------------------------------------

def main(argv: list[str]) -> int:
    started_at = time.time()
    cfg = parse_args(argv)
    cfg.output_dir.mkdir(parents=True, exist_ok=True)

    print(f"App:        {cfg.app_name} ({cfg.slug})")
    print(f"Version:    {cfg.version} ({cfg.build_number})")
    print(f"Python:     {PY_STANDALONE_VERSION} (tag {PY_STANDALONE_TAG})")
    print(f"Output:     {cfg.output_dir}")
    print(f"Host:       {platform.system()} {platform.release()} ({sys.platform})")
    if not is_windows():
        print(
            "  ! Running on non-Windows; PyInstaller and Inno Setup will be "
            "skipped. The staging tree is still produced for inspection."
        )

    # -- paths -----
    build_dir = ROOT.parent / "build"  # repo-level scratch
    staging = build_dir / "runtime-staging"
    runtime = staging / "runtime"
    launcher_work = build_dir / "launcher"
    cache_dir = ROOT / ".cache"
    cache_wheels = cache_dir / "wheels"
    cache_python = cache_dir / "python-standalone"
    pip_log = build_dir / "pip.log"

    # Clean staging from a previous run, but keep the cache.
    if staging.exists():
        shutil.rmtree(staging)
    staging.mkdir(parents=True)
    runtime.mkdir(parents=True)

    # 1. Frontend
    if cfg.skip_frontend:
        print("\n(skip-frontend) reusing kimi-cli/web/dist if present.")
    else:
        build_frontend(cfg)

    # 2. kimi-cli wheel
    kimi_wheel = build_kimi_wheel(cache_wheels)

    # 3. Python runtime
    if cfg.skip_python_runtime and (runtime / "python").exists():
        # Can't trigger here because we just wiped staging — leave as a doc.
        print("\n(skip-python-runtime) flag set but staging was wiped; downloading anyway.")
    fetch_python_runtime(cache_python, runtime)
    # Path to python.exe inside the staged runtime.
    python_exe = runtime / "python" / "python.exe"

    # 4. site-packages
    site_packages = runtime / "site-packages"
    install_site_packages(
        python_exe=python_exe,
        site_packages=site_packages,
        requirements_file=ROOT / "requirements-windows.txt",
        kimi_wheel=kimi_wheel,
        pip_log=pip_log,
    )

    # 5. Playwright chromium
    install_playwright_browsers(
        python_exe=python_exe,
        site_packages=site_packages,
        browsers_dir=runtime / "playwright-browsers",
    )

    # 6. Icon
    icon_ico = runtime / "OpenKimo.ico"
    generate_ico(cfg.icon, icon_ico)
    # Also stage at install_root so paths.py's TrayIcon.ico resolves.
    tray_icon = staging / "TrayIcon.ico"
    shutil.copy2(icon_ico, tray_icon)

    # 7. packaging/ tree + kimi_cli source
    copy_packaging_into_runtime(runtime)
    copy_kimi_cli_source(runtime)
    overlay_fresh_frontend(runtime)

    # 8. brand.json (at install_root for paths.py)
    write_brand_json(cfg, staging / "brand.json")

    # 9. Launcher .exe
    launcher_exe = build_launcher_exe(
        cfg=cfg,
        staging=staging,
        work_dir=launcher_work,
        icon_ico=icon_ico,
    )

    # 10. Inno Setup
    installer_exe = None
    if cfg.skip_installer:
        print("\n(skip-installer) skipping Inno Setup compile.")
    else:
        installer_exe = run_inno_setup(
            cfg=cfg,
            staging=staging,
            icon_ico=icon_ico,
            iss_path=ROOT / "installer.iss",
        )

    print_summary(cfg, staging, launcher_exe, installer_exe, started_at)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
