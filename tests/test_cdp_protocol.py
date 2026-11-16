"""The CDP request/response pairing in BrowserCDPGuard.

Driven with a fake websocket — no Chromium, no container. Every CDP request
used to carry a hard-coded ``"id": 1`` while a timed-out command's reply was
left unread in the socket and the connection stayed cached, so the stale reply
was accepted as the answer to the *next* command.
"""

from __future__ import annotations

import asyncio
import json
import sys
import types
from pathlib import Path

import pytest

APP = Path(__file__).resolve().parents[1] / "app"
sys.path.insert(0, str(APP))

# browser_guard imports playwright and websockets at module scope; only the
# websockets exception type is reachable from the code under test here.
if "playwright" not in sys.modules:  # pragma: no cover - env dependent
    pw = types.ModuleType("playwright")
    aio = types.ModuleType("playwright.async_api")
    aio.async_playwright = lambda: None
    aio.Browser = object
    aio.BrowserContext = object
    aio.Page = object
    pw.async_api = aio
    sys.modules["playwright"] = pw
    sys.modules["playwright.async_api"] = aio

import browser_guard  # noqa: E402


class FakeWS:
    """Records what was sent and replays a scripted set of frames."""

    def __init__(self, frames: list[dict]) -> None:
        self.sent: list[dict] = []
        self._frames = [json.dumps(f) for f in frames]
        self.closed = False

    async def send(self, data: str) -> None:
        self.sent.append(json.loads(data))

    async def recv(self) -> str:
        if not self._frames:
            await asyncio.sleep(3600)  # nothing more to say; let the timeout win
        return self._frames.pop(0)


def _guard() -> browser_guard.BrowserCDPGuard:
    return browser_guard.BrowserCDPGuard()


class TestRequestIdsAreUnique:
    def test_each_command_gets_a_new_id(self) -> None:
        guard = _guard()
        ws = FakeWS([{"id": 1, "result": {"a": 1}}, {"id": 2, "result": {"b": 2}}])

        asyncio.run(guard._send_cdp_command(ws, "Browser.getWindowForTarget"))
        asyncio.run(guard._send_cdp_command(ws, "Browser.getWindowBounds"))

        ids = [m["id"] for m in ws.sent]
        assert ids == [1, 2], "a fixed id makes replies indistinguishable"

    def test_a_stale_reply_is_not_taken_as_the_next_result(self) -> None:
        """The exact misattribution the fixed id caused.

        Command 1 times out; its reply arrives afterwards. Command 2 must skip
        it and wait for its own. With `"id": 1` on every request, command 2
        returned command 1's windowId.
        """
        guard = _guard()
        # Nothing for the first command, so it times out.
        ws = FakeWS([])
        first = asyncio.run(guard._send_cdp_command(ws, "Browser.getWindowForTarget", timeout=0.05))
        assert first is None

        # Now the late reply to command 1 shows up, followed by command 2's.
        ws._frames = [
            json.dumps({"id": 1, "result": {"windowId": 111}}),
            json.dumps({"id": 2, "result": {"bounds": {"windowState": "minimized"}}}),
        ]
        second = asyncio.run(guard._send_cdp_command(ws, "Browser.getWindowBounds", timeout=1.0))

        assert second == {"bounds": {"windowState": "minimized"}}
        assert second != {"windowId": 111}, "stale reply must not be reused"

    def test_events_are_still_skipped(self) -> None:
        guard = _guard()
        ws = FakeWS(
            [
                {"method": "Page.frameNavigated", "params": {}},
                {"id": 1, "result": {"ok": True}},
            ]
        )

        assert asyncio.run(guard._send_cdp_command(ws, "Page.enable")) == {"ok": True}


class TestAClosedConnectionSurfaces:
    def test_connection_closed_propagates_to_the_caller(self) -> None:
        """It was swallowed into None, so the caller's evict branch was dead."""
        from websockets.exceptions import ConnectionClosed

        class DeadWS(FakeWS):
            async def send(self, data: str) -> None:
                raise ConnectionClosed(None, None)

        guard = _guard()
        with pytest.raises(ConnectionClosed):
            asyncio.run(guard._send_cdp_command(DeadWS([]), "Browser.getWindowBounds"))

    def test_other_errors_still_become_none(self) -> None:
        class BrokenWS(FakeWS):
            async def send(self, data: str) -> None:
                raise ValueError("nope")

        guard = _guard()
        assert asyncio.run(guard._send_cdp_command(BrokenWS([]), "X")) is None


class TestClosedDetection:
    """`if not ws` cannot see a closed socket: the object stays truthy."""

    def test_a_truthy_but_closed_socket_is_detected(self) -> None:
        legacy = types.SimpleNamespace(closed=True)
        assert bool(legacy) is True, "this is why `if not ws` never fired"
        assert browser_guard._ws_is_closed(legacy) is True

    def test_an_open_socket_is_not(self) -> None:
        assert browser_guard._ws_is_closed(types.SimpleNamespace(closed=False)) is False

    def test_the_newer_close_code_shape(self) -> None:
        assert browser_guard._ws_is_closed(types.SimpleNamespace(close_code=1006)) is True

    def test_the_state_enum_shape(self) -> None:
        closed = types.SimpleNamespace(state=types.SimpleNamespace(name="CLOSED"))
        open_ = types.SimpleNamespace(state=types.SimpleNamespace(name="OPEN"))
        assert browser_guard._ws_is_closed(closed) is True
        assert browser_guard._ws_is_closed(open_) is False

    def test_an_unknown_shape_is_treated_as_open(self) -> None:
        """A websockets version bump must degrade to the old behaviour."""
        assert browser_guard._ws_is_closed(object()) is False
