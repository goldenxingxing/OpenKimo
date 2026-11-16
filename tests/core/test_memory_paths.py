from __future__ import annotations

import pytest

from kimo_cli.memory.paths import ANONYMOUS_USER_SENTINEL, resolve_owner_id


@pytest.mark.parametrize("owner_id", [".", "..", "   ", "../other", "a/b", "a\\b"])
def test_resolve_owner_id_rejects_non_plain_directory_names(owner_id: str) -> None:
    with pytest.raises(ValueError, match="plain directory name"):
        resolve_owner_id(owner_id)


def test_resolve_owner_id_accepts_plain_name_and_anonymous_sentinel() -> None:
    assert resolve_owner_id("user-123") == "user-123"
    assert resolve_owner_id(None) == ANONYMOUS_USER_SENTINEL
    assert resolve_owner_id("") == ANONYMOUS_USER_SENTINEL
