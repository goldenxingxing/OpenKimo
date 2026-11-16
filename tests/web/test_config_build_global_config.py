"""``_build_global_config`` treats .env LLM_PROVIDERS as the single source of truth.

The admin panel reads config.toml; Settings removes providers from .env. If a
provider/model was deleted in Settings but its ``[models.X]``/``[providers.X]``
block survives in config.toml (a config migration, a non-Settings writer, or a
failed prune), the old add-only merge resurrected it on every request. These
tests pin the fix: names absent from the env list are neither served nor
written back.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import SecretStr

from kimo_cli.config import LLMModel, LLMProvider
from kimo_cli.web.api import config as config_module


def _model(name: str) -> LLMModel:
    return LLMModel(provider=name, model=name, max_context_size=100000)


def _provider(name: str) -> LLMProvider:
    return LLMProvider(type="openai_legacy", base_url="", api_key=SecretStr("k"))


def _cfg(models: dict[str, LLMModel], providers: dict[str, LLMProvider]):
    return SimpleNamespace(
        providers=providers,
        models=models,
        default_model=next(iter(models)),
        default_thinking=True,
        loop_control=SimpleNamespace(compaction_trigger_ratio=0.7),
    )


def test_deleted_models_are_not_served_nor_written_back(monkeypatch: pytest.MonkeyPatch) -> None:
    """'gone' exists only in config.toml (deleted from .env) -> filtered out and
    pruned on write-back; 'new' comes from env -> added and served."""
    env_list = [
        {"name": "ds", "type": "openai_legacy", "model": "ds", "api_key": "k",
         "base_url": "", "max_context_size": 100000, "capabilities": []},
        {"name": "new", "type": "openai_legacy", "model": "new", "api_key": "k",
         "base_url": "", "max_context_size": 100000, "capabilities": []},
    ]
    monkeypatch.setattr(config_module, "parse_llm_providers_env", lambda: env_list)
    cfg = _cfg(
        models={"ds": _model("ds"), "gone": _model("gone")},
        providers={"ds": _provider("ds"), "gone": _provider("gone")},
    )
    monkeypatch.setattr(config_module, "load_config", lambda: cfg)
    saved: list = []
    monkeypatch.setattr(config_module, "save_config", lambda c: saved.append(c))

    gc = config_module._build_global_config()

    assert {m.name for m in gc.models} == {"ds", "new"}
    assert "gone" not in gc.models  # stale not served
    # write-back pruned the stale entry and kept the env ones
    assert saved, "env_models_added should trigger save_config"
    assert "gone" not in saved[0].models
    assert "gone" not in saved[0].providers
    assert {"ds", "new"} <= set(saved[0].models)


def test_no_llm_providers_keeps_plain_config_mode(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without LLM_PROVIDERS (legacy/plain-config mode) nothing is filtered."""
    monkeypatch.setattr(config_module, "parse_llm_providers_env", lambda: [])
    for k in ("OPENAI_API_KEY", "OPENAI_MODEL_NAME"):
        monkeypatch.delenv(k, raising=False)
    cfg = _cfg(
        models={"a": _model("a"), "b": _model("b")},
        providers={"a": _provider("a"), "b": _provider("b")},
    )
    monkeypatch.setattr(config_module, "load_config", lambda: cfg)

    gc = config_module._build_global_config()

    assert {m.name for m in gc.models} == {"a", "b"}
