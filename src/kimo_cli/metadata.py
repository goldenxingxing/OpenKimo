from __future__ import annotations

import contextlib
import json
import os
import threading
from collections.abc import Iterator
from hashlib import md5
from pathlib import Path

from kaos import get_current_kaos
from kaos.local import local_kaos
from kaos.path import KaosPath
from pydantic import BaseModel, ConfigDict, Field

from kimo_cli.share import get_share_dir
from kimo_cli.utils.io import atomic_json_write
from kimo_cli.utils.logging import logger

_windows_lock = threading.Lock()


@contextlib.contextmanager
def _metadata_lock(path: Path) -> Iterator[None]:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.with_suffix(path.suffix + ".lock").open("a+b") as lock_file:
        if os.name == "nt":
            import msvcrt

            with _windows_lock:
                lock_file.seek(0, os.SEEK_END)
                if lock_file.tell() == 0:
                    lock_file.write(b"\0")
                    lock_file.flush()
                lock_file.seek(0)
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_LOCK, 1)
                try:
                    yield
                finally:
                    lock_file.seek(0)
                    msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def get_metadata_file() -> Path:
    return get_share_dir() / "kimo.json"


class WorkDirMeta(BaseModel):
    """Metadata for a work directory."""

    path: str
    """The full path of the work directory."""

    kaos: str = local_kaos.name
    """The name of the KAOS where the work directory is located."""

    last_session_id: str | None = None
    """Last session ID of this work directory."""

    @property
    def sessions_dir(self) -> Path:
        """The directory to store sessions for this work directory."""
        if self.kaos == local_kaos.name:
            return Path(self.path) / "session-data"
        return self.legacy_sessions_dir

    @property
    def output_dir(self) -> Path | None:
        """The output directory for local sessions in this work directory."""
        if self.kaos != local_kaos.name:
            return None
        return Path(self.path) / "output"

    @property
    def legacy_sessions_dir(self) -> Path:
        """The pre-OpenKimo-0.2 session location, retained for reads."""
        path_md5 = md5(self.path.encode(encoding="utf-8")).hexdigest()
        dir_basename = path_md5 if self.kaos == local_kaos.name else f"{self.kaos}_{path_md5}"
        return get_share_dir() / "sessions" / dir_basename

    @property
    def readable_sessions_dirs(self) -> tuple[Path, ...]:
        """Session roots in write-first lookup order."""
        current = self.sessions_dir
        legacy = self.legacy_sessions_dir
        return (current,) if current == legacy else (current, legacy)


class Metadata(BaseModel):
    """Kimo metadata structure."""

    model_config = ConfigDict(extra="ignore")

    work_dirs: list[WorkDirMeta] = Field(default_factory=list[WorkDirMeta])
    """Work directory list."""
    last_work_dir: str | None = None
    """Most recently selected local work directory."""

    def get_work_dir_meta(self, path: KaosPath) -> WorkDirMeta | None:
        """Get the metadata for a work directory."""
        for wd in self.work_dirs:
            if wd.path == str(path) and wd.kaos == get_current_kaos().name:
                return wd
        return None

    def new_work_dir_meta(self, path: KaosPath) -> WorkDirMeta:
        """Get or create work directory metadata."""
        existing = self.get_work_dir_meta(path)
        if existing is not None:
            return existing
        wd_meta = WorkDirMeta(path=str(path), kaos=get_current_kaos().name)
        self.work_dirs.append(wd_meta)
        return wd_meta


def load_metadata() -> Metadata:
    metadata_file = get_metadata_file()
    logger.debug("Loading metadata from file: {file}", file=metadata_file)
    if not metadata_file.exists():
        logger.debug("No metadata file found, creating empty metadata")
        return Metadata()
    with open(metadata_file, encoding="utf-8") as f:
        data = json.load(f)
        return Metadata(**data)


def save_metadata(metadata: Metadata):
    metadata_file = get_metadata_file()
    logger.debug("Saving metadata to file: {file}", file=metadata_file)
    with _metadata_lock(metadata_file):
        try:
            on_disk = load_metadata()
        except FileNotFoundError:
            on_disk = Metadata()
        merged = {(item.path, item.kaos): item for item in on_disk.work_dirs}
        merged.update({(item.path, item.kaos): item for item in metadata.work_dirs})
        metadata.work_dirs = list(merged.values())
        atomic_json_write(metadata.model_dump(), metadata_file)
