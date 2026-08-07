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
SERVER_SPEC = importlib.util.spec_from_file_location(
    "openkimo_app_main.server", APP_MAIN_DIR / "server.py"
)
server = importlib.util.module_from_spec(SERVER_SPEC)
sys.modules[SERVER_SPEC.name] = server
SERVER_SPEC.loader.exec_module(server)
DOTENV_SPEC = importlib.util.spec_from_file_location(
    "openkimo_app_main.dotenv_io", APP_MAIN_DIR / "dotenv_io.py"
)
dotenv_io = importlib.util.module_from_spec(DOTENV_SPEC)
sys.modules[DOTENV_SPEC.name] = dotenv_io
DOTENV_SPEC.loader.exec_module(dotenv_io)


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


def test_windows_server_process_uses_no_window_and_new_group() -> None:
    options = server._popen_new_group_kwargs("win32")

    assert options["creationflags"] & 0x00000200
    assert options["creationflags"] & 0x08000000


def test_posix_server_process_starts_a_new_session() -> None:
    assert server._popen_new_group_kwargs("darwin") == {
        "start_new_session": True
    }


# --- Text encoding -------------------------------------------------------
#
# On a Chinese-locale Windows Python's default text encoding is cp936, which
# cannot represent characters a user may well paste into a provider config.
# Saving one used to fail with "'gbk' codec can't encode character '\xb3'"
# and the Settings window reported the codec error instead of the setting.

# '³' (U+00B3): representable in UTF-8, absent from GBK.
_NOT_IN_GBK = "OpenKimo³"


def test_env_round_trips_a_character_the_host_codepage_lacks(tmp_path: Path) -> None:
    env_file = tmp_path / ".env"

    dotenv_io.write_env(env_file, {"LLM_PROVIDERS": _NOT_IN_GBK})

    assert env_file.read_bytes().decode("utf-8")
    assert dotenv_io.read_env(env_file)["LLM_PROVIDERS"] == _NOT_IN_GBK


def test_env_written_in_the_host_codepage_still_reads(
    tmp_path: Path, monkeypatch
) -> None:
    # A file left behind by the version that wrote the ANSI codepage. It has to
    # keep loading, or the app comes up with no provider configured at all.
    # "mbcs" only exists on Windows, so stand in the concrete codepage here.
    monkeypatch.setattr(dotenv_io, "LEGACY_ENCODING", "gbk")
    env_file = tmp_path / ".env"
    env_file.write_bytes("LLM_DEFAULT_PROVIDER=深度求索\n".encode("gbk"))

    assert dotenv_io.read_env(env_file)["LLM_DEFAULT_PROVIDER"] == "深度求索"


def test_saving_normalises_a_legacy_env_to_utf8(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setattr(dotenv_io, "LEGACY_ENCODING", "gbk")
    env_file = tmp_path / ".env"
    env_file.write_bytes("LLM_DEFAULT_PROVIDER=深度求索\n".encode("gbk"))

    dotenv_io.write_env(env_file, {"KIMI_WEB_PORT": "5494"})

    assert env_file.read_bytes().decode("utf-8").count("深度求索") == 1


def _fake_paths(tmp_path: Path) -> SimpleNamespace:
    return SimpleNamespace(
        app_name="OpenKimo",
        static_dir=tmp_path / "static",
        work_dir=tmp_path / "work",
        sessions_dir=tmp_path / "sessions",
        output_dir=tmp_path / "output",
        skill_dir=tmp_path / "skills",
        app_support=tmp_path,
        wiki_dir=tmp_path / "wiki",
        kimi_cli=tmp_path / "kimi_cli",
        env_file=tmp_path / ".env",
        userbase=tmp_path / "userbase",
        userbase_bin=tmp_path / "userbase" / "bin",
        pip_conf=tmp_path / "pip.conf",
        bundled_python_bin=tmp_path / "python" / "bin",
    )


def test_worker_writes_its_own_streams_as_utf8(tmp_path: Path) -> None:
    # The parent reads the worker's stderr with .decode("utf-8"), and a model
    # response containing a character the ANSI codepage lacks would otherwise
    # raise UnicodeEncodeError far from anything the user did.
    env = server._build_env(_fake_paths(tmp_path))

    assert env["PYTHONIOENCODING"] == "utf-8"


def test_worker_does_not_run_in_utf8_mode(tmp_path: Path) -> None:
    """PYTHONUTF8 must stay unset. Regression guard for v0.1.29.

    Unlike PYTHONIOENCODING, PYTHONUTF8=1 also switches
    ``locale.getpreferredencoding()``, which is what ``subprocess(text=True)``
    decodes child output with. The Windows tools we shell out to legitimately
    emit the ANSI codepage: on a Chinese Windows, ``where.exe git`` prints GBK,
    the decode died inside subprocess's reader thread, git-bash detection came
    back empty, and the worker exited 1 with GitBashNotFoundError before the
    user could type anything.
    """
    env = server._build_env(_fake_paths(tmp_path))

    assert "PYTHONUTF8" not in env


def test_launcher_makes_the_same_distinction() -> None:
    # The comment there names PYTHONUTF8 to explain why it is absent, so match
    # the assignment rather than the word.
    assert 'env["PYTHONIOENCODING"] = "utf-8"' in build_windows._LAUNCHER_PY
    assert 'env["PYTHONUTF8"]' not in build_windows._LAUNCHER_PY
