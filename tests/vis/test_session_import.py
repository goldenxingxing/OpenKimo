"""Importing a session archive must not be able to hurt the machine.

Two things used to go wrong, both of them invisible in the response:

* The upload was read whole into memory, and the compressed size was
  checked against a limit that a ZIP bomb walks straight past — the archive
  declares its members' *expanded* size, and nothing bounded it.
* Unpacking ran on the event loop, so a 200 MB import stalled every other
  request in the process for the duration.
"""

from __future__ import annotations

import io
import threading
import zipfile
from pathlib import Path

import pytest
from fastapi import UploadFile
from fastapi.testclient import TestClient

from kimo_cli.vis import app as vis_app
from kimo_cli.vis.api import sessions as vis_sessions


def _zip_bytes(entries: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    return buffer.getvalue()


def _bomb_zip_bytes(expanded_bytes: int) -> bytes:
    """A ZIP that expands to *expanded_bytes* but is tiny on the wire."""
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("wire.jsonl", b"\0" * expanded_bytes)
    return buffer.getvalue()


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> TestClient:
    monkeypatch.setenv("KIMO_SHARE_DIR", str(tmp_path))
    return TestClient(vis_app.create_app())


def test_an_archive_that_expands_too_far_is_refused(client: TestClient) -> None:
    """The compressed size is not the size that lands on disk."""
    bomb = _bomb_zip_bytes(vis_sessions._MAX_EXPANDED_BYTES + 1)

    response = client.post(
        "/api/vis/sessions/import",
        files={"file": ("session.zip", io.BytesIO(bomb), "application/zip")},
    )

    assert response.status_code == 413
    assert "expands" in response.json()["detail"]


def test_an_archive_within_the_limit_still_imports(
    client: TestClient,
    tmp_path: Path,
) -> None:
    payload = _zip_bytes({"wire.jsonl": b'{"turn": 1}\n'})

    response = client.post(
        "/api/vis/sessions/import",
        files={"file": ("session.zip", io.BytesIO(payload), "application/zip")},
    )

    assert response.status_code == 200, response.text
    session_id = response.json()["session_id"]
    imported = tmp_path / "imported_sessions" / session_id
    assert (imported / "wire.jsonl").read_text(encoding="utf-8") == '{"turn": 1}\n'


@pytest.mark.asyncio
async def test_the_import_does_not_block_the_event_loop(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Unpacking runs off the loop, so the loop stays free while it works."""
    monkeypatch.setenv("KIMO_SHARE_DIR", str(tmp_path))

    event_loop_thread = threading.get_ident()
    extracted_in: list[int] = []

    def _record_extract(zf: zipfile.ZipFile, session_dir: Path) -> None:
        extracted_in.append(threading.get_ident())

    monkeypatch.setattr(vis_sessions, "_extract_and_flatten", _record_extract)

    payload = _zip_bytes({"wire.jsonl": b'{"turn": 1}\n'})
    upload = UploadFile(
        file=io.BytesIO(payload),
        filename="session.zip",
        size=len(payload),
    )

    result = await vis_sessions.import_session(upload)

    assert result["session_id"]
    assert extracted_in and extracted_in[0] != event_loop_thread
