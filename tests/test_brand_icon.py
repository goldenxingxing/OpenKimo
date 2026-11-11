from __future__ import annotations

import struct
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CANONICAL_ICON = ROOT / "web" / "public" / "logo.png"
OLD_PACKAGING_ICON = ROOT / "packaging" / "icon.png"
BRAND_CONFIG = ROOT / "packaging" / "brand.toml"


def _png_dimensions(path: Path) -> tuple[int, int]:
    data = path.read_bytes()
    assert data[:8] == b"\x89PNG\r\n\x1a\n"
    assert data[12:16] == b"IHDR"
    return struct.unpack(">II", data[16:24])


def test_builtin_brand_icon_has_one_canonical_source() -> None:
    brand = tomllib.loads(BRAND_CONFIG.read_text())
    expected_path = "../web/public/logo.png"

    assert brand["app"]["icon"] == expected_path
    assert brand["app"]["logo"] == expected_path
    # the favicon and the menu-bar icon are derived from it (derive_brand_icons.py)
    assert brand["app"]["favicon"] == "../web/public/favicon.png"
    assert brand["app"]["menubar_icon"] == "MenuBarIconTemplate.png"
    assert CANONICAL_ICON.is_file()
    assert _png_dimensions(CANONICAL_ICON) == (1024, 1024)
    assert not OLD_PACKAGING_ICON.exists()
