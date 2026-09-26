from __future__ import annotations

import base64
import hashlib
import importlib.util
import json
import re
import sqlite3
import sys
import types
from pathlib import Path

import pytest
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parents[1]
CANONICAL_ICON = ROOT / "kimi-cli" / "web" / "public" / "logo.png"
ROUND_FAVICON = ROOT / "kimi-cli" / "web" / "public" / "favicon.png"
LEGACY_ICON_SHA256 = (
    "dbd00e2ad61ea8832ef0b024662a4a8a5d1b66f0599d5d42e1c9688b9d4cfdf6"
)
SQUARE_LOGO_SHA256 = "a30b84efe092999d20b4b7f78b1b99c8a2d1e1da72e3fa09f853bf68b616f0af"
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
    assert data["branding_legacy_asset_sha256"] == [LEGACY_ICON_SHA256, SQUARE_LOGO_SHA256]


def test_windows_brand_paths_use_the_canonical_icon() -> None:
    config = build_windows.parse_args([])

    assert config.logo == CANONICAL_ICON
    assert config.favicon == ROUND_FAVICON


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
        bundled_python_root=tmp_path / "runtime" / "python",
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

    def test_it_removes_the_package_but_keeps_the_metadata(self) -> None:
        """The version lives in the metadata, and something asks for it.

        Removing the dist-info too imported cleanly from Resources/ and then
        crashed the server on startup: `kimi_cli.constant` reads
        `importlib.metadata.version("kimi-cli")` to build the User-Agent, which
        consults the dist-info rather than the package.
        """
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
        assert (packages / "kimi_cli-1.49.0.dist-info").exists(), (
            "the version is read from here at import time"
        )
        assert (packages / "fastapi").exists(), "dependencies are why the wheel is installed"


class TestWorkspaceMembersReachTheInstaller:
    """carryover has to be installed, not inherited.

    It used to be a vendored copy inside `src/kimi_cli/`, so
    copy_kimi_cli_source() carried it into the runtime for free. As a workspace
    member it lives in `kimi-cli/packages/carryover/` and is published nowhere,
    so a pip resolve that sees only the kimi-cli wheel goes looking for it on
    PyPI, gets a 404, and installs nothing — and the build only warned. The
    shipped installer launched to `ModuleNotFoundError: No module named
    'carryover'` on the first import of `kimi_cli.utils.string`.
    """

    def test_every_workspace_member_is_built(self) -> None:
        """The list comes from the file that declares it, not from a copy here.

        Deliberately not an ordering assertion. The workspace order is not a
        dependency order — kimi-code depends on kimi-cli and is declared before
        it — and pinning one here would enshrine a property the build does not
        have and does not need: every wheel goes into a single pip resolve.
        """
        members = [d.name for d in build_windows.workspace_dirs()]

        assert "carryover" in members, "the member that PyPI cannot supply"
        assert "kimi-cli" in members
        assert build_windows.workspace_modules().count("carryover") == 1

    def test_the_wheels_all_go_into_one_pip_resolve(self, monkeypatch, tmp_path) -> None:
        """Separate commands cannot satisfy a local pin from a local wheel.

        pip only resolves `carryover` from the workspace when that wheel is a
        direct requirement of the same command as the kimi-cli wheel that
        depends on it. Split across two invocations, the first goes to PyPI and
        404s — the bug this whole path exists to prevent.
        """
        commands: list[list[str]] = []

        def record(cmd: list[str], **kwargs: object) -> int:
            commands.append(list(cmd))
            return 0

        monkeypatch.setattr(build_windows, "is_windows", lambda: True)
        monkeypatch.setattr(build_windows, "run_ok", record)
        monkeypatch.setattr(build_windows, "verify_workspace_members", lambda sp: None)
        wheels = [
            tmp_path / "carryover-0.1.0-py3-none-any.whl",
            tmp_path / "kimi_cli-1.49.0-py3-none-any.whl",
        ]
        requirements = tmp_path / "requirements-windows.txt"
        requirements.write_text("", encoding="utf-8")

        build_windows.install_site_packages(
            python_exe=tmp_path / "python.exe",
            site_packages=tmp_path / "site-packages",
            requirements_file=requirements,
            local_wheels=wheels,
            pip_log=tmp_path / "pip.log",
        )

        carrying = [c for c in commands if any(str(w) in c for w in wheels)]
        assert len(carrying) == 1, "one resolve, or the local pins cannot be met"
        assert all(str(w) in carrying[0] for w in wheels)
        assert str(requirements) in carrying[0], (
            "the Windows deps share the resolve: a second --upgrade install "
            "into the same --target lets Pillow>=10.0 overwrite kimi-cli's "
            "pinned pillow==12.2.0"
        )
        assert len(commands) == 1, "one pip invocation, so nothing resolves twice"

    def test_a_missing_member_fails_the_windows_build(self, monkeypatch, tmp_path) -> None:
        site_packages = tmp_path / "site-packages"
        site_packages.mkdir()
        for module in build_windows.workspace_modules():
            if module != "carryover":
                (site_packages / module).mkdir()
        monkeypatch.setattr(build_windows, "is_windows", lambda: True)

        with pytest.raises(RuntimeError, match="carryover"):
            build_windows.verify_workspace_members(site_packages)

    def test_a_complete_install_passes(self, monkeypatch, tmp_path) -> None:
        site_packages = tmp_path / "site-packages"
        for module in build_windows.workspace_modules():
            (site_packages / module).mkdir(parents=True)
        monkeypatch.setattr(build_windows, "is_windows", lambda: True)

        build_windows.verify_workspace_members(site_packages)

    def test_a_missing_submodule_says_so(self, monkeypatch, tmp_path) -> None:
        """The hint has to be reachable, which means reading the list lazily.

        As a module-level constant this read ran at import, so a clone without
        submodules raised a bare FileNotFoundError before any of the build's
        own error handling — and took this test module down at collection.
        """
        monkeypatch.setattr(build_windows, "KIMI_CLI", tmp_path / "kimi-cli")

        with pytest.raises(RuntimeError, match="git submodule update"):
            build_windows.workspace_dirs()

    def test_missing_win_deps_fail_the_windows_build(self, monkeypatch, tmp_path) -> None:
        """requirements-windows.txt carries what the app needs to even start.

        verify_workspace_members() cannot cover for it: it inspects workspace
        members only, so losing pywebview still leaves it reporting "all
        present", the installer builds green, and the app dies on launch with
        ModuleNotFoundError: No module named 'webview'.
        """
        monkeypatch.setattr(build_windows, "is_windows", lambda: True)
        monkeypatch.setattr(build_windows, "run_ok", lambda cmd, **kw: 0)

        with pytest.raises(RuntimeError, match="pywebview"):
            build_windows.install_site_packages(
                python_exe=tmp_path / "python.exe",
                site_packages=tmp_path / "site-packages",
                requirements_file=tmp_path / "requirements-windows.txt",
                local_wheels=[tmp_path / "carryover-0.1.0-py3-none-any.whl"],
                pip_log=tmp_path / "pip.log",
            )

    def test_a_glob_member_is_not_blamed_on_submodules(self, monkeypatch, tmp_path) -> None:
        """uv allows `members = ["packages/*"]`; this build reads them literally.

        Left alone, the glob becomes a path that never exists and the build
        tells the developer to re-init submodules that are already there.
        """
        kimi = tmp_path / "kimi-cli"
        kimi.mkdir()
        (kimi / "pyproject.toml").write_text(
            '[tool.uv.workspace]\nmembers = ["packages/*"]\n', encoding="utf-8"
        )
        monkeypatch.setattr(build_windows, "KIMI_CLI", kimi)

        with pytest.raises(RuntimeError, match="glob workspace members"):
            build_windows.workspace_dirs()

    def test_a_failed_resolve_fails_the_windows_build(self, monkeypatch, tmp_path) -> None:
        """pip's own error must surface, not six members reported missing.

        The wheels pin each other, so one stale pin makes the resolve
        unsatisfiable and installs nothing. Carrying on from there reaches
        verify_workspace_members(), whose message describes the symptom and
        loses the conflict pip actually printed.
        """
        monkeypatch.setattr(build_windows, "is_windows", lambda: True)
        monkeypatch.setattr(build_windows, "run_ok", lambda cmd, **kw: 1)
        pip_log = tmp_path / "pip.log"
        # Present so the assertion can only be about the failed resolve, not
        # about which input install_site_packages happens to validate first.
        requirements = tmp_path / "requirements-windows.txt"
        requirements.write_text("", encoding="utf-8")

        with pytest.raises(RuntimeError, match=re.escape(str(pip_log))):
            build_windows.install_site_packages(
                python_exe=tmp_path / "python.exe",
                site_packages=tmp_path / "site-packages",
                requirements_file=requirements,
                local_wheels=[tmp_path / "carryover-0.1.0-py3-none-any.whl"],
                pip_log=pip_log,
            )


class TestBundledInterpreterIsPinned:
    """The interpreter inside every installer must be verified, like busybox.

    release.yml caches packaging/.cache, and a GitHub Actions cache is writable
    by a workflow run on any branch of the repo. Before this, a substituted
    archive was taken from the cache, had its digest printed and compared to
    nothing, and was unpacked with an unfiltered extractall.
    """

    def test_the_archive_has_a_pinned_digest(self) -> None:
        assert len(build_windows.PY_STANDALONE_SHA256) == 64
        assert set(build_windows.PY_STANDALONE_SHA256) <= set("0123456789abcdef")

    def test_a_mismatched_archive_is_rejected_and_evicted(self, monkeypatch, tmp_path) -> None:
        """A bad archive must fail the build and not survive in the cache.

        Leaving it behind would make the next run take the `tar_path.exists()`
        branch and fail on the same bytes forever.
        """
        cache = tmp_path / "cache"
        cache.mkdir()
        tar_name = (
            f"cpython-{build_windows.PY_STANDALONE_VERSION}"
            f"+{build_windows.PY_STANDALONE_TAG}"
            f"-{build_windows.PY_STANDALONE_TARGET}.tar.gz"
        )
        impostor = cache / tar_name
        impostor.write_bytes(b"not the interpreter you pinned")

        with pytest.raises(RuntimeError, match="sha256 mismatch"):
            build_windows.fetch_python_runtime(cache, tmp_path / "runtime")

        assert not impostor.exists(), "a rejected archive must not stay cached"

    def test_extraction_refuses_a_traversing_member(self, monkeypatch, tmp_path) -> None:
        """filter='data' is what stops a crafted archive writing outside dest."""
        import tarfile

        cache = tmp_path / "cache"
        cache.mkdir()
        tar_name = (
            f"cpython-{build_windows.PY_STANDALONE_VERSION}"
            f"+{build_windows.PY_STANDALONE_TAG}"
            f"-{build_windows.PY_STANDALONE_TARGET}.tar.gz"
        )
        payload = tmp_path / "payload"
        payload.write_bytes(b"pwned")
        with tarfile.open(cache / tar_name, "w:gz") as tar:
            tar.add(payload, arcname="../escaped.txt")
        # Skip the digest gate; this test is about the extraction step.
        monkeypatch.setattr(
            build_windows, "_sha256", lambda p: build_windows.PY_STANDALONE_SHA256
        )

        with pytest.raises(tarfile.OutsideDestinationError):
            build_windows.fetch_python_runtime(cache, tmp_path / "runtime")

        assert not (tmp_path / "escaped.txt").exists()


class TestAFailedBuildExitsNonZero:
    """A build that produced no installer must not report success.

    build_launcher_exe() and run_inno_setup() return None on failure — missing
    pyinstaller, missing or failing iscc — and both were only warned about, so
    `python build_windows.py` exited 0 with nothing in dist-win/. The release
    workflow only caught it later, in an ad-hoc Get-ChildItem check.
    """

    def test_a_missing_installer_is_a_failure_on_windows(self) -> None:
        import inspect

        source = inspect.getsource(build_windows.main)
        tail = source[source.index("print_summary(cfg, staging"):]

        assert "return 1" in tail, "main must fail when no installer was produced"
        assert "installer_exe is None" in tail
        assert "launcher_exe is None" in tail

    def test_the_check_is_scoped_to_windows(self) -> None:
        """Off Windows those steps are skipped by design; the tree is the output."""
        import inspect

        source = inspect.getsource(build_windows.main)
        tail = source[source.index("print_summary(cfg, staging"):]

        assert "if is_windows():" in tail
