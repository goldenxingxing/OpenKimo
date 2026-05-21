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


# ---- 1. wheels ------------------------------------------------------------

WORKSPACE_DIRS = (
    KIMI_CLI / "packages" / "kosong",
    KIMI_CLI / "packages" / "kaos",
    KIMI_CLI / "packages" / "kimi-code",
    KIMI_CLI / "sdks" / "kimi-sdk",
    KIMI_CLI,
)

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

def _create_resolved_toml(cfg: BuildConfig, wheels_dir: Path) -> Path:
    """Substitute kimi-cli + workspace wheels into venvstacks.toml."""
    src = (ROOT / "venvstacks.toml").read_text()
    wheels = sorted(wheels_dir.glob("*.whl"))
    if not wheels:
        raise RuntimeError("no wheels were produced under packaging/wheels/")

    # Keep ordering: kosong / kaos / kimi-code / kimi-sdk first, then kimi-cli.
    def order_key(p: Path) -> int:
        for i, prefix in enumerate(("kosong", "kaos", "kimi_code", "kimi_sdk", "kimi_cli")):
            if p.name.startswith(prefix):
                return i
        return 99
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
    _run_layer_postinstall(runtimes)


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
    sign_app(cfg, bundle)
    dmg = create_dmg(cfg, bundle)
    print(f"\n✓ {bundle}")
    print(f"✓ {dmg}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
