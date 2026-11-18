"""Tests for the IM bridge: db crud, REST API, and adapter lifecycle."""

from __future__ import annotations

import asyncio
import contextlib
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from kimo_cli.web.db.database import init_db
from kimo_cli.web.im import db
from kimo_cli.web.im.feishu_adapter import FeishuAdapter
from kimo_cli.web.im.parser import _sniff_image_mime

_TEST_ADMIN_PASSWORD = "im-bridge-test-password"


@pytest.fixture
def client(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> TestClient:
    """An app on a throwaway share dir (users.db lives under it)."""
    monkeypatch.setenv("KIMO_SHARE_DIR", str(tmp_path))
    monkeypatch.setenv("KIMO_USE_CONTAINERS", "false")
    monkeypatch.delenv("KIMO_WEB_SESSION_TOKEN", raising=False)
    monkeypatch.setenv("KIMO_INITIAL_ADMIN_PASSWORD", _TEST_ADMIN_PASSWORD)
    init_db()

    from kimo_cli.web.app import create_app

    with TestClient(create_app()) as c:
        yield c


# ---------------------------------------------------------------------- #
# db layer
# ---------------------------------------------------------------------- #
def test_channel_crud_and_config_defaults(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KIMO_SHARE_DIR", str(tmp_path))
    init_db()

    row = db.create_channel(type="feishu", name="测试渠道", app_id="cli_x", app_secret="sec")
    assert row.id > 0
    assert row.enabled is False
    # Defaults merged for absent config keys.
    assert row.config["show_thinking"] is True
    assert row.config["timeout_seconds"] == 600

    updated = db.update_channel(row.id, enabled=True, config={"timeout_seconds": 300})
    assert updated is not None
    assert updated.enabled is True
    assert updated.config["timeout_seconds"] == 300
    # Unrelated config keys survive a partial update.
    assert updated.config["show_thinking"] is True
    # Secret survives a patch that does not mention it.
    again = db.update_channel(row.id, name="改名")
    assert again is not None and again.app_secret == "sec"

    db.delete_channel(row.id)
    assert db.get_channel(row.id) is None


def test_whitelist_and_bindings(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KIMO_SHARE_DIR", str(tmp_path))
    init_db()
    row = db.create_channel(type="feishu", name="w")

    db.set_whitelist(row.id, ["ou_a", "ou_b", "ou_a", "", "  "])
    assert db.get_whitelist(row.id) == ["ou_a", "ou_b"]
    assert db.is_whitelisted(row.id, "ou_a") is True
    assert db.is_whitelisted(row.id, "ou_c") is False

    db.add_whitelist(row.id, "ou_c")
    assert db.get_whitelist(row.id) == ["ou_a", "ou_b", "ou_c"]

    db.set_binding(row.id, "ou_a", "sess-1", "张三")
    assert db.get_binding(row.id, "ou_a") == "sess-1"
    db.set_binding(row.id, "ou_a", "sess-2", "张三")
    assert db.get_binding(row.id, "ou_a") == "sess-2"  # upsert
    db.drop_binding(row.id, "ou_a")
    assert db.get_binding(row.id, "ou_a") is None

    # Deleting a channel cleans up its whitelist and bindings.
    db.set_whitelist(row.id, ["ou_a"])
    db.set_binding(row.id, "ou_a", "sess-1")
    db.delete_channel(row.id)
    assert db.get_whitelist(row.id) == []
    assert db.list_bindings(row.id) == []


def test_pending_record_approve_flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KIMO_SHARE_DIR", str(tmp_path))
    init_db()
    row = db.create_channel(type="feishu", name="p")

    # A stranger messages twice: recorded once, count grows, last_seen updates.
    db.record_pending(row.id, "ou_x", "ou_x")
    time.sleep(0.01)
    db.record_pending(row.id, "ou_x", "")
    pending = db.list_pending(row.id)
    assert len(pending) == 1
    assert pending[0]["external_id"] == "ou_x"
    assert pending[0]["message_count"] == 2
    first_seen = pending[0]["last_seen"]

    # Keep only the newest three: older entries are evicted FIFO.
    for i in range(5):
        db.record_pending(row.id, f"ou_{i}", f"ou_{i}")
        time.sleep(0.01)
    pending = db.list_pending(row.id)
    assert len(pending) == 3
    assert [p["external_id"] for p in pending] == ["ou_4", "ou_3", "ou_2"]

    # Re-recording an existing pending refreshes its position instead of
    # duplicating it.
    time.sleep(0.01)
    db.record_pending(row.id, "ou_2", "ou_2")
    pending = db.list_pending(row.id)
    assert [p["external_id"] for p in pending] == ["ou_2", "ou_4", "ou_3"]

    # Approving moves them to the whitelist and clears that pending entry.
    db.add_whitelist(row.id, "ou_x")
    db.delete_pending(row.id, "ou_x")
    assert all(p["external_id"] != "ou_x" for p in db.list_pending(row.id))
    assert db.is_whitelisted(row.id, "ou_x") is True
    assert pending[0]["last_seen"] >= first_seen


# ---------------------------------------------------------------------- #
# REST API
# ---------------------------------------------------------------------- #
def test_api_channel_roundtrip_hides_secret(client: TestClient) -> None:
    resp = client.post(
        "/api/im/channels",
        json={
            "type": "feishu",
            "name": "飞书机器人",
            "app_id": "cli_abc",
            "app_secret": "top-secret",
            "config": {"show_thinking": False},
        },
    )
    assert resp.status_code == 201, resp.text
    created = resp.json()
    assert created["has_secret"] is True
    assert "app_secret" not in created
    assert created["config"]["show_thinking"] is False
    assert created["config"]["timeout_seconds"] == 600  # default merged
    assert created["status"]["running"] is False

    listing = client.get("/api/im/channels").json()
    assert all("app_secret" not in c for c in listing)

    # Patch with empty secret leaves it unchanged.
    resp = client.patch(f"/api/im/channels/{created['id']}", json={"app_secret": ""})
    assert resp.status_code == 200, resp.text
    assert resp.json()["has_secret"] is True

    resp = client.patch(
        f"/api/im/channels/{created['id']}",
        json={"app_secret": "new-secret", "enabled": True},
    )
    assert resp.json()["has_secret"] is True

    resp = client.delete(f"/api/im/channels/{created['id']}")
    assert resp.status_code == 204
    assert client.get("/api/im/channels").json() == []


def test_api_404s(client: TestClient) -> None:
    assert client.patch("/api/im/channels/999", json={"name": "x"}).status_code == 404
    assert client.delete("/api/im/channels/999").status_code == 204  # idempotent
    assert client.get("/api/im/channels/999/whitelist").status_code == 404
    assert client.put("/api/im/channels/999/whitelist", json={"open_ids": []}).status_code == 404
    assert client.get("/api/im/channels/999/bindings").status_code == 404


def test_api_whitelist_and_bindings(client: TestClient) -> None:
    channel_id = client.post("/api/im/channels", json={"type": "feishu", "name": "w"}).json()["id"]

    resp = client.put(
        f"/api/im/channels/{channel_id}/whitelist",
        json={"open_ids": [" ou_a ", "", "ou_b"]},
    )
    assert resp.status_code == 204
    assert client.get(f"/api/im/channels/{channel_id}/whitelist").json()["open_ids"] == [
        "ou_a",
        "ou_b",
    ]

    db.set_binding(channel_id, "ou_a", "sess-9", "张三")
    bindings = client.get(f"/api/im/channels/{channel_id}/bindings").json()
    assert bindings == [{"external_id": "ou_a", "display_name": "张三", "session_id": "sess-9"}]


def test_api_pending_approve_flow(client: TestClient) -> None:
    channel_id = client.post("/api/im/channels", json={"type": "feishu", "name": "p"}).json()["id"]

    # A stranger messages the bot: adapter records them, UI sees the pending list.
    db.record_pending(channel_id, "ou_stranger", "ou_stranger")
    pending = client.get(f"/api/im/channels/{channel_id}/pending").json()
    assert len(pending) == 1
    assert pending[0]["external_id"] == "ou_stranger"
    assert pending[0]["message_count"] == 1

    # One-click approve: whitelist gains them, pending clears.
    resp = client.post(f"/api/im/channels/{channel_id}/pending/ou_stranger/approve")
    assert resp.status_code == 204
    assert client.get(f"/api/im/channels/{channel_id}/pending").json() == []
    assert client.get(f"/api/im/channels/{channel_id}/whitelist").json()["open_ids"] == [
        "ou_stranger"
    ]

    # Dismiss without approving works too.
    db.record_pending(channel_id, "ou_other", "")
    resp = client.delete(f"/api/im/channels/{channel_id}/pending/ou_other")
    assert resp.status_code == 204
    assert client.get(f"/api/im/channels/{channel_id}/pending").json() == []
    assert client.get(f"/api/im/channels/{channel_id}/whitelist").json()["open_ids"] == [
        "ou_stranger"
    ]

    assert client.get("/api/im/channels/999/pending").status_code == 404


# ---------------------------------------------------------------------- #
# adapter lifecycle through the manager
# ---------------------------------------------------------------------- #
class _RecordingAdapter:
    """Stand-in adapter: records start/stop, reports connected status."""

    instances: list[_RecordingAdapter] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        self.started = False
        self.stopped = False
        _RecordingAdapter.instances.append(self)

    async def start(self, ctx: Any) -> None:
        self.started = True
        ctx.on_status(None)  # connected
        await asyncio.Event().wait()  # run until cancelled

    async def stop(self) -> None:
        self.stopped = True


def test_enabled_channel_spawns_and_stops_adapter(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    _RecordingAdapter.instances.clear()
    monkeypatch.setattr(
        "kimo_cli.web.im.manager.ImBridgeManager._build_adapter",
        lambda self, channel: _RecordingAdapter(),
    )

    channel_id = client.post(
        "/api/im/channels",
        json={"type": "feishu", "name": "live", "enabled": True},
    ).json()["id"]

    def _running() -> bool:
        return client.get("/api/im/channels").json()[0]["status"]["running"]

    deadline = time.time() + 5
    while not _running() and time.time() < deadline:
        time.sleep(0.05)
    assert _running() is True
    assert len(_RecordingAdapter.instances) == 1

    client.patch(f"/api/im/channels/{channel_id}", json={"enabled": False})
    deadline = time.time() + 5
    while _running() and time.time() < deadline:
        time.sleep(0.05)
    assert _running() is False


# ---------------------------------------------------------------------- #
# small unit checks on ported feishu helpers
# ---------------------------------------------------------------------- #
def test_adapter_teardown_closes_ws_and_disables_reconnect(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disabling a channel must close the Feishu long connection.

    Regression: task cancellation alone left the SDK's receive loop holding
    the socket, so a disabled channel kept receiving messages and shared the
    bot with any other agent bridged to the same app credentials.
    """
    import lark_oapi as lark

    from kimo_cli.web.im.adapter import AdapterContext

    class _FakeClient:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            self._auto_reconnect = True
            self.connected = False
            self.closed = False

        async def _connect(self) -> None:
            self.connected = True

        async def _disconnect(self) -> None:
            self.closed = True

    fakes: list[_FakeClient] = []

    def _ctor(*args: Any, **kwargs: Any) -> _FakeClient:
        client = _FakeClient()
        fakes.append(client)
        return client

    monkeypatch.setattr(lark.ws, "Client", _ctor)

    async def _noop(*args: Any, **kwargs: Any) -> None:
        return None

    status: dict[str, Any] = {}
    ctx = AdapterContext(
        channel_id=1,
        channel_name="t",
        config={},
        is_whitelisted=_noop,
        run_turn=_noop,
        drop_session=_noop,
        record_pending=_noop,
        on_status=lambda e: status.update(last_error=e),
    )

    async def _run() -> None:
        adapter = FeishuAdapter("id", "sec", Path("/tmp/im-test-media"))
        task = asyncio.create_task(adapter.start(ctx))
        deadline = time.time() + 5
        while not (fakes and fakes[0].connected) and time.time() < deadline:
            await asyncio.sleep(0.01)
        assert fakes[0].connected, "adapter never connected"
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        assert fakes[0].closed, "cancelled adapter left the ws connection open"
        assert fakes[0]._auto_reconnect is False, "receive loop would reconnect"
        assert adapter._ctx is None, "late events would still be dispatched"

    asyncio.run(_run())


def test_sniff_image_mime() -> None:
    import base64

    png = base64.b64encode(b"\x89PNG\r\n\x1a\n" + b"0" * 20).decode()
    jpg = base64.b64encode(b"\xff\xd8\xff\xe0" + b"0" * 20).decode()
    assert _sniff_image_mime(png) == "image/png"
    assert _sniff_image_mime(jpg) == "image/jpeg"
    assert _sniff_image_mime("AAAA") == "image/jpeg"  # fallback


def test_adapter_dedup_and_plain_text() -> None:
    adapter = FeishuAdapter("id", "sec", Path("/tmp/im-test-media"))
    assert adapter._is_duplicate("m1") is False
    assert adapter._is_duplicate("m1") is True
    assert adapter._is_duplicate("m2") is False

    parts = [
        {"type": "text", "text": " /ok_reset "},
        {"type": "image_url", "image_url": {"url": "data:..."}},
    ]
    assert FeishuAdapter._plain_text(parts).strip() in ("/ok_reset", "/reset")


# ---------------------------------------------------------------------- #
# DingTalk adapter
# ---------------------------------------------------------------------- #
def test_dingtalk_adapter_teardown_closes_connection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Disabling a dingtalk channel must drop the stream connection.

    Same ruling as the Feishu teardown regression: the SDK loop swallows
    cancellation, so the adapter's own finally must close the websocket or a
    disabled channel would keep receiving (and sharing the bot).
    """

    import kimo_cli.web.im.dingtalk_adapter as dt

    class _FakeWs:
        def __init__(self) -> None:
            self.closed = False

        async def close(self) -> None:
            self.closed = True

    class _FakeClient:
        def __init__(self, credential: Any, stop: Any, **kwargs: Any) -> None:
            self._stop = stop
            self.websocket: _FakeWs | None = None
            self.callback_handler_map: dict = {}
            self.started = False

        def register_callback_handler(self, topic: Any, handler: Any) -> None:
            pass

        async def start(self) -> None:
            self.started = True
            self.websocket = _FakeWs()
            while not self._stop.is_set():
                await asyncio.sleep(0.01)

    fakes: list[_FakeClient] = []

    def _ctor(credential: Any, stop: Any, **kwargs: Any) -> _FakeClient:
        client = _FakeClient(credential, stop, **kwargs)
        fakes.append(client)
        return client

    monkeypatch.setattr(dt, "_CancellableStreamClient", _ctor)

    async def _run() -> None:
        adapter = dt.DingTalkAdapter("id", "sec", Path("/tmp/im-test-media"))
        task = asyncio.create_task(adapter.start(_make_ctx()))
        deadline = time.time() + 5
        while not (fakes and fakes[0].started) and time.time() < deadline:
            await asyncio.sleep(0.01)
        assert fakes[0].started, "adapter never connected"
        ws_ref = fakes[0].websocket
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        assert ws_ref is not None and ws_ref.closed, (
            "cancelled adapter left the stream connection open"
        )
        assert adapter._ctx is None, "late events would still be dispatched"

    asyncio.run(_run())


def _make_ctx() -> Any:
    from kimo_cli.web.im.adapter import AdapterContext

    async def _noop(*args: Any, **kwargs: Any) -> None:
        return None

    return AdapterContext(
        channel_id=1,
        channel_name="t",
        config={},
        is_whitelisted=_noop,
        run_turn=_noop,
        drop_session=_noop,
        record_pending=_noop,
        on_status=lambda _e: None,
    )


def test_dingtalk_parse_text_and_group_gate() -> None:
    import kimo_cli.web.im.dingtalk_adapter as dt

    adapter = dt.DingTalkAdapter("id", "sec", Path("/tmp/im-test-media"))
    incoming = dt.ds.ChatbotMessage.from_dict(
        {
            "msgtype": "text",
            "text": {"content": "你好"},
            "msgId": "m1",
            "senderId": "u1",
            "senderStaffId": "s1",
            "senderNick": "小伟",
            "conversationType": "1",
        }
    )
    parts = asyncio.run(adapter._parse(incoming))
    assert parts == [{"type": "text", "text": "你好"}]

    # Group message without @mention is ignored; with @ and config on, passes.
    group = dt.ds.ChatbotMessage.from_dict(
        {
            "msgtype": "text",
            "text": {"content": "hi"},
            "msgId": "m2",
            "senderId": "u1",
            "senderStaffId": "s1",
            "conversationType": "2",
            "isInAtList": False,
        }
    )
    assert asyncio.run(adapter._parse(group))  # parse itself is type-agnostic

    async def _gate(is_at: bool, enabled: bool) -> bool:
        adapter._ctx = _make_ctx_with_config(enabled)
        got: list = []

        async def fake_run(external_id, display, user_input):
            got.append(user_input)
            return "ok"

        adapter._ctx.run_turn = fake_run  # type: ignore[method-assign]
        incoming = dt.ds.ChatbotMessage.from_dict(
            {
                "msgtype": "text",
                "text": {"content": "hi"},
                "msgId": f"m-at{is_at}-en{enabled}",
                "senderId": "u1",
                "senderStaffId": "s1",
                "conversationType": "2",
                "isInAtList": is_at,
            }
        )
        await adapter.handle(incoming)
        return bool(got)

    assert asyncio.run(_gate(is_at=False, enabled=True)) is False
    assert asyncio.run(_gate(is_at=True, enabled=False)) is False
    assert asyncio.run(_gate(is_at=True, enabled=True)) is True


def _make_ctx_with_config(group_enabled: bool) -> Any:
    from kimo_cli.web.im.adapter import AdapterContext

    async def _whitelist(_e: str) -> bool:
        return True

    async def _noop(*args: Any, **kwargs: Any) -> None:
        return None

    async def _run_turn(_e: str, _n: str, _u: list) -> str:
        return "ok"

    ctx = AdapterContext(
        channel_id=1,
        channel_name="t",
        config={"group_chat_enabled": group_enabled, "show_thinking": False},
        is_whitelisted=_whitelist,
        run_turn=_run_turn,
        drop_session=_noop,
        record_pending=_noop,
        on_status=lambda _e: None,
    )
    return ctx


def test_dingtalk_stream_client_accepts_status_callbacks() -> None:
    """Construct the real stream client with status hooks.

    Regression: wiring that passes on_connected/on_error must match the
    class signature — a monkeypatched fake hid a TypeError that only fired
    in the packaged app.
    """
    import threading

    import dingtalk_stream as ds

    import kimo_cli.web.im.dingtalk_adapter as dt

    stop = threading.Event()
    fired: list[str] = []
    client = dt._CancellableStreamClient(
        ds.Credential("id", "sec"),
        stop,
        on_connected=lambda: fired.append("connected"),
        on_error=lambda e: fired.append(f"error:{e}"),
    )
    assert client._stop_flag is stop
    client._on_connected()
    client._on_error("boom")
    assert fired == ["connected", "error:boom"]


def test_manager_keeps_last_error_after_channel_crash(
    client: TestClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A crashed adapter's error must stay visible instead of blanking out."""

    class _CrashingAdapter:
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            pass

        async def start(self, ctx: Any) -> None:
            raise RuntimeError("boom")

        async def stop(self) -> None:
            return None

    monkeypatch.setattr(
        "kimo_cli.web.im.manager.ImBridgeManager._build_adapter",
        lambda self, channel: _CrashingAdapter(),
    )

    client.post(
        "/api/im/channels",
        json={"type": "feishu", "name": "crashy", "enabled": True},
    )

    def _status() -> dict:
        return client.get("/api/im/channels").json()[0]["status"]

    deadline = time.time() + 5
    while _status()["last_error"] is None and time.time() < deadline:
        time.sleep(0.05)
    assert "boom" in (_status()["last_error"] or "")
    assert _status()["running"] is False


def test_im_binding_reused_across_turns(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A sender must keep ONE session across messages.

    Regression: Session.continue_() resolves via the global metadata's
    ``last_session_id``, which only the web runner writes — IM sessions never
    set it, so every message created a fresh session and the conversation lost
    its context.
    """
    from kimo_cli import metadata as metadata_mod
    from kimo_cli.web.im import runner as im_runner

    bindings: dict = {}
    monkeypatch.setattr(im_runner.db, "get_binding", lambda ch, ex: bindings.get((ch, ex)))
    monkeypatch.setattr(
        im_runner.db,
        "set_binding",
        lambda ch, ex, sid, name: bindings.__setitem__((ch, ex), sid),
    )
    monkeypatch.setattr(im_runner.share, "get_share_dir", lambda: tmp_path)
    monkeypatch.setattr(metadata_mod, "get_metadata_file", lambda: tmp_path / "kimo.json")

    async def _go() -> tuple:
        s1 = await im_runner._load_or_create_session(9, "ext1", "测试人", "t")
        s2 = await im_runner._load_or_create_session(9, "ext1", "测试人", "t")
        return s1, s2

    s1, s2 = asyncio.run(_go())
    assert s1.id == s2.id, "second message landed in a different session"


def test_im_turn_answers_questions_automatically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AskUserQuestion must be auto-answered so IM turns never hang on it."""
    from kimo_cli.web.im.runner import ImTurnRunner
    from kimo_cli.wire.types import QuestionItem, QuestionOption, QuestionRequest

    class _FakeQueue:
        def __init__(self, msgs: list) -> None:
            self._it = iter(msgs)

        async def get(self):  # noqa: ANN202 - test double
            for m in self._it:
                return m
            await asyncio.sleep(3600)

    class _FakeHub:
        def __init__(self, msgs: list) -> None:
            self._msgs = msgs

        def subscribe(self):  # noqa: ANN202 - test double
            return _FakeQueue(self._msgs)

        def unsubscribe(self, q) -> None:  # noqa: ANN001 - test double
            pass

    request = QuestionRequest(
        id="q1",
        tool_call_id="t1",
        questions=[
            QuestionItem(
                question="继续吗？",
                header="",
                options=[QuestionOption(label="是", description="")],
                multi_select=False,
            )
        ],
    )
    answered: dict = {}
    original_resolve = QuestionRequest.resolve

    def _spy(self: QuestionRequest, answers: dict) -> None:
        answered.update(answers)
        original_resolve(self, answers)

    monkeypatch.setattr(QuestionRequest, "resolve", _spy)

    fake_kimo = type("K", (), {"_runtime": type("R", (), {"root_wire_hub": _FakeHub([request])})})

    async def _go() -> None:
        task = asyncio.create_task(ImTurnRunner._answer_questions(fake_kimo))
        await asyncio.sleep(0.1)
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task

    asyncio.run(_go())
    assert answered.get("继续吗？") and "IM" in answered["继续吗？"], (
        "question was not auto-answered with the IM guidance note"
    )
