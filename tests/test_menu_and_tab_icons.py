"""The icons on every platform, and the Windows tray click.

The brand source is a full square. Used as it was, it was a hard-cornered square
everywhere and a coloured square in the macOS menu bar, where icons are monochrome
shapes that follow a light or dark bar. Every icon now has rounded corners, except
the menu bar's template. And on Windows a left click on the tray icon ran the
default menu item, "Open Web UI", so every click opened another browser tab.
"""

from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import sqlite3
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest
from PIL import Image

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "packaging" / "LogoSource.png"
LOGO = ROOT / "kimi-cli" / "web" / "public" / "logo.png"
FAVICON = ROOT / "kimi-cli" / "web" / "public" / "favicon.png"
APP_ICON = ROOT / "packaging" / "AppIcon.png"
MENUBAR = ROOT / "packaging" / "MenuBarIconTemplate.png"
# what earlier builds shipped: the square logo, then the round favicon
SQUARE_LOGO_SHA256 = "a30b84efe092999d20b4b7f78b1b99c8a2d1e1da72e3fa09f853bf68b616f0af"
ROUND_FAVICON_SHA256 = "4f428414c9abb5bc592de38941460a3ec1cbfedc3d49803fe0973680bf86d6f3"


def _load(name: str, path: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize("path", [LOGO, FAVICON], ids=["logo", "favicon"])
def test_the_logo_and_the_favicon_have_rounded_corners(path: Path) -> None:
    image = Image.open(path).convert("RGBA")
    width, height = image.size

    assert width == height
    for corner in ((0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1)):
        assert image.getpixel(corner)[3] == 0
    # a rounded square, not a disc: the navy reaches the middle of every edge
    for edge in ((width // 2, 0), (0, height // 2), (width - 1, height // 2)):
        assert image.getpixel(edge)[3] == 255
    assert image.getpixel((width // 2, height // 2))[3] == 255


def test_the_macos_app_icon_sits_on_apples_grid() -> None:
    image = Image.open(APP_ICON).convert("RGBA")

    assert image.size == (1024, 1024)
    assert image.getpixel((512, 512))[3] == 255
    assert image.getpixel((512, 101))[3] == 255  # the body starts 100 px in
    assert image.getpixel((512, 40))[3] == 0  # the margin around it is clear
    assert image.getpixel((110, 110))[3] < 255  # its corners are rounded off


def test_the_menu_bar_icon_is_a_monochrome_template_shape() -> None:
    image = Image.open(MENUBAR).convert("RGBA")
    pixels = list(image.getdata())

    assert image.size == (44, 44)
    assert {p[:3] for p in pixels if p[3] > 0} == {(0, 0, 0)}  # colour comes from macOS
    assert image.getpixel((0, 0))[3] == 0
    assert image.getpixel((22, 8))[3] == 255  # the disc
    # the K is cut out of the disc
    inside = [image.getpixel((x, 22))[3] for x in range(12, 32)]
    assert min(inside) == 0 and max(inside) == 255


def test_the_committed_icons_are_what_the_script_makes(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    derive = _load("openkimo_derive_brand_icons", ROOT / "packaging" / "derive_brand_icons.py")
    committed = {"LOGO": LOGO, "FAVICON": FAVICON, "APP_ICON": APP_ICON, "MENUBAR": MENUBAR}
    for name, path in committed.items():
        monkeypatch.setattr(derive, name, tmp_path / path.name)
    monkeypatch.setattr(derive, "ROOT", tmp_path)

    derive.main()

    for path in committed.values():
        made = Image.open(tmp_path / path.name)
        assert list(made.getdata()) == list(Image.open(path).getdata()), path.name


def test_the_macos_build_uses_the_inset_app_icon() -> None:
    assert build.parse_args([]).icon == APP_ICON.resolve()


def test_the_web_ui_points_at_the_round_favicon() -> None:
    index = (ROOT / "kimi-cli" / "web" / "index.html").read_text()
    branding = (ROOT / "kimi-cli" / "web" / "src" / "hooks" / "useBranding.tsx").read_text()

    assert '<link rel="icon" type="image/png" href="/favicon.png" />' in index
    assert 'favicon: "/favicon.png"' in branding


# --- upgrading an install that stored the square favicon -----------------------------

APP_MAIN = types.ModuleType("openkimo_icons_app_main")
APP_MAIN.__path__ = [str(ROOT / "packaging" / "app_main")]
sys.modules[APP_MAIN.__name__] = APP_MAIN
seed_branding = _load(
    "openkimo_icons_app_main.seed_branding", ROOT / "packaging" / "app_main" / "seed_branding.py"
)
build = _load("openkimo_build_macos", ROOT / "packaging" / "build.py")


def _data_url(path: Path) -> str:
    return "data:image/png;base64," + base64.b64encode(path.read_bytes()).decode()


def test_the_macos_build_lists_the_earlier_icons_as_built_ins_to_replace(tmp_path: Path) -> None:
    config = build.parse_args([])
    build.write_brand_json(config, tmp_path)

    data = json.loads((tmp_path / "brand.json").read_text())
    legacy = data["branding_legacy_asset_sha256"]
    assert SQUARE_LOGO_SHA256 in legacy
    assert ROUND_FAVICON_SHA256 in legacy
    assert hashlib.sha256(SOURCE.read_bytes()).hexdigest() == SQUARE_LOGO_SHA256
    assert hashlib.sha256(LOGO.read_bytes()).hexdigest() not in legacy
    assert data["branding_seed"]["favicon"] == _data_url(FAVICON)
    assert data["branding_seed"]["logo"] == _data_url(LOGO)


def _install(tmp_path: Path, stored: dict[str, str]) -> SimpleNamespace:
    config = build.parse_args([])
    build.write_brand_json(config, tmp_path)
    paths = SimpleNamespace(
        brand_json=tmp_path / "brand.json",
        sessions_dir=tmp_path / "sessions",
        env_file=tmp_path / ".env",
    )
    paths.sessions_dir.mkdir()
    connection = seed_branding._connect(paths.sessions_dir / "users.db")
    for key, value in stored.items():
        connection.execute("INSERT INTO branding (key, value) VALUES (?, ?)", (key, value))
    connection.commit()
    connection.close()
    return paths


def _stored(paths: SimpleNamespace) -> dict[str, str]:
    with sqlite3.connect(paths.sessions_dir / "users.db") as connection:
        return dict(connection.execute("SELECT key, value FROM branding"))


def test_an_install_with_the_square_icons_gets_the_rounded_ones(tmp_path: Path) -> None:
    square = _data_url(SOURCE)
    paths = _install(tmp_path, {"favicon": square, "logo": square})

    seed_branding.seed_if_needed(paths)

    stored = _stored(paths)
    assert stored["favicon"] == _data_url(FAVICON)
    assert stored["logo"] == _data_url(LOGO)


def test_a_favicon_set_in_the_admin_panel_is_kept(tmp_path: Path) -> None:
    custom = "data:image/png;base64," + base64.b64encode(b"my own icon").decode()
    paths = _install(tmp_path, {"favicon": custom})

    seed_branding.seed_if_needed(paths)

    assert _stored(paths)["favicon"] == custom


def test_an_up_to_date_asset_is_not_rewritten_on_every_launch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    paths = _install(tmp_path, {"favicon": _data_url(FAVICON), "logo": _data_url(LOGO)})
    seed_branding.seed_if_needed(paths)  # fills the remaining empty fields once
    writes: list[dict[str, str]] = []
    monkeypatch.setattr(seed_branding, "_upsert", lambda _conn, values: writes.append(values))

    seed_branding.seed_if_needed(paths)

    assert writes == []


# --- Windows tray ---------------------------------------------------------------------


def test_a_left_click_on_the_windows_tray_icon_opens_the_menu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    received: list[int] = []

    class _PystrayWin32Icon:  # what pystray.Icon is on Windows, as far as this goes
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def _on_notify(self, wparam: int, lparam: int) -> None:
            received.append(lparam)

    stub = types.ModuleType("pystray")
    stub.Icon = _PystrayWin32Icon  # type: ignore[attr-defined]
    stub.Menu = SimpleNamespace(SEPARATOR=None)  # type: ignore[attr-defined]
    stub.MenuItem = object  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "pystray", stub)
    source = (ROOT / "packaging" / "app_main_win" / "__main__.py").read_text()
    start = source.index("class TrayIcon(")
    end = source.index("class OpenKimoApp")
    namespace: dict[str, object] = {"pystray": stub}
    exec(compile(source[start:end], "app_main_win/__main__.py", "exec"), namespace)  # noqa: S102
    tray = namespace["TrayIcon"]()  # type: ignore[operator]

    tray._on_notify(0, 0x0202)  # WM_LBUTTONUP
    tray._on_notify(0, 0x0205)  # WM_RBUTTONUP

    assert received == [0x0205, 0x0205]  # both show the menu


def test_open_web_ui_is_no_longer_the_tray_default() -> None:
    source = (ROOT / "packaging" / "app_main_win" / "__main__.py").read_text()
    item = source[source.index('"Open Web UI"') : source.index('"Open Admin"')]

    assert "default=True" not in item
