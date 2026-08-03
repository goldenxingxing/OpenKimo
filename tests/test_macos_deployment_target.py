"""The advertised macOS floor must be reachable by Intel Macs and honest.

`LSMinimumSystemVersion` is a promise, not a filter. Set it too high and Macs
that could run the app refuse to install it; set it below what the shipped
binaries support and the app installs on an old Mac and then dies at import.
Both directions are regressions, so both are pinned here.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
import tomllib
import types
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BRAND = ROOT / "packaging" / "brand.toml"
BUILD_PY = ROOT / "packaging" / "build.py"

# The newest macOS that every still-supported Intel Mac can reach. Many Intel
# machines top out at Monterey (12) or Ventura (13); requiring Sonoma (14) shut
# all of them out.
MAX_INTEL_REACHABLE = (12, 0)


def _version(value: str) -> tuple[int, ...]:
    return tuple(int(part) for part in value.split(".") if part.isdigit())


def _build_module() -> types.ModuleType:
    spec = importlib.util.spec_from_file_location("packaging_build", BUILD_PY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["packaging_build"] = module
    spec.loader.exec_module(module)
    return module


def test_declared_floor_is_reachable_by_intel_macs() -> None:
    config = tomllib.loads(BRAND.read_text(encoding="utf-8"))
    declared = config["app"]["min_macos_version"]

    assert _version(declared) <= MAX_INTEL_REACHABLE, (
        f"min_macos_version={declared} locks out Intel Macs that cannot run "
        f"macOS {MAX_INTEL_REACHABLE[0]}"
    )


def test_the_launcher_is_compiled_against_the_declared_floor() -> None:
    """The floor must reach the compiler, not just Info.plist."""
    source = BUILD_PY.read_text(encoding="utf-8")

    assert '-mmacosx-version-min={cfg.min_macos}' in source
    assert '"LSMinimumSystemVersion": cfg.min_macos' in source


def test_the_build_verifies_the_floor_before_signing() -> None:
    source = BUILD_PY.read_text(encoding="utf-8")
    verify_at = source.index("verify_min_macos(cfg, bundle)")
    sign_at = source.index("sign_app(cfg, bundle)", verify_at)

    assert verify_at < sign_at


@pytest.mark.skipif(sys.platform != "darwin", reason="needs otool")
def test_verification_rejects_a_binary_that_needs_a_newer_macos(tmp_path: Path) -> None:
    """Build a real Mach-O that over-shoots the floor and expect a refusal."""
    source = tmp_path / "probe.c"
    source.write_text("int main(void) { return 0; }\n", encoding="utf-8")
    bundle = tmp_path / "Probe.app"
    macos_dir = bundle / "Contents" / "MacOS"
    macos_dir.mkdir(parents=True)
    try:
        subprocess.run(
            ["cc", "-mmacosx-version-min=14.0", "-o", str(macos_dir / "Probe"), str(source)],
            check=True,
            capture_output=True,
        )
    except (subprocess.CalledProcessError, FileNotFoundError):  # pragma: no cover
        pytest.skip("no working C toolchain")

    build = _build_module()

    with pytest.raises(SystemExit, match="Refusing to ship"):
        build.verify_min_macos(types.SimpleNamespace(min_macos="11.0"), bundle)

    # ...and accepts it once the floor genuinely covers it.
    build.verify_min_macos(types.SimpleNamespace(min_macos="14.0"), bundle)
