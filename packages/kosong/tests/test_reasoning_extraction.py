"""Unit tests for the multi-field reasoning extraction shared by chat providers."""

from types import SimpleNamespace

from kosong.contrib.chat_provider.common import DEFAULT_REASONING_KEYS, extract_reasoning


class TestExtractReasoning:
    def test_returns_first_non_empty_from_default_keys(self):
        obj = SimpleNamespace(reasoning_content="hello", reasoning="world")
        assert extract_reasoning(obj) == "hello"

    def test_falls_back_to_reasoning_when_reasoning_content_absent(self):
        obj = SimpleNamespace(reasoning="only reasoning field")
        assert extract_reasoning(obj) == "only reasoning field"

    def test_falls_back_to_reasoning_when_reasoning_content_empty(self):
        obj = SimpleNamespace(reasoning_content="", reasoning="sense-nova style")
        assert extract_reasoning(obj) == "sense-nova style"

    def test_returns_none_when_no_key_matches(self):
        obj = SimpleNamespace(content="no reasoning here")
        assert extract_reasoning(obj) is None

    def test_returns_none_when_obj_is_none(self):
        assert extract_reasoning(None) is None

    def test_returns_none_when_obj_has_no_attributes(self):
        assert extract_reasoning(object()) is None

    def test_honours_custom_keys_order(self):
        obj = SimpleNamespace(foo="aaa", bar="bbb")
        assert extract_reasoning(obj, ("bar", "foo")) == "bbb"

    def test_default_keys_cover_deepseek_and_sensenova(self):
        assert "reasoning_content" in DEFAULT_REASONING_KEYS
        assert "reasoning" in DEFAULT_REASONING_KEYS
        # reasoning_content must come first (de-facto standard round-trip target)
        assert DEFAULT_REASONING_KEYS[0] == "reasoning_content"
