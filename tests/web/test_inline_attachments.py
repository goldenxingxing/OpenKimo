"""A file placed in the message text reaches the model at that spot."""

from __future__ import annotations

import io
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from PIL import Image

from kimo_cli.config import LLMModel
from kimo_cli.web.runner import process as process_module
from kimo_cli.web.runner.process import SessionProcess
from kimo_cli.wire.jsonrpc import JSONRPCPromptMessage


@pytest.fixture
def uploads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    session_dir = tmp_path / "session"
    folder = session_dir / "uploads"
    folder.mkdir(parents=True)
    buffer = io.BytesIO()
    Image.new("RGB", (4, 4), "red").save(buffer, format="PNG")
    (folder / "shot_a1b2c3.png").write_bytes(buffer.getvalue())
    (folder / "report_d4e5f6.pdf").write_bytes(b"%PDF-1.4 not really")
    (folder / "notes_0a0b0c.md").write_text("# Notes\nline\n", encoding="utf-8")

    monkeypatch.setattr(
        process_module,
        "load_session_by_id",
        lambda _id: SimpleNamespace(kimo_cli_session=SimpleNamespace(dir=session_dir), model="m"),
    )
    config = SimpleNamespace(
        default_model="m",
        models={"m": LLMModel(provider="p", model="m", max_context_size=1000)},
    )
    monkeypatch.setattr(process_module, "load_config", lambda: config)
    monkeypatch.setattr(process_module, "derive_model_capabilities", lambda _m: {"image_in"})
    return folder


async def _prompt(text: str) -> list[dict]:
    process = SessionProcess(uuid4())
    message = JSONRPCPromptMessage(id="p1", params={"user_input": text})
    forwarded = await process._handle_in_message(message)
    assert forwarded is not None
    return json.loads(forwarded)["params"]["user_input"]


def _texts(parts: list[dict]) -> list[str]:
    return [part["text"] if part["type"] == "text" else f"<{part['type']}>" for part in parts]


async def test_placed_files_sit_where_their_markers_stood(uploads: Path) -> None:
    parts = await _prompt(
        '把 <attachment file="shot_a1b2c3.png"/> 和 <attachment file="report_d4e5f6.pdf"/> 对比'
    )
    texts = _texts(parts)

    # The file the message did not place still goes first, as before.
    assert texts[0].startswith("<uploaded_files>")
    assert "notes_0a0b0c.md" in texts[0]
    assert "shot_a1b2c3.png" not in texts[0] and "report_d4e5f6.pdf" not in texts[0]
    assert texts[1].startswith('<document path="') and 'inline="1"' not in texts[1]

    rest = texts[texts.index("</document>\n\n") + 1 :]
    shot = str(uploads / "shot_a1b2c3.png")
    report = str(uploads / "report_d4e5f6.pdf")
    assert rest == [
        "把 ",
        f'<image path="{shot}" content_type="image/png" inline="1">',
        "<image_url>",
        "</image>",
        " 和 ",
        f'<file path="{report}" content_type="application/pdf" inline="1"/>',
        " 对比",
    ]


async def test_a_marker_for_a_file_that_is_not_there_is_dropped(uploads: Path) -> None:
    texts = _texts(await _prompt('看 <attachment file="missing_000000.png"/> 这个'))

    assert "看 " in texts and " 这个" in texts
    assert not any("missing_000000" in text for text in texts)


async def test_a_message_without_markers_is_unchanged(uploads: Path) -> None:
    texts = _texts(await _prompt("hello"))

    assert texts[0].startswith("<uploaded_files>")
    assert texts[-1] == "hello"
    assert not any('inline="1"' in text for text in texts)
