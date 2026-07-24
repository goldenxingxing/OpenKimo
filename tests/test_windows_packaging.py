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

APP_MAIN_DIR = ROOT / "packaging" / "app_main"
APP_MAIN_PACKAGE = types.ModuleType("openkimo_app_main")
APP_MAIN_PACKAGE.__path__ = [str(APP_MAIN_DIR)]
sys.modules[APP_MAIN_PACKAGE.__name__] = APP_MAIN_PACKAGE
SEED_SPEC = importlib.util.spec_from_file_location(
    "openkimo_app_main.seed_branding", APP_MAIN_DIR / "seed_branding.py"
)
seed_branding = importlib.util.module_from_spec(SEED_SPEC)
sys.modules[SEED_SPEC.name] = seed_branding
SEED_SPEC.loader.exec_module(seed_branding)


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


def _data_url(payload: bytes) -> str:
    return "data:image/png;base64," + base64.b64encode(payload).decode()


def _branding_paths(tmp_path: Path, *, legacy_hash: str) -> SimpleNamespace:
    brand_json = tmp_path / "brand.json"
    brand_json.write_text(
        json.dumps(
            {
                "branding_seed": {
                    "brand_name": "OpenKimo",
                    "logo": _data_url(b"canonical"),
                    "favicon": _data_url(b"canonical"),
                },
                "branding_legacy_asset_sha256": [legacy_hash],
            }
        )
    )
    env_file = tmp_path / ".env"
    env_file.write_text("")
    return SimpleNamespace(
        brand_json=brand_json,
        env_file=env_file,
        sessions_dir=tmp_path / "sessions",
    )


def _read_branding(paths: SimpleNamespace) -> dict[str, str]:
    with sqlite3.connect(paths.sessions_dir / "users.db") as connection:
        return dict(connection.execute("SELECT key, value FROM branding"))


def test_branding_seed_migrates_legacy_asset_and_seeds_empty_fields(
    tmp_path: Path,
) -> None:
    legacy_bytes = b"legacy built-in icon"
    paths = _branding_paths(
        tmp_path, legacy_hash=hashlib.sha256(legacy_bytes).hexdigest()
    )
    database = paths.sessions_dir / "users.db"
    connection = seed_branding._connect(database)
    connection.execute(
        "INSERT INTO branding (key, value) VALUES (?, ?)",
        ("logo", _data_url(legacy_bytes)),
    )
    connection.commit()
    connection.close()

    seed_branding.seed_if_needed(paths)

    branding = _read_branding(paths)
    assert branding["logo"] == _data_url(b"canonical")
    assert branding["favicon"] == _data_url(b"canonical")
    assert branding["brand_name"] == "OpenKimo"


def test_branding_seed_preserves_custom_asset(tmp_path: Path) -> None:
    paths = _branding_paths(
        tmp_path, legacy_hash=hashlib.sha256(b"legacy").hexdigest()
    )
    custom = _data_url(b"user custom icon")
    database = paths.sessions_dir / "users.db"
    connection = seed_branding._connect(database)
    connection.execute(
        "INSERT INTO branding (key, value) VALUES (?, ?)",
        ("favicon", custom),
    )
    connection.commit()
    connection.close()

    seed_branding.seed_if_needed(paths)

    assert _read_branding(paths)["favicon"] == custom
