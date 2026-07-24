from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CANONICAL_ICON = ROOT / "kimi-cli" / "web" / "public" / "logo.png"
LEGACY_ICON_SHA256 = (
    "dbd00e2ad61ea8832ef0b024662a4a8a5d1b66f0599d5d42e1c9688b9d4cfdf6"
)
BUILD_WINDOWS_PATH = ROOT / "packaging" / "build_windows.py"
SPEC = importlib.util.spec_from_file_location("openkimo_build_windows", BUILD_WINDOWS_PATH)
build_windows = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = build_windows
SPEC.loader.exec_module(build_windows)


def _build_config(tmp_path: Path) -> build_windows.BuildConfig:
    return build_windows.BuildConfig(
        app_name="OpenKimo",
        slug="openkimo",
        version="0.1.19",
        build_number="2026.07.24.1",
        copyright="OpenKimo",
        py_version="3.12",
        icon=CANONICAL_ICON,
        logo=CANONICAL_ICON,
        favicon=CANONICAL_ICON,
        brand_name="OpenKimo",
        page_title="OpenKimo",
        output_dir=tmp_path,
    )


def test_windows_brand_json_contains_canonical_web_assets(tmp_path: Path) -> None:
    output = tmp_path / "brand.json"

    build_windows.write_brand_json(_build_config(tmp_path), output)

    data = json.loads(output.read_text())
    seed = data["branding_seed"]
    assert seed["logo"].startswith("data:image/png;base64,")
    assert seed["favicon"] == seed["logo"]
    assert data["branding_legacy_asset_sha256"] == [LEGACY_ICON_SHA256]


def test_windows_brand_paths_use_the_canonical_icon() -> None:
    config = build_windows.parse_args([])

    assert config.logo == CANONICAL_ICON
    assert config.favicon == CANONICAL_ICON
