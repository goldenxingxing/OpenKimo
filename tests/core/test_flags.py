"""Tests for the experimental feature-flag gate (kimo_cli.flags)."""

from __future__ import annotations

import os

import pytest

from kimo_cli import flags


@pytest.fixture(autouse=True)
def _clean_flags(monkeypatch: pytest.MonkeyPatch):
    """Isolate each test: no inherited env, no real config file, empty overrides."""
    for key in list(os.environ):
        if key.startswith(flags.ENV_PREFIX) or key == flags.MASTER_ENV:
            monkeypatch.delenv(key, raising=False)
    flags.set_config_overrides({})
    yield
    flags.set_config_overrides({})
    flags.reset_cache()


def test_unknown_flag_is_disabled():
    assert flags.enabled("does_not_exist") is False
    assert flags.explain("does_not_exist") is None


def test_default_off():
    flags.register_flag("gate_default_off", default=False)
    state = flags.explain("gate_default_off")
    assert state is not None
    assert state.enabled is False
    assert state.source == "default"


def test_default_on():
    flags.register_flag("gate_default_on", default=True)
    state = flags.explain("gate_default_on")
    assert state is not None
    assert state.enabled is True
    assert state.source == "default"


def test_env_enables(monkeypatch: pytest.MonkeyPatch):
    flags.register_flag("gate_env", default=False)
    monkeypatch.setenv("KIMO_CODE_EXPERIMENTAL_GATE_ENV", "1")
    state = flags.explain("gate_env")
    assert state is not None
    assert state.enabled is True
    assert state.source == "env"


def test_env_disables_despite_default_on(monkeypatch: pytest.MonkeyPatch):
    flags.register_flag("gate_env_off", default=True)
    monkeypatch.setenv("KIMO_CODE_EXPERIMENTAL_GATE_ENV_OFF", "0")
    state = flags.explain("gate_env_off")
    assert state is not None
    assert state.enabled is False
    assert state.source == "env"


def test_config_overrides_default():
    flags.register_flag("gate_config", default=False)
    flags.set_config_overrides({"gate_config": True})
    state = flags.explain("gate_config")
    assert state is not None
    assert state.enabled is True
    assert state.source == "config"


def test_env_beats_config(monkeypatch: pytest.MonkeyPatch):
    flags.register_flag("gate_env_config", default=False)
    flags.set_config_overrides({"gate_env_config": True})
    monkeypatch.setenv("KIMO_CODE_EXPERIMENTAL_GATE_ENV_CONFIG", "0")
    state = flags.explain("gate_env_config")
    assert state is not None
    assert state.enabled is False
    assert state.source == "env"


def test_master_env_enables_all(monkeypatch: pytest.MonkeyPatch):
    flags.register_flag("gate_master", default=False)
    monkeypatch.setenv(flags.MASTER_ENV, "1")
    state = flags.explain("gate_master")
    assert state is not None
    assert state.enabled is True
    assert state.source == "master-env"


def test_per_flag_env_beats_master(monkeypatch: pytest.MonkeyPatch):
    flags.register_flag("gate_env_master", default=False)
    monkeypatch.setenv(flags.MASTER_ENV, "1")
    monkeypatch.setenv("KIMO_CODE_EXPERIMENTAL_GATE_ENV_MASTER", "0")
    state = flags.explain("gate_env_master")
    assert state is not None
    assert state.enabled is False
    assert state.source == "env"


def test_config_beats_master(monkeypatch: pytest.MonkeyPatch):
    flags.register_flag("gate_config_master", default=False)
    flags.set_config_overrides({"gate_config_master": False})
    monkeypatch.setenv(flags.MASTER_ENV, "1")
    state = flags.explain("gate_config_master")
    assert state is not None
    assert state.enabled is False
    assert state.source == "config"


def test_env_var_name_is_uppercased():
    definition = flags.register_flag("image_preview")
    assert definition.env == "KIMO_CODE_EXPERIMENTAL_IMAGE_PREVIEW"


def test_snapshot_covers_registered_flags():
    flags.register_flag("gate_snap_a", default=False)
    flags.register_flag("gate_snap_b", default=True)
    snap = flags.snapshot()
    assert snap["gate_snap_a"] is False
    assert snap["gate_snap_b"] is True


def test_reregister_last_wins():
    flags.register_flag("gate_idem", default=False)
    flags.register_flag("gate_idem", default=True)
    assert flags.explain("gate_idem").default is True
    # Only one entry for the id.
    assert len([d for d in flags.registry.list() if d.id == "gate_idem"]) == 1
