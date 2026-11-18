from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, cast
from uuid import UUID, uuid4

import pytest
from kaos.path import KaosPath

from kimo_cli.session import Session
from kimo_cli.session_state import load_session_state, save_session_state
from kimo_cli.web.api import sessions as sessions_api
from kimo_cli.web.models import GenerateTitleRequest

if TYPE_CHECKING:
    from kimo_cli.web.runner.process import KimoCLIRunner


def _anonymous_request() -> SimpleNamespace:
    """A stand-in for the caller's connection.

    The per-session routes now resolve who is asking, so they can refuse a
    session that belongs to somebody else. These sessions have no owner, which
    is the single-user case: the check returns before it looks at the
    connection at all.
    """
    return SimpleNamespace()


@pytest.fixture
def isolated_share_dir(monkeypatch, tmp_path: Path) -> Path:
    share_dir = tmp_path / "share"
    share_dir.mkdir()

    def _get_share_dir() -> Path:
        share_dir.mkdir(parents=True, exist_ok=True)
        return share_dir

    monkeypatch.setattr("kimo_cli.share.get_share_dir", _get_share_dir)
    monkeypatch.setattr("kimo_cli.metadata.get_share_dir", _get_share_dir)
    return share_dir


@pytest.fixture
def work_dir(tmp_path: Path) -> KaosPath:
    path = tmp_path / "work"
    path.mkdir()
    return KaosPath.unsafe_from_local_path(path)


class _FakeOAuthManager:
    def __init__(self, _config: object) -> None:
        pass

    async def ensure_fresh(self) -> None:
        return None


class _FakeRunner:
    """Stand-in for ``KimoCLIRunner`` for tests that bypass FastAPI dependency injection."""

    def get_session(self, _session_id: UUID) -> None:
        return None


class _FakeLLM:
    chat_provider = object()


class _FakeMessage:
    def __init__(self, text: str) -> None:
        self._text = text

    def extract_text(self) -> str:
        return self._text


class _FakeResult:
    def __init__(self, text: str) -> None:
        self.message = _FakeMessage(text)


@pytest.mark.anyio
async def test_startup_dir_prefers_last_selected_work_directory(
    isolated_share_dir: Path, work_dir: KaosPath
) -> None:
    from kimo_cli.metadata import Metadata, save_metadata

    save_metadata(Metadata(last_work_dir=str(work_dir)))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(startup_dir="/fallback")))

    assert await sessions_api.get_startup_dir(request) == str(work_dir)


def test_web_store_loads_session_from_legacy_root(
    isolated_share_dir: Path, work_dir: KaosPath
) -> None:
    from kimo_cli.metadata import Metadata, WorkDirMeta, save_metadata
    from kimo_cli.web.store.sessions import invalidate_sessions_cache, load_session_by_id

    session_id = uuid4()
    work_meta = WorkDirMeta(path=str(work_dir))
    session_dir = work_meta.legacy_sessions_dir / str(session_id)
    session_dir.mkdir(parents=True)
    (session_dir / "context.jsonl").write_text(
        '{"role":"user","content":"legacy"}\n', encoding="utf-8"
    )
    save_metadata(Metadata(work_dirs=[work_meta]))
    invalidate_sessions_cache()

    loaded = load_session_by_id(session_id)

    assert loaded is not None
    assert loaded.kimo_cli_session.dir == session_dir


@pytest.mark.anyio
async def test_generate_title_preserves_concurrent_manual_title(
    isolated_share_dir: Path,
    work_dir: KaosPath,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session = await Session.create(work_dir)

    config = SimpleNamespace(
        default_model="test-model",
        models={"test-model": SimpleNamespace(provider="test-provider")},
        providers={"test-provider": object()},
    )

    monkeypatch.setattr("kimo_cli.config.load_config", lambda: config)
    monkeypatch.setattr(
        "kimo_cli.llm.create_llm",
        lambda provider_config, model_config, oauth=None: _FakeLLM(),
    )
    monkeypatch.setattr("kimo_cli.auth.oauth.OAuthManager", _FakeOAuthManager)

    async def fake_generate(*, chat_provider, system_prompt, tools, history):
        state = load_session_state(session.dir)
        state.custom_title = "Manual Title"
        state.title_generated = True
        save_session_state(state, session.dir)
        return _FakeResult("AI Title")

    monkeypatch.setattr("kosong.generate", fake_generate)

    response = await sessions_api.generate_session_title(
        UUID(session.id),
        _anonymous_request(),
        GenerateTitleRequest(
            user_message="debug the flaky web session rename issue",
            assistant_response="I'll inspect the session state writes.",
        ),
        runner=cast("KimoCLIRunner", _FakeRunner()),
    )

    state = load_session_state(session.dir)
    assert response.title == "Manual Title"
    assert state.custom_title == "Manual Title"
    assert state.title_generated is True
    assert state.title_generate_attempts == 0


@pytest.mark.anyio
async def test_generate_title_is_built_from_what_the_person_wrote(
    isolated_share_dir: Path,
    work_dir: KaosPath,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Seen in a real session: the title was the raw ``<pasted_text>`` markup."""
    session = await Session.create(work_dir)
    config = SimpleNamespace(
        default_model="test-model",
        models={"test-model": SimpleNamespace(provider="test-provider")},
        providers={"test-provider": object()},
    )
    monkeypatch.setattr("kimo_cli.config.load_config", lambda: config)
    monkeypatch.setattr(
        "kimo_cli.llm.create_llm",
        lambda provider_config, model_config, oauth=None: _FakeLLM(),
    )
    monkeypatch.setattr("kimo_cli.auth.oauth.OAuthManager", _FakeOAuthManager)
    prompts: list[str] = []

    async def failing_generate(*, chat_provider, system_prompt, tools, history):
        del chat_provider, system_prompt, tools
        prompts.append(history[0].extract_text())
        raise RuntimeError("rate limited")

    monkeypatch.setattr("kosong.generate", failing_generate)

    response = await sessions_api.generate_session_title(
        UUID(session.id),
        _anonymous_request(),
        GenerateTitleRequest(
            user_message=(
                '帮我看看这段订单数据有没有异常：<pasted_text index="1">'
                "order_id,date,city\r\n100036,2026-01-02,上海</pasted_text>，重点看金额。"
            ),
            assistant_response="我先读一下数据。",
        ),
        runner=cast("KimoCLIRunner", _FakeRunner()),
    )

    assert response.title == "帮我看看这段订单数据有没有异常：⟦…⟧，重点看金额。"
    assert "order_id" not in prompts[0]
    assert "<pasted_text" not in prompts[0]


def test_first_turn_stopped_before_any_reply_still_has_its_question(tmp_path: Path) -> None:
    """A stopped first turn gave no title, so a later "继续" became it."""
    import asyncio

    from kosong.message import TextPart

    from kimo_cli.wire.file import WireFile
    from kimo_cli.wire.types import TurnBegin, TurnEnd

    wire = WireFile(path=tmp_path / "wire.jsonl")

    async def write() -> None:
        await wire.append_message(TurnBegin(user_input="形成上周的周报"))
        await wire.append_message(TurnEnd())
        await wire.append_message(TurnBegin(user_input="继续"))
        await wire.append_message(TextPart(text="周报已写好。"))
        await wire.append_message(TurnEnd())

    asyncio.run(write())

    assert sessions_api.extract_first_turn_from_wire(tmp_path) == ("形成上周的周报", "")


@pytest.mark.anyio
async def test_startup_dir_falls_back_when_last_dir_is_evolution(
    isolated_share_dir: Path, monkeypatch
) -> None:
    """A last_work_dir under <share>/evolution must not become the startup dir."""
    from kimo_cli.metadata import Metadata, save_metadata

    monkeypatch.setattr(sessions_api, "get_share_dir", lambda: isolated_share_dir)
    evo = isolated_share_dir / "evolution" / "verify_runs" / "p1" / "baseline"
    save_metadata(Metadata(last_work_dir=str(evo)))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(startup_dir="/fallback")))

    assert await sessions_api.get_startup_dir(request) == "/fallback"


@pytest.mark.anyio
async def test_startup_dir_falls_back_when_last_dir_is_im_bridge(
    isolated_share_dir: Path, monkeypatch
) -> None:
    """A last_work_dir under <share>/im_bridge must not become the startup dir."""
    from kimo_cli.metadata import Metadata, save_metadata

    monkeypatch.setattr(sessions_api, "get_share_dir", lambda: isolated_share_dir)
    im_ws = isolated_share_dir / "im_bridge" / "workspaces" / "ch1_ou_abc"
    save_metadata(Metadata(last_work_dir=str(im_ws)))
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(startup_dir="/fallback")))

    assert await sessions_api.get_startup_dir(request) == "/fallback"


def test_get_work_dirs_filters_evolution_dirs(isolated_share_dir: Path, monkeypatch) -> None:
    """Evolution artifact dirs must not surface in the recent work dirs list."""
    from kimo_cli.metadata import Metadata, WorkDirMeta, save_metadata

    monkeypatch.setattr(sessions_api, "get_share_dir", lambda: isolated_share_dir)
    # Use a real directory that is not under /tmp|/var/folders|/.cache/ so the
    # existing temp-dir filter does not swallow the legit entry.
    normal = Path.cwd()
    evo_root = isolated_share_dir / "evolution"
    leaked = [
        WorkDirMeta(path=str(evo_root / "verify_runs" / "p1" / "baseline")),
        WorkDirMeta(path=str(evo_root / "verify_runs" / "p1" / "candidate")),
        WorkDirMeta(path=str(evo_root / "proposals" / "p2")),
        WorkDirMeta(path=str(normal)),
    ]
    save_metadata(Metadata(work_dirs=leaked))
    sessions_api.invalidate_work_dirs_cache()

    assert sessions_api._get_work_dirs_sync() == [str(normal)]


def test_get_work_dirs_filters_im_bridge_dirs(isolated_share_dir: Path, monkeypatch) -> None:
    """IM-bridge per-sender workspaces must not surface in the recent work dirs list."""
    from kimo_cli.metadata import Metadata, WorkDirMeta, save_metadata

    monkeypatch.setattr(sessions_api, "get_share_dir", lambda: isolated_share_dir)
    # Use a real directory that is not under /tmp|/var/folders|/.cache/ so the
    # existing temp-dir filter does not swallow the legit entry.
    normal = Path.cwd()
    im_root = isolated_share_dir / "im_bridge"
    leaked = [
        WorkDirMeta(path=str(im_root / "workspaces" / "ch1_ou_abc")),
        WorkDirMeta(path=str(im_root / "ch1" / "media")),
        WorkDirMeta(path=str(normal)),
    ]
    save_metadata(Metadata(work_dirs=leaked))
    sessions_api.invalidate_work_dirs_cache()

    assert sessions_api._get_work_dirs_sync() == [str(normal)]


@pytest.mark.anyio
async def test_session_create_skips_recent_for_evolution_subprocess(
    isolated_share_dir: Path, work_dir: KaosPath, monkeypatch
) -> None:
    """A verify subprocess session must not pollute work_dirs / last_work_dir."""
    from kimo_cli.metadata import Metadata
    from kimo_cli.session import Session

    monkeypatch.setenv("KIMO_EVOLUTION_SUBPROCESS", "1")
    monkeypatch.setattr("kimo_cli.metadata.get_current_kaos", lambda: SimpleNamespace(name="local"))
    md = Metadata()
    saved = []
    monkeypatch.setattr("kimo_cli.session.load_metadata", lambda: md)
    monkeypatch.setattr("kimo_cli.session.save_metadata", lambda m: saved.append(m))

    session = await Session.create(work_dir, session_id="evo-sub")

    assert saved == []
    assert md.work_dirs == []
    assert md.last_work_dir is None
    assert session.id == "evo-sub"


@pytest.mark.anyio
async def test_session_create_records_recent_normally(
    isolated_share_dir: Path, work_dir: KaosPath, monkeypatch
) -> None:
    """A normal session still records the work dir as recently used."""
    from kimo_cli.metadata import Metadata
    from kimo_cli.session import Session

    monkeypatch.delenv("KIMO_EVOLUTION_SUBPROCESS", raising=False)
    monkeypatch.setattr("kimo_cli.metadata.get_current_kaos", lambda: SimpleNamespace(name="local"))
    md = Metadata()
    saved = []
    monkeypatch.setattr("kimo_cli.session.load_metadata", lambda: md)
    monkeypatch.setattr("kimo_cli.session.save_metadata", lambda m: saved.append(m))

    session = await Session.create(work_dir, session_id="norm")

    assert len(saved) == 1
    assert len(md.work_dirs) == 1
    assert md.work_dirs[0].path == str(work_dir)
    assert md.last_work_dir == str(work_dir)
    assert session.id == "norm"
