#!/usr/bin/env python3
"""Build OpenKimo (or a white-label rebrand) into a macOS .app + .dmg.

Pipeline:
  1. Parse CLI flags (defaults from ``packaging/brand.toml``).
  2. ``npm install`` (if needed) + ``npm run build`` in ``kimi-cli/web/`` so
     the bundled UI matches the current kimi-cli version. Skip with
     ``--skip-frontend``.
  3. ``uv build --wheel`` for kimi-cli + workspace members.
  4. Resolve ``venvstacks.toml`` (substitute local wheel paths).
  5. ``venvstacks lock / build / local-export``.
  6. Compile the C launcher to ``Contents/MacOS/<AppName>``.
  7. Convert ``icon.png`` → ``AppIcon.icns`` (sips + iconutil).
  8. Render Info.plist, brand.json, Credits.rtf, CLI wrapper.
  9. Copy ``Resources/kimi_cli`` from the submodule source; the freshly
     built ``kimi-cli/web/dist/`` overrides the committed pre-built static
     artefact under ``kimi-cli/src/kimi_cli/web/static/``.
 10. Ad-hoc codesign the bundle.
 11. Build a UDZO DMG with an Applications symlink.

Usage examples are in ``packaging/README.md``.
"""

from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import os
import platform
import plistlib
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
import tomllib
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent
REPO = ROOT.parent
KIMI_CLI = REPO / "kimi-cli"

LOGO_MAX_BYTES = 512 * 1024
FAVICON_MAX_BYTES = 256 * 1024


# ---- config ---------------------------------------------------------------

@dataclass
class BuildConfig:
    app_name: str
    slug: str
    bundle_id: str
    version: str
    build_number: str
    copyright: str
    min_macos: str
    py_version: str
    icon: Path
    logo: Path | None
    favicon: Path | None
    brand_name: str
    page_title: str
    output_dir: Path
    arch: str
    sign_identity: str = "-"  # "-" = ad-hoc
    skip_venv: bool = False
    skip_frontend: bool = False
    dmg_only: bool = False
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
    bbuild = brand.get("build", {})

    parser = argparse.ArgumentParser(description="Build OpenKimo macOS app/.dmg")
    parser.add_argument("--app-name", default=bapp.get("name", "OpenKimo"))
    parser.add_argument("--bundle-id", default=bapp.get("bundle_id"))
    parser.add_argument("--version", default=None,
                        help="Marketing version; defaults to kimi-cli pyproject.")
    parser.add_argument("--build-number", default=None,
                        help="Build number; defaults to today's date + serial.")
    parser.add_argument("--copyright",
                        default=bapp.get("copyright",
                                         "© 2026 OpenKimo Contributors. Apache License 2.0."))
    parser.add_argument("--min-macos", default=bapp.get("min_macos_version", "14.0"))
    parser.add_argument("--icon", default=bapp.get("icon", "icon.png"))
    parser.add_argument("--logo", default=bapp.get("logo"))
    parser.add_argument("--favicon", default=bapp.get("favicon"))
    parser.add_argument("--brand-name", default=bbrand.get("brand_name"))
    parser.add_argument("--page-title", default=bbrand.get("page_title"))
    parser.add_argument("--py-version", default=bpy.get("version", "3.12"))
    parser.add_argument("--output-dir", default=bbuild.get("output_dir", "../dist"))
    parser.add_argument("--sign-identity", default="-",
                        help='codesign identity; "-" for ad-hoc.')
    parser.add_argument("--skip-venv", action="store_true",
                        help="Reuse the existing venvstacks export.")
    parser.add_argument("--skip-frontend", action="store_true",
                        help="Skip the npm install + npm run build step "
                             "(assumes a fresh frontend dist or you've "
                             "already built it manually).")
    parser.add_argument("--dmg-only", action="store_true",
                        help="Re-pack only the DMG from a built .app.")
    parser.add_argument("--arch", choices=["arm64", "x86_64"], default=platform.machine(),
                        help="Target CPU architecture (default: host).")
    args = parser.parse_args(argv)

    app_name = args.app_name.strip()
    slug = _slugify(app_name)
    bundle_id = args.bundle_id or f"local.{slug}.app"
    version = args.version or _kimi_version()
    if args.build_number:
        build_number = args.build_number
    else:
        today = dt.datetime.now().strftime("%Y.%m.%d")
        build_number = f"{today}.1"

    icon = (ROOT / args.icon).resolve()
    logo = (ROOT / args.logo).resolve() if args.logo else None
    favicon = (ROOT / args.favicon).resolve() if args.favicon else None

    return BuildConfig(
        app_name=app_name,
        slug=slug,
        bundle_id=bundle_id,
        version=version,
        build_number=build_number,
        copyright=args.copyright,
        min_macos=args.min_macos,
        py_version=args.py_version,
        icon=icon,
        logo=logo,
        favicon=favicon,
        brand_name=args.brand_name or app_name,
        page_title=args.page_title or app_name,
        output_dir=(ROOT / args.output_dir).resolve(),
        arch=args.arch,
        sign_identity=args.sign_identity,
        skip_venv=args.skip_venv,
        skip_frontend=args.skip_frontend,
        dmg_only=args.dmg_only,
    )


# ---- shell helpers --------------------------------------------------------

def run(cmd: list[str], **kwargs) -> None:
    print(f"$ {shlex.join(cmd)}", flush=True)
    subprocess.run(cmd, check=True, **kwargs)


def section(title: str) -> None:
    bar = "─" * (len(title) + 4)
    print(f"\n{bar}\n  {title}\n{bar}", flush=True)


# ---- 0. frontend ----------------------------------------------------------

def build_frontend(cfg: BuildConfig) -> None:
    """Build the React/Vite frontend so the bundled web UI matches the
    current ``kimi-cli`` version. The Vite config bakes
    ``__KIMI_CLI_VERSION__`` at build time, so a stale committed
    ``src/kimi_cli/web/static/`` would otherwise show the wrong version
    in the top-left after a kimi-cli version bump.
    """
    section("Building web frontend (npm run build)")
    web_dir = KIMI_CLI / "web"
    if not web_dir.exists():
        raise RuntimeError(
            f"frontend source directory {web_dir} does not exist; cannot "
            "build the web UI. Pass --skip-frontend to bypass."
        )

    if shutil.which("node") is None or shutil.which("npm") is None:
        raise RuntimeError(
            "node and/or npm not found on PATH. Install Node.js from "
            "https://nodejs.org/ (LTS is fine), or pass --skip-frontend to "
            "reuse the existing frontend build."
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
            f"npm run build completed but {dist} is empty or missing. "
            "Check the Vite output above for errors."
        )

    # Vite writes to web/dist; the wheel packages src/kimi_cli/web/static. This
    # step used to stop at the check above, so the freshly built UI was thrown
    # away and whatever happened to be sitting in static/ shipped instead —
    # five days stale when it was noticed, and static/ is not even tracked, so
    # there was nothing to notice it against. The docstring above describes
    # exactly the failure this omission caused.
    static = KIMI_CLI / "src" / "kimi_cli" / "web" / "static"
    if static.exists():
        shutil.rmtree(static)
    shutil.copytree(dist, static)
    print(f"  copied {sum(1 for _ in static.rglob('*') if _.is_file())} files into {static}")


# ---- 1. wheels ------------------------------------------------------------

def _workspace_dirs() -> tuple[Path, ...]:
    """Every workspace member, read from the file that declares them.

    This used to be a second list here, which meant adding a workspace member
    was two edits in two repositories and the build only complained about the
    missing one at resolution time — as a package that could not be found on
    PyPI, which reads as a network problem rather than a forgotten line.

    kimi-cli comes last: it depends on the others, and the resolved venvstacks
    requirements keep that order.
    """
    with (KIMI_CLI / "pyproject.toml").open("rb") as f:
        members = tomllib.load(f)["tool"]["uv"]["workspace"]["members"]
    return tuple(KIMI_CLI / m for m in members) + (KIMI_CLI,)


WORKSPACE_DIRS = _workspace_dirs()

# Pure-Python deps that PyPI only ships as sdists. venvstacks locks with
# `--only-binary :all:`, so we build wheels for them locally and drop them
# alongside the workspace wheels.
THIRD_PARTY_SDIST_SPECS = (
    "ripgrepy==2.2.0",
    "rumps==0.4.0",
)


def _build_sdist_wheel(spec: str, wheels_dir: Path) -> None:
    with tempfile.TemporaryDirectory(prefix="sdist-") as tmp:
        run(["pip", "download", "--no-deps", "--no-binary", ":all:",
             "-d", tmp, spec])
        sdist = next(Path(tmp).glob("*.tar.gz"), None) \
            or next(Path(tmp).glob("*.zip"), None)
        if sdist is None:
            raise RuntimeError(f"failed to download sdist for {spec}")
        run(["uv", "build", "--wheel", "--out-dir", str(wheels_dir), str(sdist)])


def build_local_wheels(cfg: BuildConfig) -> Path:
    section("Building local wheels (uv build)")
    wheels_dir = ROOT / "wheels"
    if wheels_dir.exists():
        shutil.rmtree(wheels_dir)
    wheels_dir.mkdir()

    for src in WORKSPACE_DIRS:
        if not src.exists():
            print(f"  ! skip missing workspace member: {src}")
            continue
        # uv build --wheel writes into <pkg>/dist/
        run(["uv", "build", "--wheel", "--out-dir", str(wheels_dir), str(src)])

    for spec in THIRD_PARTY_SDIST_SPECS:
        _build_sdist_wheel(spec, wheels_dir)

    return wheels_dir


# ---- 2. venvstacks --------------------------------------------------------

def _unpin_local_wheels(build_dir: Path, wheels_dir: Path) -> None:
    """Make venvstacks reinstall the locally built wheels.

    pip skips a package whose installed version matches the wheel's, and the
    workspace members keep their version across every local build — so a source
    change ships stale and silently. Observed: a threshold added to carryover was
    present in the wheel, absent from the .app, and the mismatch only surfaced
    as an ImportError when something imported it.

    Removing the dist-info is enough to make pip treat it as not installed. It
    is done per package rather than with venvstacks --clean, which rebuilds the
    whole framework layer and costs minutes on every iteration.
    """
    names = {w.name.split("-")[0] for w in wheels_dir.glob("*.whl")}
    if not names:
        return
    removed = 0
    for info in build_dir.rglob("*.dist-info"):
        if info.name.split("-")[0] in names:
            shutil.rmtree(info, ignore_errors=True)
            removed += 1
    if removed:
        print(f"  unpinned {removed} locally built package(s) so they are reinstalled")

    _evict_dropped_local_packages(build_dir, names)


#: Names this build installed into the layer last time, so the next build can
#: tell "no longer a workspace member" from "a dependency pulled in by pip".
_LOCAL_MANIFEST = "local-packages.json"


def _evict_dropped_local_packages(build_dir: Path, current: set[str]) -> None:
    """Remove packages this build used to install and no longer does.

    venvstacks reuses its layer between builds, and nothing in it removes a
    package — installing is the only operation. So a workspace member that goes
    away stays in the layer forever: renaming amem to carryover shipped a .app
    containing both, the dead one still importable, and the rename looked
    complete everywhere else.

    Only names this build wrote itself are considered, recorded in a manifest
    beside the layer. Walking the layer and deleting anything unexpected would
    also delete pip's own transitive dependencies.
    """
    # From the parent: venvstacks builds under _build and exports under
    # _export, and it is the export that becomes the .app. Evicting only where
    # packages are installed leaves the copy that actually ships.
    root = build_dir.parent if build_dir.name.startswith("_") else build_dir
    manifest = root / _LOCAL_MANIFEST
    try:
        previous = set(json.loads(manifest.read_text(encoding="utf-8")))
    except (OSError, ValueError):
        previous = set()

    for name in sorted(previous - current):
        gone = 0
        for site in root.rglob("site-packages"):
            for path in (*site.glob(f"{name}"), *site.glob(f"{name}-*.dist-info")):
                shutil.rmtree(path, ignore_errors=True)
                gone += 1
        if gone:
            print(f"  evicted {name}: no longer built here, {gone} path(s) removed")

    manifest.parent.mkdir(parents=True, exist_ok=True)
    manifest.write_text(json.dumps(sorted(current), indent=2) + "\n", encoding="utf-8")


def _create_resolved_toml(cfg: BuildConfig, wheels_dir: Path) -> Path:
    """Substitute kimi-cli + workspace wheels into venvstacks.toml."""
    src = (ROOT / "venvstacks.toml").read_text()
    wheels = sorted(wheels_dir.glob("*.whl"))
    if not wheels:
        raise RuntimeError("no wheels were produced under packaging/wheels/")

    # Keep ordering: kosong / kaos / kimi-code / kimi-sdk first, then kimi-cli.
    # Dependencies before dependents, in the order the workspace declares them,
    # with kimi-cli last. Derived rather than transcribed for the same reason
    # as the list itself: a member missing from a hand-written order sorts to
    # the end and installs before what it needs.
    order = [d.name.replace("-", "_") for d in WORKSPACE_DIRS]

    def order_key(p: Path) -> int:
        for i, prefix in enumerate(order):
            if p.name.startswith(prefix):
                return i
        return len(order)
    wheels.sort(key=order_key)

    reqs = [f'"{w.as_uri()}"' for w in wheels]
    block = "\n    " + ",\n    ".join(reqs) + ","
    out = src.replace(
        'requirements = [\n    # kimi-cli wheel + workspace member wheels are appended here at build\n    # time by _create_resolved_toml(). The rest is pulled in transitively.\n]',
        f"requirements = [{block}\n]",
    )
    # Place the resolved spec next to the original so relative paths in
    # the TOML (e.g. launch_module = "app_main") still resolve correctly.
    resolved = ROOT / "venvstacks.resolved.toml"
    resolved.write_text(out)
    return resolved


def venvstacks_export(cfg: BuildConfig, resolved: Path) -> Path:
    section("Running venvstacks (lock / build / local-export)")
    # venvstacks resolves --build-dir / --output-dir relative to the spec file,
    # so all intermediate state lands next to packaging/.build/venvstacks.resolved.toml.
    spec = str(resolved)
    build_dir = ".build/_build"
    artifacts_dir = ".build/_artifacts"
    export_name = ".build/_export"
    export_dir = resolved.parent / export_name
    if export_dir.exists():
        shutil.rmtree(export_dir)

    # `uvx` keeps build dependencies isolated from the host Python.
    # --local-wheels lets venvstacks find wheels for sdist-only PyPI deps
    # (rumps, ripgrepy) that we built into packaging/wheels/.
    base = ["uvx", "venvstacks"]
    local_wheels = "wheels"
    run(base + ["lock", spec,
                "--build-dir", build_dir,
                "--local-wheels", local_wheels])
    _unpin_local_wheels(ROOT / build_dir, ROOT / local_wheels)
    run(base + ["build", spec,
                "--build-dir", build_dir,
                "--output-dir", artifacts_dir,
                "--local-wheels", local_wheels])
    run(base + ["local-export", spec,
                "--build-dir", build_dir,
                "--output-dir", export_name])
    return export_dir


# ---- 3. icon --------------------------------------------------------------

def _ensure_placeholder_icon(path: Path) -> None:
    if path.exists():
        return
    # 1024×1024 placeholder via Python — uses the bundled Pillow if available;
    # otherwise sips creates a solid PNG from any AppKit colour swatch.
    try:
        from PIL import Image, ImageDraw, ImageFont  # type: ignore
    except ImportError:
        # Fallback: sips can't synthesise from nothing, so just abort.
        raise RuntimeError(
            f"Icon not found at {path} and Pillow is not available to "
            "generate a placeholder. Drop a 1024×1024 PNG at packaging/icon.png "
            "or `pip install pillow` before re-running."
        )
    img = Image.new("RGBA", (1024, 1024), (40, 50, 90, 255))
    draw = ImageDraw.Draw(img)
    draw.ellipse([(140, 140), (884, 884)], fill=(70, 130, 220, 255))
    try:
        font = ImageFont.truetype("/System/Library/Fonts/SFNS.ttf", 360)
    except OSError:
        font = ImageFont.load_default()
    draw.text((512, 512), "K", fill=(255, 255, 255, 255), anchor="mm", font=font)
    path.parent.mkdir(parents=True, exist_ok=True)
    img.save(path)
    print(f"  wrote placeholder icon at {path}")


def _create_icon(cfg: BuildConfig, dest: Path) -> Path:
    section("Generating AppIcon.icns")
    _ensure_placeholder_icon(cfg.icon)
    iconset = ROOT / ".build" / "AppIcon.iconset"
    if iconset.exists():
        shutil.rmtree(iconset)
    iconset.mkdir(parents=True)
    sizes = [16, 32, 64, 128, 256, 512, 1024]
    for s in sizes:
        out = iconset / f"icon_{s}x{s}.png"
        run(["sips", "-z", str(s), str(s), str(cfg.icon),
             "--out", str(out)],
            stdout=subprocess.DEVNULL)
        if s in (16, 32, 64, 128, 256, 512):
            out2 = iconset / f"icon_{s}x{s}@2x.png"
            run(["sips", "-z", str(s * 2), str(s * 2), str(cfg.icon),
                 "--out", str(out2)],
                stdout=subprocess.DEVNULL)
    icns = dest
    icns.parent.mkdir(parents=True, exist_ok=True)
    run(["iconutil", "-c", "icns", "-o", str(icns), str(iconset)])

    # Menu-bar icon: a small PNG sized for the status item (22pt @ 2x = 44px).
    # rumps loads this via NSImage; we keep it colored (not template) so the
    # brand is recognisable even on the dark menu bar.
    menubar = dest.parent / "MenuBarIcon.png"
    run(["sips", "-z", "44", "44", str(cfg.icon),
         "--out", str(menubar)],
        stdout=subprocess.DEVNULL)
    return icns


# ---- 4. C launcher --------------------------------------------------------

def compile_launcher(cfg: BuildConfig, output: Path) -> None:
    section("Compiling C launcher")
    output.parent.mkdir(parents=True, exist_ok=True)
    run([
        "cc",
        "-arch", cfg.arch,
        f"-mmacosx-version-min={cfg.min_macos}",
        "-O2",
        f'-DCPYTHON_PREFIX="cpython-{cfg.py_version}"',
        "-o", str(output),
        str(ROOT / "launcher.c"),
    ])


# ---- 5. Info.plist + brand.json + Credits + CLI wrapper -------------------

def _data_url(path: Path, max_bytes: int) -> str:
    raw = path.read_bytes()
    if len(raw) > max_bytes:
        raise RuntimeError(f"{path} is {len(raw)} bytes; limit is {max_bytes}")
    mime = "image/png"
    if path.suffix.lower() in (".jpg", ".jpeg"):
        mime = "image/jpeg"
    elif path.suffix.lower() == ".svg":
        mime = "image/svg+xml"
    elif path.suffix.lower() == ".ico":
        mime = "image/x-icon"
    return f"data:{mime};base64,{base64.b64encode(raw).decode('ascii')}"


def write_info_plist(cfg: BuildConfig, contents: Path) -> None:
    plist = {
        "CFBundleName": cfg.app_name,
        "CFBundleDisplayName": cfg.app_name,
        "CFBundleIdentifier": cfg.bundle_id,
        "CFBundleExecutable": cfg.app_name,
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": cfg.version,
        "CFBundleVersion": cfg.build_number,
        "CFBundleIconFile": "AppIcon",
        "LSMinimumSystemVersion": cfg.min_macos,
        "LSUIElement": False,  # menu bar accessory; True hides Dock icon entirely
        "NSHighResolutionCapable": True,
        "NSHumanReadableCopyright": cfg.copyright,
        "NSAppTransportSecurity": {"NSAllowsLocalNetworking": True},
        "NSSupportsAutomaticGraphicsSwitching": True,
    }
    (contents / "Info.plist").write_bytes(plistlib.dumps(plist))


def write_brand_json(cfg: BuildConfig, resources: Path) -> None:
    seed = {
        "brand_name": cfg.brand_name,
        "page_title": cfg.page_title,
        "version": cfg.version,
    }
    if cfg.logo:
        seed["logo"] = _data_url(cfg.logo, LOGO_MAX_BYTES)
    if cfg.favicon:
        seed["favicon"] = _data_url(cfg.favicon, FAVICON_MAX_BYTES)

    data = {
        "app_name": cfg.app_name,
        "slug": cfg.slug,
        "bundle_id": cfg.bundle_id,
        "display_name": cfg.app_name,
        "copyright": cfg.copyright,
        "version": cfg.version,
        "build_number": cfg.build_number,
        "branding_seed": seed,
    }
    (resources / "brand.json").write_text(json.dumps(data, indent=2))


def write_credits_rtf(cfg: BuildConfig, resources: Path) -> None:
    md = (ROOT / "credits.md").read_text().replace("{APP_NAME}", cfg.app_name)
    # Minimal RTF: paragraph per line, no rich formatting. Sufficient for
    # the standard About panel which only renders plain text.
    lines = [r"{\rtf1\ansi\ansicpg1252\cocoartf2580", r"\fs28"]
    for raw in md.splitlines():
        line = raw.replace("\\", r"\\").replace("{", r"\{").replace("}", r"\}")
        lines.append(f"{line}\\par")
    lines.append("}")
    (resources / "Credits.rtf").write_text("\n".join(lines))


def write_cli_wrapper(cfg: BuildConfig, resources: Path) -> None:
    # Lives under Resources/, not MacOS/, so codesign doesn't try to validate
    # it as a Mach-O sibling of the main launcher.
    tmpl = (ROOT / "cli-wrapper.sh.template").read_text()
    rendered = (
        tmpl.replace("{APP_NAME}", cfg.app_name)
            .replace("{SLUG}", cfg.slug)
            .replace("{PY_VERSION}", cfg.py_version)
    )
    target = resources / f"{cfg.slug}-cli"
    target.write_text(rendered)
    target.chmod(0o755)


# ---- 6. assemble ----------------------------------------------------------

def copy_kimi_cli_source(resources: Path) -> None:
    section("Copying kimi_cli source into Resources/")
    src = KIMI_CLI / "src" / "kimi_cli"
    dest = resources / "kimi_cli"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


def copy_app_main(resources: Path) -> None:
    src = ROOT / "app_main"
    dest = resources / "app_main"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(src, dest, ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))


def copy_runtimes(export_dir: Path, runtimes: Path) -> None:
    section("Copying venvstacks layers into Resources/runtimes/")
    if runtimes.exists():
        shutil.rmtree(runtimes)
    runtimes.mkdir(parents=True)
    # venvstacks local-export emits one directory per layer.
    for entry in export_dir.iterdir():
        if entry.is_dir():
            shutil.copytree(entry, runtimes / entry.name, symlinks=True)
    _drop_layered_kimi_cli(runtimes)
    _run_layer_postinstall(runtimes)


def _drop_layered_kimi_cli(runtimes: Path) -> None:
    """Remove kimi_cli from the layers; Resources/kimi_cli is the only copy.

    The framework layer installs the kimi-cli wheel to pull in its dependency
    tree, and gets the package's own code as a side effect. That copy is a
    liability twice over.

    It is stale, and stays stale: the wheel's version never changes, so the
    resolver sees kimi-cli==1.49.0 already installed and skips it however much
    the contents moved. The copy shipped in the last build was months behind —
    its dist-info listed seven modules under `memory/` where the wheel has
    thirteen.

    And it is only ever shadowed, never used. The launcher puts Resources/
    first on PYTHONPATH precisely so the source copy wins, which means the
    layered one contributes nothing but the chance of winning some day — after
    a change to path order, or when anything runs the bundled Python directly.
    Both would be silent: the app would simply behave like an older version.

    The dist-info stays. Only the package directory goes.

    That distinction is the whole fix: `kimi_cli.constant` asks
    `importlib.metadata.version("kimi-cli")` for the version it puts in the
    User-Agent, and that reads the dist-info, not the package. Removing both
    imported cleanly from Resources/ and then died at
    `PackageNotFoundError: No package metadata was found for kimi-cli` — the
    server came up crashed with the code perfectly in place.

    A dist-info whose RECORD lists files that are gone is untidy and harmless:
    nothing verifies RECORD at runtime, and the metadata's job here is to
    answer one question about a version.
    """
    removed = []
    for layer in runtimes.iterdir():
        site = layer / "lib"
        if not site.is_dir():
            continue
        for packages in site.glob("python*/site-packages"):
            package = packages / "kimi_cli"
            if package.is_dir():
                shutil.rmtree(package)
                removed.append(f"{layer.name}/kimi_cli")
    if removed:
        print(f"  removed shadowed copies: {', '.join(removed)}")


def _run_layer_postinstall(runtimes: Path) -> None:
    """Each venvstacks layer ships a postinstall.py that generates pyvenv.cfg
    and sitecustomize.py so the upper layers' sys.path resolves into the
    lower ones. Without running these, the bundled Python only sees its own
    stdlib and `import rumps` (which lives in the app layer) fails.

    Use the bundled CPython (from the runtime layer) to run them, since the
    host Python may not exist on a fresh machine.
    """
    cpython = next(
        (p for p in runtimes.iterdir() if p.name.startswith("cpython-")),
        None,
    )
    if cpython is None:
        return
    py = cpython / "bin" / "python3"
    for layer in sorted(runtimes.iterdir()):
        script = layer / "postinstall.py"
        if script.exists():
            run([str(py), str(script)], cwd=str(layer))


def create_app_bundle(cfg: BuildConfig, export_dir: Path) -> Path:
    section(f"Assembling {cfg.app_name}.app")
    bundle = cfg.output_dir / f"{cfg.app_name}.app"
    if bundle.exists():
        shutil.rmtree(bundle)
    contents = bundle / "Contents"
    macos = contents / "MacOS"
    resources = contents / "Resources"
    # venvstacks layers go under Resources/runtimes/, not Contents/Frameworks/.
    # Frameworks/ is reserved for proper macOS framework bundles, and codesign
    # rejects venv-style directories there ("bundle format unrecognized").
    runtimes = resources / "runtimes"
    macos.mkdir(parents=True)
    resources.mkdir(parents=True)
    runtimes.mkdir(parents=True)

    write_info_plist(cfg, contents)

    compile_launcher(cfg, macos / cfg.app_name)
    write_cli_wrapper(cfg, resources)

    _create_icon(cfg, resources / "AppIcon.icns")
    write_brand_json(cfg, resources)
    write_credits_rtf(cfg, resources)
    license_src = REPO / "LICENSE"
    if license_src.exists():
        shutil.copy2(license_src, resources / "LICENSE.txt")

    copy_app_main(resources)
    copy_kimi_cli_source(resources)

    if not cfg.skip_venv:
        copy_runtimes(export_dir, runtimes)
    elif export_dir and export_dir.exists():
        # When skipping venv, reuse whatever is on disk if available.
        copy_runtimes(export_dir, runtimes)

    # A freshly built Vite dist always wins over the committed pre-built
    # artefact. Otherwise the version-string baked into the committed copy
    # at last commit would override whatever the current kimi-cli version is.
    static_src = KIMI_CLI / "web" / "dist"           # fresh Vite build output
    static_alt = KIMI_CLI / "src" / "kimi_cli" / "web" / "static"  # committed pre-built fallback
    static_dest = resources / "kimi_cli" / "web" / "static"
    if static_src.exists() and any(static_src.iterdir()):
        # Fresh Vite build — replace whatever copy_kimi_cli_source brought over.
        if static_dest.exists():
            shutil.rmtree(static_dest)
        shutil.copytree(static_src, static_dest)
    elif static_alt.exists() and any(static_alt.iterdir()):
        # No fresh build; fall back to the committed pre-built artefact already
        # copied by copy_kimi_cli_source. No-op.
        pass
    else:
        print("  ! frontend not built; KIMI_WEB_STATIC_DIR will be empty.\n"
              "    Run `cd kimi-cli/web && npm install && npm run build` "
              "before packaging, or remove --skip-frontend.")

    return bundle


# ---- 7. sign + DMG --------------------------------------------------------

def _macho_min_os(path: Path) -> str | None:
    """Return the minimum macOS version a Mach-O file declares, if any."""
    try:
        out = subprocess.run(
            ["otool", "-l", str(path)],
            capture_output=True, text=True, check=True,
        ).stdout
    except (subprocess.CalledProcessError, OSError):
        return None
    for line in out.splitlines():
        fields = line.split()
        # LC_BUILD_VERSION carries "minos X.Y"; older LC_VERSION_MIN_MACOSX
        # carries "version X.Y".
        if len(fields) == 2 and fields[0] in ("minos", "version"):
            return fields[1]
    return None


def _version_tuple(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split(".") if part.isdigit())


def verify_min_macos(cfg: BuildConfig, bundle: Path) -> None:
    """Fail the build if anything shipped needs a newer macOS than we advertise.

    LSMinimumSystemVersion is a promise, not a filter: macOS only refuses to
    launch apps that ask for *more* than the host. Advertising a floor lower
    than the bundled CPython or a native wheel actually supports produces an
    app that installs on an old Mac and then dies at import time, which is far
    harder to diagnose than a refusal to install.
    """
    section(f"Verifying every binary supports macOS {cfg.min_macos}")
    declared = _version_tuple(cfg.min_macos)
    offenders: list[tuple[Path, str]] = []
    checked = 0
    for path in bundle.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        if path.suffix not in (".so", ".dylib", "") :
            continue
        minimum = _macho_min_os(path)
        if minimum is None:
            continue
        checked += 1
        if _version_tuple(minimum) > declared:
            offenders.append((path.relative_to(bundle), minimum))
    if offenders:
        print(f"!! {len(offenders)} binary/binaries need a newer macOS than "
              f"{cfg.min_macos}:", file=sys.stderr)
        for rel, minimum in offenders[:20]:
            print(f"     {minimum:>6}  {rel}", file=sys.stderr)
        if len(offenders) > 20:
            print(f"     ... and {len(offenders) - 20} more", file=sys.stderr)
        raise SystemExit(
            f"Refusing to ship an app that advertises macOS {cfg.min_macos} "
            f"but cannot run there. Raise min_macos_version in brand.toml, or "
            f"rebuild the offending dependencies against an older deployment "
            f"target."
        )
    print(f"  {checked} Mach-O file(s) checked; all support macOS {cfg.min_macos} or older")


def sign_app(cfg: BuildConfig, bundle: Path) -> None:
    section(f"Codesigning ({cfg.sign_identity})")
    # `codesign --deep` mis-detects venvstacks' lib/pythonX.Y directories as
    # broken frameworks. Sign every Mach-O artifact explicitly instead, then
    # seal the bundle without recursion.
    is_adhoc = cfg.sign_identity == "-"
    base = ["codesign", "--force", "--sign", cfg.sign_identity, "--no-strict"]
    if not is_adhoc:
        base += ["--options", "runtime", "--timestamp"]

    lib_targets: list[Path] = []
    for ext in (".dylib", ".so"):
        lib_targets.extend(sorted(bundle.rglob(f"*{ext}")))
    # Mach-O executables in bin/ directories become processes when spawned
    # (e.g. supervisor → app-layer python wrapper → cpython interpreter), so
    # they need the entitlement to dlopen ad-hoc-signed extension .so files
    # without macOS rejecting the load as "different Team IDs".
    exe_targets: list[Path] = []
    for bin_dir in bundle.rglob("bin"):
        if bin_dir.is_dir():
            for entry in sorted(bin_dir.iterdir()):
                if entry.is_file() and not entry.is_symlink() and os.access(entry, os.X_OK):
                    exe_targets.append(entry)

    launcher = bundle / "Contents" / "MacOS" / cfg.app_name

    # Sign libraries first (no entitlements; they're loaded, not run).
    for path in lib_targets:
        run(base + [str(path)])

    # The disable-library-validation entitlement is required on every binary
    # that may become a running process loading ad-hoc-signed extension
    # modules — the launcher *and* every cpython interpreter in bin/.
    with tempfile.NamedTemporaryFile(
        mode="w", suffix=".plist", delete=False
    ) as ent:
        ent.write(_LAUNCHER_ENTITLEMENTS)
        ent_path = ent.name
    try:
        ent_args = ["--entitlements", ent_path]
        for path in exe_targets:
            run(base + ent_args + [str(path)])
        run(base + ent_args + [str(launcher)])
        # Seal the .app bundle itself last. Pass `--entitlements` again so
        # that codesign's implicit re-signing of the bundle's main executable
        # preserves the disable-library-validation entitlement; without this,
        # the launcher ends up with an entitlement-less signature and dlopen
        # of ad-hoc-signed libpython gets rejected on first launch.
        run(base + ent_args + [str(bundle)])
    finally:
        os.unlink(ent_path)


_LAUNCHER_ENTITLEMENTS = """<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>com.apple.security.cs.disable-library-validation</key>
  <true/>
  <key>com.apple.security.cs.allow-dyld-environment-variables</key>
  <true/>
  <key>com.apple.security.cs.allow-unsigned-executable-memory</key>
  <true/>
</dict>
</plist>
"""


def create_dmg(cfg: BuildConfig, bundle: Path) -> Path:
    section("Building DMG")
    cfg.output_dir.mkdir(parents=True, exist_ok=True)
    dmg = cfg.output_dir / f"{cfg.app_name}-{cfg.version}-{cfg.arch}.dmg"
    if dmg.exists():
        dmg.unlink()
    with tempfile.TemporaryDirectory(prefix="kimi-dmg-") as tmp:
        staging = Path(tmp) / "staging"
        staging.mkdir()
        shutil.copytree(bundle, staging / bundle.name, symlinks=True)
        (staging / "Applications").symlink_to("/Applications")
        run([
            "hdiutil", "create",
            "-volname", cfg.app_name,
            "-srcfolder", str(staging),
            "-ov", "-format", "UDZO",
            str(dmg),
        ])
    return dmg


# ---- main -----------------------------------------------------------------

def verify_bundle_matches_source(bundle: Path) -> None:
    """Fail if the bundle carries different code than the tree it was built from.

    Every packaging bug here has been silent. pip skipped a same-version wheel
    and shipped a .app whose source differed from the repository's; the layer
    kept a workspace member that had been renamed away, so both amem and
    carryover shipped and the dead one stayed importable. Neither raised, and
    both were found by accident well after the fact.

    So it is asked directly, against the file rather than the version string: a
    version can match while the contents do not, which is exactly what the pip
    skip did. What is compared is every workspace member's package directory,
    byte for byte.
    """
    section("Verifying the bundle matches the source")
    # Every layer, not the first found: venvstacks builds three (cpython, the
    # shared framework, the app) and the workspace members live in the middle
    # one. Taking whichever rglob returned first reported all six packages
    # missing from a bundle that had them.
    sites = sorted((bundle / "Contents/Resources/runtimes").rglob("site-packages"))
    if not sites:
        raise RuntimeError(f"no site-packages inside {bundle}")
    # kimi_cli is laid next to the runtimes rather than installed into one, so
    # a search of site-packages alone reports the application itself missing.
    sites.append(bundle / "Contents/Resources")

    def installed(name: str) -> Path | None:
        return next((site / name for site in sites if (site / name).is_dir()), None)

    problems: list[str] = []
    for src in WORKSPACE_DIRS:
        for package in (src / "src").glob("*/"):
            if not (package / "__init__.py").is_file():
                continue
            shipped = installed(package.name)
            if shipped is None:
                problems.append(f"{package.name}: in the workspace, absent from the bundle")
                continue
            for path in sorted(package.rglob("*.py")):
                mirror = shipped / path.relative_to(package)
                if not mirror.is_file():
                    problems.append(f"{package.name}: {path.relative_to(package)} missing")
                elif mirror.read_bytes() != path.read_bytes():
                    problems.append(f"{package.name}: {path.relative_to(package)} differs")

    # The other direction: something built here once, still installed, and no
    # longer in the workspace — how a renamed package survives in the layer.
    # Found through direct_url.json, which pip writes only for a distribution
    # installed from a path, so the hundreds of packages that came from PyPI
    # are not candidates and do not have to be enumerated.
    built_here = {w.name.split("-")[0].replace("_", "-").lower() for w in (ROOT / "wheels").glob("*.whl")}
    for info in sorted(i for site in sites for i in site.glob("*.dist-info")):
        if not (info / "direct_url.json").is_file():
            continue
        try:
            url = json.loads((info / "direct_url.json").read_text(encoding="utf-8")).get("url", "")
        except ValueError:
            continue
        if not url.startswith("file://"):
            continue
        name = info.name.split("-")[0].replace("_", "-").lower()
        if name not in built_here:
            problems.append(f"{name}: installed from a local wheel this build no longer produces")

    # The web UI is not a Python package, so the comparison above never saw
    # it — which is how a frontend from five days earlier shipped inside a
    # bundle whose Python was current.
    shipped_static = bundle / "Contents" / "Resources" / "kimi_cli" / "web" / "static"
    source_static = KIMI_CLI / "src" / "kimi_cli" / "web" / "static"
    if source_static.is_dir():
        if not shipped_static.is_dir():
            problems.append("web/static: built here, absent from the bundle")
        else:
            for path in sorted(source_static.rglob("*")):
                if not path.is_file():
                    continue
                mirror = shipped_static / path.relative_to(source_static)
                if not mirror.is_file():
                    problems.append(f"web/static: {path.relative_to(source_static)} missing")
                elif mirror.read_bytes() != path.read_bytes():
                    problems.append(f"web/static: {path.relative_to(source_static)} differs")

    if problems:
        raise RuntimeError(
            "the bundle does not match the source:\n  " + "\n  ".join(problems[:20])
        )
    print(f"  {len(built_here)} locally built package(s) match the source byte for byte")
    print("  web/static matches what npm run build produced")


def main(argv: list[str]) -> int:
    cfg = parse_args(argv)
    cfg.output_dir.mkdir(parents=True, exist_ok=True)

    print(f"App:        {cfg.app_name} ({cfg.slug})")
    print(f"Bundle ID:  {cfg.bundle_id}")
    print(f"Version:    {cfg.version} ({cfg.build_number})")
    print(f"Python:     {cfg.py_version}")
    print(f"Arch:       {cfg.arch}")
    print(f"Output:     {cfg.output_dir}")

    if cfg.arch != platform.machine():
        print(f"!! Warning: --arch {cfg.arch} differs from host {platform.machine()}; venvstacks layers will follow the host platform and the resulting app likely won't run on the target arch.", file=sys.stderr)

    if cfg.dmg_only:
        bundle = cfg.output_dir / f"{cfg.app_name}.app"
        if not bundle.exists():
            print(f"!! {bundle} does not exist; build it first.", file=sys.stderr)
            return 2
        create_dmg(cfg, bundle)
        return 0

    if not cfg.skip_frontend:
        build_frontend(cfg)

    if not cfg.skip_venv:
        wheels_dir = build_local_wheels(cfg)
        resolved = _create_resolved_toml(cfg, wheels_dir)
        export_dir = venvstacks_export(cfg, resolved)
    else:
        export_dir = ROOT / ".build" / "_export"

    bundle = create_app_bundle(cfg, export_dir)
    verify_min_macos(cfg, bundle)
    verify_bundle_matches_source(bundle)
    sign_app(cfg, bundle)
    dmg = create_dmg(cfg, bundle)
    print(f"\n✓ {bundle}")
    print(f"✓ {dmg}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
