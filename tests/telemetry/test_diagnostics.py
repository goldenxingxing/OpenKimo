"""Regression tests for the telemetry diagnostics."""

from __future__ import annotations

import pytest

import kimo_cli.telemetry as telemetry_mod
from kimo_cli.telemetry import disable, track_session_started_once
from kimo_cli.telemetry.transport import AsyncTransport


@pytest.fixture(autouse=True)
def _reset_state():
    telemetry_mod._session_started_sessions.clear()
    telemetry_mod._session_started_order.clear()
    telemetry_mod._event_queue.clear()
    telemetry_mod._sink = None
    telemetry_mod._disabled = False
    yield
    telemetry_mod._session_started_sessions.clear()
    telemetry_mod._session_started_order.clear()


# ---------------------------------------------------------------------------
# F165 — the transport logs say what went wrong
# ---------------------------------------------------------------------------


async def test_transport_logs_the_underlying_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """``except Exception:`` without the exception object told nobody anything."""
    from kimo_cli import logger as kimo_logger

    transport = AsyncTransport()

    async def _boom(_events):
        raise RuntimeError("connection reset by peer")

    monkeypatch.setattr(transport, "_send_http", _boom)

    records: list[str] = []
    kimo_logger.enable("kimo_cli")
    sink_id = kimo_logger.add(lambda message: records.append(str(message)), level="DEBUG")
    try:
        await transport.send([{"type": "probe"}])
    finally:
        kimo_logger.remove(sink_id)

    assert any("connection reset by peer" in record for record in records)


# ---------------------------------------------------------------------------
# F166 — disabling telemetry never raises
# ---------------------------------------------------------------------------


def test_disable_survives_a_broken_sink() -> None:
    """``clear_buffer`` was the one call in ``disable`` without a guard."""

    class _BrokenSink:
        def clear_buffer(self) -> None:
            raise RuntimeError("sink is broken")

        def flush_sync(self) -> None:
            return None

    telemetry_mod._sink = _BrokenSink()  # type: ignore[assignment]

    disable()  # must not raise


# ---------------------------------------------------------------------------
# F167 — the set of already-announced sessions stays bounded
# ---------------------------------------------------------------------------


def test_session_started_ids_are_evicted(monkeypatch: pytest.MonkeyPatch) -> None:
    """A long-lived ACP process used to accumulate one id per session."""
    monkeypatch.setattr(telemetry_mod, "_MAX_TRACKED_SESSIONS", 5)

    for index in range(20):
        monkeypatch.setattr(telemetry_mod, "_session_id", f"session-{index}")
        track_session_started_once(ui_mode="shell", resumed=False)

    assert len(telemetry_mod._session_started_sessions) <= 5
    # The most recent id is still remembered, so it is not announced twice.
    assert "session-19" in telemetry_mod._session_started_sessions
