"""Config file I/O hardening: atomic saves, decode errors, migration failures."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from kimo_cli import config as config_module
from kimo_cli.config import Config, get_default_config, load_config, save_config
from kimo_cli.exception import ConfigError


def test_save_config_keeps_previous_file_when_the_write_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A failed write must leave the old document intact, never a truncated one."""
    target = tmp_path / "config.toml"
    save_config(get_default_config(), target)
    before = target.read_text(encoding="utf-8")

    modified = get_default_config()
    modified.default_model = "some-other-model"

    def _disk_full(_fd: int) -> None:
        raise OSError("No space left on device")

    monkeypatch.setattr(config_module.os, "fsync", _disk_full)

    with pytest.raises(OSError, match="No space left"):
        save_config(modified, target)

    assert target.read_text(encoding="utf-8") == before
    # No temp file may be left behind next to the config.
    assert [p.name for p in tmp_path.iterdir()] == ["config.toml"]


def test_save_config_replaces_atomically(tmp_path: Path) -> None:
    target = tmp_path / "config.json"
    save_config(get_default_config(), target)
    first = target.read_text(encoding="utf-8")

    modified = get_default_config()
    modified.default_model = "another-model"
    save_config(modified, target)

    assert target.read_text(encoding="utf-8") != first
    assert json.loads(target.read_text(encoding="utf-8"))["default_model"] == "another-model"


def test_load_config_reports_non_utf8_file_as_config_error(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    target.write_bytes(b"\xff\xfe\x00not-utf8")

    with pytest.raises(ConfigError, match="not valid UTF-8"):
        load_config(target)


def test_migration_survives_a_failed_backup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A backup failure must not break the first start with a migrated TOML."""
    monkeypatch.setenv("KIMO_SHARE_DIR", str(tmp_path))
    legacy = tmp_path / "config.json"
    legacy.write_text(json.dumps(get_default_config().model_dump(mode="json")), encoding="utf-8")

    real_replace = Path.replace

    def _replace(self: Path, target: Path) -> Path:
        if self.name == "config.json":
            raise OSError("backup blocked")
        return real_replace(self, target)

    monkeypatch.setattr(Path, "replace", _replace)

    load_config()  # must not raise

    assert (tmp_path / "config.toml").exists()
    # Legacy file untouched, so nothing is silently destroyed.
    assert legacy.exists()


def test_config_roundtrip_preserves_values(tmp_path: Path) -> None:
    target = tmp_path / "config.toml"
    original = get_default_config()
    save_config(original, target)

    reloaded: Config = load_config(target)
    assert reloaded.model_dump(mode="json", exclude_none=True) == original.model_dump(
        mode="json", exclude_none=True
    )


def test_save_config_is_readable_after_partial_temp_cleanup(tmp_path: Path) -> None:
    """``mkstemp`` files must not accumulate when many saves happen."""
    target = tmp_path / "config.toml"
    for _ in range(3):
        save_config(get_default_config(), target)

    assert os.listdir(tmp_path) == ["config.toml"]
