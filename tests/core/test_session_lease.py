"""Session epoch lease against double writers.

A worker claims the lease once at startup (incrementing a monotonic epoch in
``lease.json``) and re-checks it before every session-file write. When the
gateway restarts and a fresh worker takes over while the old one is still
alive, the old worker's next write fails its check instead of forking the
transcript — jcode's ``session_lease.rs`` adapted to OpenKimo's file layout.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from kimo_cli.session_lease import (
    LeaseLostError,
    claim_session_lease,
)
from kimo_cli.wire.file import WireFile
from kimo_cli.wire.types import TurnBegin


def test_claim_increments_epoch(tmp_path: Path) -> None:
    lease1 = claim_session_lease(tmp_path)
    assert lease1.epoch == 1
    lease2 = claim_session_lease(tmp_path)
    assert lease2.epoch == 2
    on_disk = json.loads((tmp_path / "lease.json").read_text(encoding="utf-8"))
    assert on_disk["epoch"] == 2


def test_concurrent_claims_get_distinct_epochs(tmp_path: Path) -> None:
    with ThreadPoolExecutor(max_workers=8) as pool:
        leases = list(pool.map(lambda _: claim_session_lease(tmp_path), range(16)))
    assert sorted(lease.epoch for lease in leases) == list(range(1, 17))
    assert json.loads((tmp_path / "lease.json").read_text())["epoch"] == 16


def test_verify_fails_closed_for_non_object_lease_payload(tmp_path: Path) -> None:
    lease = claim_session_lease(tmp_path)
    (tmp_path / "lease.json").write_text("null", encoding="utf-8")
    with pytest.raises(LeaseLostError):
        lease.verify()


def test_verify_passes_while_we_hold_the_lease(tmp_path: Path) -> None:
    lease = claim_session_lease(tmp_path)
    lease.verify()  # no exception


def test_verify_fails_after_takeover(tmp_path: Path) -> None:
    old = claim_session_lease(tmp_path)
    claim_session_lease(tmp_path)  # a fresh worker takes over
    with pytest.raises(LeaseLostError):
        old.verify()


def test_verify_fails_closed_when_lease_file_unreadable(tmp_path: Path) -> None:
    lease = claim_session_lease(tmp_path)
    (tmp_path / "lease.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(LeaseLostError):
        lease.verify()


def test_missing_lease_file_starts_at_zero(tmp_path: Path) -> None:
    # Old sessions without lease.json: first claim is epoch 1.
    lease = claim_session_lease(tmp_path)
    assert lease.epoch == 1


@pytest.mark.asyncio
async def test_wire_append_fails_after_takeover(tmp_path: Path) -> None:
    wire_file = WireFile(tmp_path / "wire.jsonl")
    wire_file.lease = claim_session_lease(tmp_path)

    # Holder writes fine.
    await wire_file.append_message(TurnBegin(user_input="hello"))

    # A fresh worker takes over.
    claim_session_lease(tmp_path)

    with pytest.raises(LeaseLostError):
        await wire_file.append_message(TurnBegin(user_input="world"))


@pytest.mark.asyncio
async def test_wire_append_without_lease_is_unchanged(tmp_path: Path) -> None:
    wire_file = WireFile(tmp_path / "wire.jsonl")
    await wire_file.append_message(TurnBegin(user_input="hello"))
    await wire_file.append_message(TurnBegin(user_input="world"))
    assert wire_file.path.read_text(encoding="utf-8").count("TurnBegin") == 2


@pytest.mark.asyncio
async def test_stale_worker_cannot_fork_transcript(tmp_path: Path) -> None:
    """The end-to-end scenario: old worker paused mid-turn, gateway restarts,
    new worker claims and writes, old worker wakes up and tries to write."""
    old_lease = claim_session_lease(tmp_path)
    old_wire = WireFile(tmp_path / "wire.jsonl", lease=old_lease)

    new_lease = claim_session_lease(tmp_path)
    new_wire = WireFile(tmp_path / "wire.jsonl", lease=new_lease)

    await new_wire.append_message(TurnBegin(user_input="from new worker"))
    with pytest.raises(LeaseLostError):
        await old_wire.append_message(TurnBegin(user_input="from old worker"))

    lines = [
        line
        for line in old_wire.path.read_text(encoding="utf-8").splitlines()
        if "TurnBegin" in line
    ]
    assert len(lines) == 1
    assert "from new worker" in lines[0]
