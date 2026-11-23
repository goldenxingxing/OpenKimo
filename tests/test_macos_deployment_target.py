"""The advertised macOS floor must be reachable by Intel Macs and honest.

`LSMinimumSystemVersion` is a promise, not a filter. Set it too high and Macs
that could run the app refuse to install it; set it below what the shipped
binaries support and the app installs on an old Mac and then dies at import.
Both directions are regressions, so both are pinned here.
"""

from __future__ import annotations

import importlib.util
import os
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

    assert "-mmacosx-version-min={cfg.min_macos}" in source
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


# ---------------------------------------------------------------------------
# Shell wrappers must not need a newer macOS than the binaries do
# ---------------------------------------------------------------------------


VENVSTACKS_WRAPPER = """\
#!/bin/zsh
set -eu
# Note: `readlink -f` (long available in GNU coreutils) is available on macOS 12.3 and later
script_dir="$(cd "$(dirname "$(readlink -f "$0")")" 1> /dev/null 2>&1 && pwd)"
script_path="$script_dir/python"
test -f "$script_path" || { echo 1>&2 "Invalid wrapper script path: $script_path"; exit 1; }
exec -a "$script_path" "$script_dir/python_" "$@"
"""


@pytest.mark.skipif(sys.platform != "darwin", reason="POSIX wrapper exec bit / readlink -f")
def test_the_build_rewrites_readlink_f_out_of_layer_wrappers(tmp_path: Path) -> None:
    """`readlink -f` is GNU; BSD readlink only learned it in macOS 12.3.

    venvstacks emits it in every layer's bin/python — its own comment says so.
    Below 12.3 it prints "illegal option -- f" and nothing to stdout, so
    dirname sees "", cd lands on the process's cwd, and the wrapper exits 1
    with "Invalid wrapper script path: <cwd>/python". app_main/server.py spawns
    uvicorn with cwd=~/Library/Application Support/<App>, which is exactly the
    path users reported.

    LSMinimumSystemVersion is 11.0 — deliberately, per the floor pinned at the
    top of this file — so the app installs on those machines and then cannot
    start its server.
    """
    runtimes = tmp_path / "runtimes"
    for layer in ("cpython-3.12", "framework-framework-kimo", "app-app-openkimo"):
        wrapper = runtimes / layer / "bin" / "python"
        wrapper.parent.mkdir(parents=True)
        wrapper.write_text(VENVSTACKS_WRAPPER, encoding="utf-8")
        wrapper.chmod(0o755)

    build = _build_module()
    build.make_wrappers_portable(runtimes)

    for wrapper in runtimes.glob("*/bin/python"):
        text = wrapper.read_text(encoding="utf-8")
        assert "readlink -f" not in text.replace("# Note: `readlink -f`", "").replace(
            "venvstacks emits `readlink -f", ""
        ), wrapper
        assert 'while [ -h "$_wrapper_target" ]' in text, wrapper
        assert wrapper.stat().st_mode & 0o111, f"{wrapper} lost its executable bit"


@pytest.mark.skipif(not Path("/bin/zsh").exists(), reason="needs the macOS zsh at /bin/zsh")
def test_the_portable_wrapper_resolves_the_same_directory(tmp_path: Path) -> None:
    """Run the rewritten wrapper with a readlink that predates macOS 12.3."""
    runtimes = tmp_path / "runtimes"
    wrapper = runtimes / "app-x" / "bin" / "python"
    wrapper.parent.mkdir(parents=True)
    wrapper.write_text(VENVSTACKS_WRAPPER, encoding="utf-8")
    wrapper.chmod(0o755)
    # What the wrapper execs; any executable will do.
    (wrapper.parent / "python_").symlink_to("/bin/echo")

    build = _build_module()
    build.make_wrappers_portable(runtimes)

    # A readlink without -f, as shipped before macOS 12.3.
    fake_bin = tmp_path / "fakebin"
    fake_bin.mkdir()
    fake_readlink = fake_bin / "readlink"
    fake_readlink.write_text(
        "#!/bin/sh\n"
        'case "$1" in -f*) echo "readlink: illegal option -- f" >&2; exit 1 ;; esac\n'
        'exec /usr/bin/readlink "$@"\n',
        encoding="utf-8",
    )
    fake_readlink.chmod(0o755)

    elsewhere = tmp_path / "cwd-the-server-uses"
    elsewhere.mkdir()
    env = {**os.environ, "PATH": f"{fake_bin}:{os.environ['PATH']}"}
    result = subprocess.run(
        ["/bin/zsh", str(wrapper), "MARKER"],
        cwd=elsewhere,
        env=env,
        capture_output=True,
        text=True,
        timeout=30,
    )

    assert "Invalid wrapper script path" not in result.stderr, result.stderr
    assert result.returncode == 0, result.stderr
    assert "MARKER" in result.stdout
