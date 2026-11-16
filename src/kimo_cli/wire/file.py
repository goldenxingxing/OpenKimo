from __future__ import annotations

import asyncio
import json
import os
import sys
import time
from collections.abc import AsyncIterator
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

import aiofiles
from pydantic import BaseModel, ConfigDict, ValidationError

from kimo_cli.session_lease import SessionLease
from kimo_cli.utils.logging import logger
from kimo_cli.wire.protocol import WIRE_PROTOCOL_LEGACY_VERSION, WIRE_PROTOCOL_VERSION
from kimo_cli.wire.types import WireMessage, WireMessageEnvelope


class WireFileMetadata(BaseModel):
    """Metadata header stored as the first line in wire.jsonl."""

    model_config = ConfigDict(extra="ignore")

    type: Literal["metadata"] = "metadata"
    protocol_version: str


class WireMessageRecord(BaseModel):
    """The persisted record of a `WireMessage`."""

    model_config = ConfigDict(extra="ignore")

    timestamp: float
    message: WireMessageEnvelope

    @classmethod
    def from_wire_message(cls, msg: WireMessage, *, timestamp: float) -> WireMessageRecord:
        return cls(timestamp=timestamp, message=WireMessageEnvelope.from_wire_message(msg))

    def to_wire_message(self) -> WireMessage:
        return self.message.to_wire_message()


def parse_wire_file_metadata(line: str) -> WireFileMetadata | None:
    """Parse a wire file metadata line; return None if the line is not metadata."""
    try:
        return WireFileMetadata.model_validate_json(line)
    except (ValidationError, ValueError):
        return None


def parse_wire_file_line(line: str) -> WireFileMetadata | WireMessageRecord:
    """Parse a wire file line into metadata or a message record."""
    metadata = parse_wire_file_metadata(line)
    if metadata is not None:
        return metadata
    return WireMessageRecord.model_validate_json(line)


@dataclass(slots=True)
class WireFile:
    path: Path
    protocol_version: str = WIRE_PROTOCOL_VERSION
    #: Optional epoch lease (see kimo_cli.session_lease). When set, every
    #: append re-checks the lease; a lost lease aborts the write instead of
    #: letting a stale worker fork the transcript.
    lease: SessionLease | None = None

    def __post_init__(self) -> None:
        if self.path.exists():
            version = _load_protocol_version(self.path)
            self.protocol_version = version if version is not None else WIRE_PROTOCOL_LEGACY_VERSION
        else:
            self.protocol_version = WIRE_PROTOCOL_VERSION

    def __str__(self) -> str:
        return str(self.path)

    @property
    def version(self) -> str:
        return self.protocol_version

    def is_empty(self) -> bool:
        if not self.path.exists():
            return True
        try:
            with self.path.open(encoding="utf-8") as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    if parse_wire_file_metadata(line) is not None:
                        continue
                    return False
        except OSError:
            logger.exception("Failed to read wire file {file}:", file=self.path)
            return False
        return True

    async def iter_records(self) -> AsyncIterator[WireMessageRecord]:
        if not self.path.exists():
            return
        try:
            async with aiofiles.open(self.path, encoding="utf-8") as f:
                async for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        parsed = parse_wire_file_line(line)
                    except Exception:
                        logger.exception(
                            "Failed to parse line in wire file {file}:", file=self.path
                        )
                        continue
                    if isinstance(parsed, WireFileMetadata):
                        continue
                    yield parsed
        except Exception:
            logger.exception("Failed to read wire file {file}:", file=self.path)

    async def append_message(self, msg: WireMessage, *, timestamp: float | None = None) -> None:
        record = WireMessageRecord.from_wire_message(
            msg,
            timestamp=time.time() if timestamp is None else timestamp,
        )
        await self.append_record(record)

    async def append_record(self, record: WireMessageRecord) -> None:
        if self.lease is not None:
            self.lease.verify()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # Atomically check-if-exists + write-header + write-record under
        # fcntl.flock. Without this, two concurrent append_record calls both
        # see the file as non-existent (TOCTOU) and both write a header,
        # producing duplicate-header JSONL that breaks downstream parsers.
        # The lock is held across the check and both writes.
        data = _dump_line(record)
        if sys.platform.startswith("win"):
            # Windows has no fcntl; fall back to the old racy path.
            needs_header = not self.path.exists() or self.path.stat().st_size == 0
            async with aiofiles.open(self.path, mode="a", encoding="utf-8") as f:
                if needs_header:
                    metadata = WireFileMetadata(protocol_version=self.protocol_version)
                    await f.write(_dump_line(metadata))
                await f.write(data)
        else:
            # Run synchronous file locking + I/O in a thread to avoid
            # blocking the event loop (fcntl.flock + os.read/write are
            # synchronous syscalls).
            await asyncio.to_thread(self._append_record_locked, data)

    def _append_record_locked(self, data: str) -> None:
        """Synchronous locked append, run via asyncio.to_thread.

        Only reached on non-Windows (see ``append_record``); ``fcntl`` is
        Unix-only, so it is imported here rather than at module level.
        """
        import fcntl

        fd = os.open(self.path, os.O_RDWR | os.O_CREAT, 0o644)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            os.lseek(fd, 0, os.SEEK_SET)
            first = os.read(fd, 1)
            has_header = first == b"{"  # header is a JSON object
            os.lseek(fd, 0, os.SEEK_END)
            if not has_header:
                metadata = WireFileMetadata(protocol_version=self.protocol_version)
                os.write(fd, _dump_line(metadata).encode("utf-8"))
            os.write(fd, data.encode("utf-8"))
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
            os.close(fd)


def _dump_line(model: BaseModel) -> str:
    return json.dumps(model.model_dump(mode="json"), ensure_ascii=False) + "\n"


def _load_protocol_version(path: Path) -> str | None:
    try:
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                metadata = parse_wire_file_metadata(line)
                if metadata is None:
                    return None
                return metadata.protocol_version
    except OSError:
        logger.exception("Failed to read wire file {file}:", file=path)
    return None
