"""Regression tests for the evolution editor."""

import pytest

from kimo_cli.config import LLMModel, get_default_config
from kimo_cli.evolution import editor


@pytest.mark.asyncio
@pytest.mark.parametrize("limit", [0, -1])
async def test_propose_rejects_nonpositive_limit(limit: int):
    with pytest.raises(ValueError, match="limit must be greater than 0"):
        await editor.propose(object(), limit=limit)


def test_editor_llm_tolerates_missing_provider(monkeypatch: pytest.MonkeyPatch):
    from kimo_cli import llm
    from kimo_cli.auth import oauth

    config = get_default_config()
    config.default_model = "broken"
    config.models = {"broken": LLMModel(provider="missing", model="model", max_context_size=1000)}
    monkeypatch.setattr(oauth, "OAuthManager", lambda _config: object())
    monkeypatch.setattr(llm, "augment_provider_with_env_vars", lambda _provider, _model: {})
    sentinel = object()
    captured = {}

    def fake_create_llm(provider, model, **_kwargs):
        captured["provider"] = provider
        captured["model"] = model
        return sentinel

    monkeypatch.setattr(llm, "create_llm", fake_create_llm)

    assert editor._editor_llm(config, None) is sentinel
    assert captured["provider"].type == "openai_legacy"
    assert captured["model"].model == ""


def test_editor_llm_rejects_an_unconfigured_model(monkeypatch: pytest.MonkeyPatch):
    """An unknown --model used to be silently replaced by the env-var model."""
    config = get_default_config()
    config.models = {"known": LLMModel(provider="p", model="m", max_context_size=1000)}

    with pytest.raises(RuntimeError, match="not configured"):
        editor._editor_llm(config, "unknown-model")


def test_validate_editor_payload_rejects_non_string_system_append():
    with pytest.raises(ValueError, match="non-string system_append"):
        editor._validate_editor_payload(["a", "b"], None)


def test_validate_editor_payload_rejects_non_mapping_skills():
    with pytest.raises(ValueError, match="non-mapping skills"):
        editor._validate_editor_payload("append", ["a"])


def test_validate_editor_payload_rejects_non_string_skill_content():
    with pytest.raises(ValueError, match="non-string content"):
        editor._validate_editor_payload(None, {"good-name": 0})


def test_validate_editor_payload_rejects_too_many_skills():
    with pytest.raises(ValueError, match="max 2 is allowed"):
        editor._validate_editor_payload(None, {"a": "x", "b": "y", "c": "z"})


def test_validate_editor_payload_accepts_a_well_formed_payload():
    system_append, skills = editor._validate_editor_payload("  text  ", {"a": "body"})

    assert system_append == "  text  "
    assert skills == {"a": "body"}


def test_validate_editor_payload_normalises_none():
    assert editor._validate_editor_payload(None, None) == (None, None)


def test_evidence_records_uses_all_session_roots(monkeypatch: pytest.MonkeyPatch):
    """propose evidence must aggregate share + local work dirs, not share only."""
    from kimo_cli.evolution import library as lib

    sentinel = object()
    captured = {}

    def fake_all():
        captured["called"] = True
        return sentinel

    monkeypatch.setattr(lib, "iter_all_session_records", fake_all)
    assert editor._evidence_records() is sentinel
    assert captured["called"] is True


def test_evidence_records_never_uses_share_only_root(monkeypatch: pytest.MonkeyPatch):
    """Regression: the share-only root used to hide workspace evidence."""
    from kimo_cli.evolution import library as lib

    def boom(*_args, **_kwargs):
        raise AssertionError("share-only iter_session_records must not be used")

    monkeypatch.setattr(lib, "iter_session_records", boom)
    monkeypatch.setattr(lib, "iter_all_session_records", lambda: [])
    assert editor._evidence_records() == []
