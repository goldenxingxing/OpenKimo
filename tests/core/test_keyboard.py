from __future__ import annotations

import threading

from kimo_cli.ui.shell.keyboard import KeyboardListener


def test_pause_returns_when_listener_is_cancelled() -> None:
    listener = KeyboardListener()
    worker = threading.Thread(target=listener._pause_sync)
    worker.start()
    listener._cancel_event.set()
    worker.join(timeout=1)
    assert not worker.is_alive()


def test_pause_returns_if_already_cancelled() -> None:
    listener = KeyboardListener()
    listener._cancel_event.set()
    listener._pause_sync()
