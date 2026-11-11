"""Regression tests for the background worker's shutdown path."""

from __future__ import annotations

import asyncio

import pytest

from kimo_cli.background.worker import _cancel_helper_tasks


@pytest.mark.asyncio
async def test_helper_task_failure_is_swallowed() -> None:
    """A helper that already failed must not take the worker down with it."""

    async def _boom() -> None:
        raise RuntimeError("helper died")

    task = asyncio.create_task(_boom())
    await asyncio.sleep(0)  # let it fail

    await _cancel_helper_tasks((task, None))  # must not raise


@pytest.mark.asyncio
async def test_cleanup_does_not_replace_the_original_exception() -> None:
    """The error the worker was handling has to survive the cleanup."""

    async def _boom() -> None:
        raise RuntimeError("helper died")

    task = asyncio.create_task(_boom())
    await asyncio.sleep(0)

    with pytest.raises(ValueError, match="original"):
        try:
            raise ValueError("original")
        finally:
            await _cancel_helper_tasks((task,))


@pytest.mark.asyncio
async def test_cleanup_cancels_a_running_helper() -> None:
    started = asyncio.Event()

    async def _forever() -> None:
        started.set()
        await asyncio.sleep(60)

    task = asyncio.create_task(_forever())
    await started.wait()

    await _cancel_helper_tasks((task,))

    assert task.cancelled() or task.done()
