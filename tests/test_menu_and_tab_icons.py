"""The menu-bar icon, the browser-tab icon and the Windows tray click.

The logo is a full square. Shrunk as it was, it made a coloured square in the macOS
menu bar, where icons are monochrome shapes that follow a light or dark bar, and a
hard square in the browser tab. And on Windows a left click on the tray icon ran the
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
LOGO = ROOT / "kimi-cli" / "web" / "public" / "logo.png"
FAVICON = ROOT / "kimi-cli" / "web" / "public" / "favicon.png"
MENUBAR = ROOT / "packaging" / "MenuBarIconTemplate.png"


def _load(name: str, path: Path) -> types.ModuleType:
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_the_favicon_is_round() -> None:
    image = Image.open(FAVICON).convert("RGBA")
    width, height = image.size

    assert width == height
    for corner in ((0, 0), (width - 1, 0), (0, height - 1), (width - 1, height - 1)):
        assert image.getpixel(corner)[3] == 0
    assert image.getpixel((width // 2, height // 5))[3] == 255  # inside the disc


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
    monkeypatch.setattr(derive, "FAVICON", tmp_path / "favicon.png")
    monkeypatch.setattr(derive, "MENUBAR", tmp_path / "MenuBarIconTemplate.png")
    monkeypatch.setattr(derive, "ROOT", tmp_path)

    derive.main()

    for made, committed in ((tmp_path / "favicon.png", FAVICON), (tmp_path / "MenuBarIconTemplate.png", MENUBAR)):
        assert list(Image.open(made).getdata()) == list(Image.open(committed).getdata())


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


def test_the_macos_build_lists_the_square_logo_as_a_built_in_to_replace(tmp_path: Path) -> None:
    config = build.parse_args([])
    build.write_brand_json(config, tmp_path)

    data = json.loads((tmp_path / "brand.json").read_text())
    assert hashlib.sha256(LOGO.read_bytes()).hexdigest() in data["branding_legacy_asset_sha256"]
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


def test_an_install_with_the_square_favicon_gets_the_round_one(tmp_path: Path) -> None:
    square = _data_url(LOGO)
    paths = _install(tmp_path, {"favicon": square, "logo": square})

    seed_branding.seed_if_needed(paths)

    stored = _stored(paths)
    assert stored["favicon"] == _data_url(FAVICON)
    assert stored["logo"] == square  # the logo is unchanged


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
