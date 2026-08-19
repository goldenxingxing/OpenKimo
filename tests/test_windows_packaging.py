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


# --- Port conflicts ------------------------------------------------------
#
# A machine that already has a listener on the port used to spin forever: the
# supervisor spawned uvicorn, uvicorn died with WinError 10048, and the health
# probe — which only asked whether *something* answered the port — saw the
# other server's 200 and called the dead child healthy, resetting the restart
# counter. 947 respawns in 90 minutes, with nothing in the UI but "lost
# connection to the session".

def test_health_probe_rejects_a_server_that_is_not_our_child(monkeypatch) -> None:
    monkeypatch.setattr(server, "_probe_health", lambda port: (True, 4242))

    assert server._check_health(5494, expect_pid=4242) is True
    assert server._check_health(5494, expect_pid=1111) is False


def test_health_probe_stays_lenient_with_a_server_that_reports_no_pid(monkeypatch) -> None:
    """An older server predates the pid field; do not call it an impostor."""
    monkeypatch.setattr(server, "_probe_health", lambda port: (True, None))

    assert server._check_health(5494, expect_pid=1111) is True


def test_health_probe_is_false_when_nothing_answers(monkeypatch) -> None:
    monkeypatch.setattr(server, "_probe_health", lambda port: (False, None))

    assert server._check_health(5494, expect_pid=1111) is False
    assert server._port_owner_pid(5494) is None


def test_health_is_probed_on_the_endpoint_that_actually_exists() -> None:
    """`/health` does not exist — the SPA catch-all answers it with index.html
    and a 200, so probing it declared every port healthy, including one held
    by a stranger. That is what let a dead child be marked RUNNING."""
    assert server._HEALTH_PATH == "/healthz"


def test_an_html_page_is_not_a_health_check(monkeypatch) -> None:
    import io

    class _Resp(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(
        server, "_port_is_listening", lambda port: True, raising=False
    )
    import urllib.request

    monkeypatch.setattr(
        urllib.request, "urlopen", lambda *a, **k: _Resp(b"<!doctype html><html>")
    )

    assert server._probe_health(5494) == (False, None)


def test_a_json_health_body_reports_its_pid(monkeypatch) -> None:
    import io

    class _Resp(io.BytesIO):
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

    import urllib.request

    monkeypatch.setattr(
        urllib.request,
        "urlopen",
        lambda *a, **k: _Resp(b'{"status":"ok","pid":4242}'),
    )

    assert server._probe_health(5494) == (True, 4242)


def test_uvicorn_log_config_is_swapped_in_atomically(tmp_path: Path) -> None:
    """Two instances writing this file concurrently used to leave a reader
    parsing a truncated one — uvicorn then died on its own log config."""
    target = tmp_path / "uvicorn-log.json"

    server._write_atomic(target, json.dumps({"version": 1}))

    assert json.loads(target.read_text(encoding="utf-8")) == {"version": 1}
    assert [p.name for p in tmp_path.iterdir()] == ["uvicorn-log.json"]


# --- Orphan listener reaping (Windows) -----------------------------------
#
# The POSIX reaper shells out to lsof and ps. Neither exists on Windows, so
# the whole function was a no-op there and a uvicorn left behind by a
# force-killed tray app kept the port indefinitely.

_NETSTAT = """
Active Connections

  Proto  Local Address          Foreign Address        State           PID
  TCP    127.0.0.1:5494         0.0.0.0:0              LISTENING       4242
  TCP    127.0.0.1:54941        127.0.0.1:5494         ESTABLISHED     9001
  TCP    0.0.0.0:135            0.0.0.0:0              LISTENING       1024
  TCP    [::1]:5494             [::]:0                 LISTENING       4242
"""


def _fake_windows_calls(monkeypatch, cmdline: str, killed: list[int]):
    def fake_run(cmd, **kwargs):
        if cmd[0] == "netstat":
            return SimpleNamespace(stdout=_NETSTAT, stderr="", returncode=0)
        if cmd[0] == "powershell":
            return SimpleNamespace(stdout=cmdline, stderr="", returncode=0)
        if cmd[0] == "taskkill":
            killed.append(int(cmd[2]))
            return SimpleNamespace(stdout="", stderr="", returncode=0)
        raise AssertionError(f"unexpected command {cmd!r}")

    monkeypatch.setattr(server.subprocess, "run", fake_run)
    monkeypatch.setattr(server, "_port_owner_pid", lambda port: None)


def test_windows_reaper_kills_our_own_leftover_listener(monkeypatch) -> None:
    killed: list[int] = []
    _fake_windows_calls(
        monkeypatch,
        r"C:\Programs\OpenKimo\runtime\python\python.exe -m uvicorn kimi_cli.web.app:create_app",
        killed,
    )

    server._reap_orphan_listener_windows(5494)

    # Both the IPv4 and IPv6 rows name the same pid; it must be killed once.
    assert killed == [4242]


def test_windows_reaper_leaves_a_foreign_listener_alone(monkeypatch) -> None:
    """Never kill by port alone — something else may legitimately own it."""
    killed: list[int] = []
    _fake_windows_calls(monkeypatch, r"C:\Windows\System32\inetsrv\w3wp.exe", killed)

    server._reap_orphan_listener_windows(5494)

    assert killed == []


def test_windows_reaper_survives_netstat_being_unavailable(monkeypatch) -> None:
    def boom(cmd, **kwargs):
        raise FileNotFoundError(cmd[0])

    monkeypatch.setattr(server.subprocess, "run", boom)

    server._reap_orphan_listener_windows(5494)  # must not raise


def test_watcher_does_not_overwrite_a_crash_diagnosis(tmp_path: Path) -> None:
    """CRASHED carries the reason the user is meant to act on.

    The watcher sees "no child process" for a clean stop and for a refusal to
    start alike; only the latter has something to say.
    """
    import threading
    import time as _time

    paths = SimpleNamespace(
        app_support=tmp_path, logs=tmp_path, server_log=tmp_path / "server.log"
    )
    sup = server.UvicornSupervisor(paths)  # type: ignore[arg-type]
    states: list[server.ServerState] = []
    sup.on_state_change = lambda state, info: states.append(state)
    sup._state = server.ServerState.CRASHED
    sup._proc = None

    watcher = threading.Thread(target=sup._watch_loop, daemon=True)
    watcher.start()
    _time.sleep(0.6)
    sup._stop_supervisor.set()
    watcher.join(timeout=3)

    assert sup.state is server.ServerState.CRASHED
    assert states == []


# --- Single instance -----------------------------------------------------

APP_MAIN_WIN_DIR = ROOT / "packaging" / "app_main_win"
SINGLE_SPEC = importlib.util.spec_from_file_location(
    "openkimo_app_main_win.single_instance", APP_MAIN_WIN_DIR / "single_instance.py"
)
single_instance = importlib.util.module_from_spec(SINGLE_SPEC)
sys.modules[SINGLE_SPEC.name] = single_instance
SINGLE_SPEC.loader.exec_module(single_instance)


def test_single_instance_guard_is_inert_off_windows(monkeypatch) -> None:
    monkeypatch.setattr(single_instance.sys, "platform", "darwin")

    assert single_instance.acquire("openkimo") is True


def test_single_instance_guard_starts_anyway_when_the_mutex_cannot_be_made(
    monkeypatch,
) -> None:
    """A machine that refuses the mutex is better served by launching.

    Refusing to start would turn a hardening measure into an outage.
    """
    monkeypatch.setattr(single_instance.sys, "platform", "win32")

    def boom(name):
        raise OSError("no ctypes here")

    monkeypatch.setitem(sys.modules, "ctypes", None)
    assert single_instance.acquire("openkimo") is True


class TestPackagingNameCollision:
    """This directory is named ``packaging``, same as the PyPI distribution.

    Whichever comes first on the path wins, and both layouts put this one
    first: the Windows launcher sets ``PYTHONPATH=runtime;runtime/site-packages``
    so ``runtime/packaging/`` shadows it, and in dev the repo root is the
    working directory so this file shadows it there. The shim grafts the real
    distribution onto ``__path__`` to keep both importable.

    Only the bundled half of that was covered before, and the dev half did not
    work: running anything from the repo root failed on ``import fastmcp``,
    because fastmcp imports ``packaging.version``.
    """

    def test_the_real_distribution_is_reachable_from_the_repo_root(self) -> None:
        import subprocess
        import sys
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [sys.executable, "-c", "import packaging.version; print(packaging.version.parse('1.0'))"],
            cwd=root,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, (
            f"the repo's own packaging/ is shadowing the PyPI one: {result.stderr}"
        )
        assert "1.0" in result.stdout

    def test_our_own_submodules_still_win(self) -> None:
        """Grafting must add to the search path, never take it over."""
        import subprocess
        import sys
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [sys.executable, "-c", "import packaging.app_main.server as s; print(s.__file__)"],
            cwd=root,
            capture_output=True,
            text=True,
        )

        assert result.returncode == 0, result.stderr
        assert str(root / "packaging") in result.stdout


class TestOnlyOneKimiCli:
    """The bundle must ship one copy of kimi_cli, not two.

    The framework layer installs the kimi-cli wheel for its dependency tree and
    receives the package's own code with it. That copy is stale and stays
    stale: the wheel's version never changes, so the resolver treats
    kimi-cli==1.49.0 as already satisfied however much the contents moved. The
    copy in the last build was months behind — seven modules under `memory/`
    against the wheel's thirteen — including none of the retrieval or
    consolidation code.

    Nothing broke only because the launcher puts Resources/ first on
    PYTHONPATH. That is one ordering change, or one invocation of the bundled
    Python without the launcher, away from the app silently running an old
    memory system.
    """

    def test_the_build_removes_the_layered_copy(self) -> None:
        import inspect
        import sys
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from packaging import build

        source = inspect.getsource(build.copy_runtimes)

        assert "_drop_layered_kimi_cli" in source, (
            "layers must be stripped before postinstall runs over them"
        )

    def test_it_removes_the_package_and_its_metadata_but_not_the_dependencies(self) -> None:
        import sys
        import tempfile
        from pathlib import Path

        sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
        from packaging import build

        runtimes = Path(tempfile.mkdtemp())
        packages = runtimes / "framework-x" / "lib" / "python3.12" / "site-packages"
        packages.mkdir(parents=True)
        (packages / "kimi_cli").mkdir()
        (packages / "kimi_cli" / "__init__.py").write_text("", encoding="utf-8")
        (packages / "kimi_cli-1.49.0.dist-info").mkdir()
        (packages / "fastapi").mkdir()

        build._drop_layered_kimi_cli(runtimes)

        assert not (packages / "kimi_cli").exists()
        assert not (packages / "kimi_cli-1.49.0.dist-info").exists()
        assert (packages / "fastapi").exists(), "dependencies are why the wheel is installed"
