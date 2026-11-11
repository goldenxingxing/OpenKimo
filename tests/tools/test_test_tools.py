"""Tests for the built-in tools used by tool-system tests."""

import pytest

from kimo_cli.tools.test import Compare, CompareParams


@pytest.mark.parametrize(("a", "b"), [(float("nan"), 1.0), (1.0, float("nan"))])
async def test_compare_rejects_nan(a: float, b: float) -> None:
    result = await Compare()(CompareParams(a=a, b=b))

    assert result.is_error
    assert "NaN" in result.message


async def test_compare_uses_exact_float_ordering() -> None:
    result = await Compare()(CompareParams(a=0.1 + 0.2, b=0.3))

    assert not result.is_error
    assert result.output == "greater"
