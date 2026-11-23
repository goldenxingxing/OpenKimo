from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from kosong import StepResult
from kosong.message import ImageURLPart, Message

import kimo_cli.soul.kimosoul as kimosoul_module
from kimo_cli.llm import LLM
from kimo_cli.soul.agent import Runtime
from kimo_cli.soul.compaction import CompactionResult
from kimo_cli.soul.toolset import KimoToolset
from kimo_cli.wire.types import TextPart
from tests.core.test_compaction_failure import _CompactionFails
from tests.core.test_kimosoul_repeat import _make_soul


def _soul(runtime: Runtime, tmp_path: Path):
    llm = LLM(
        chat_provider=_CompactionFails(),
        max_context_size=100_000,
        capabilities=set(),
    )
    return _make_soul(runtime, llm, KimoToolset(), tmp_path)


def _step_result() -> StepResult:
    return StepResult(
        id="step-1",
        message=Message(role="assistant", content=[TextPart(text="done")]),
        usage=None,
        tool_calls=[],
        _tool_result_futures={},
    )


@pytest.mark.asyncio
async def test_step_cancellation_waits_for_context_growth(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    soul = _soul(runtime, tmp_path)
    soul._current_step_no = 0
    started = asyncio.Event()
    release = asyncio.Event()
    finished = False

    async def _blocked_grow(*_args) -> None:
        nonlocal finished
        started.set()
        await release.wait()
        finished = True

    monkeypatch.setattr(soul, "_grow_context", _blocked_grow)

    async def _fake_step(*_args, **_kwargs) -> StepResult:
        return _step_result()

    monkeypatch.setattr(kimosoul_module.kosong, "step", _fake_step)
    monkeypatch.setattr(kimosoul_module, "wire_send", lambda _msg: None)

    step_task = asyncio.create_task(soul._step())
    await asyncio.wait_for(started.wait(), timeout=5)
    step_task.cancel()
    await asyncio.sleep(0)
    assert not step_task.done()
    assert not finished

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(step_task, timeout=5)
    assert finished


@pytest.mark.asyncio
async def test_step_cancellation_propagates_context_growth_error(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    soul = _soul(runtime, tmp_path)
    soul._current_step_no = 0
    started = asyncio.Event()
    release = asyncio.Event()

    async def _failing_grow(*_args) -> None:
        started.set()
        await release.wait()
        raise RuntimeError("context write failed")

    monkeypatch.setattr(soul, "_grow_context", _failing_grow)

    async def _fake_step(*_args, **_kwargs) -> StepResult:
        return _step_result()

    monkeypatch.setattr(kimosoul_module.kosong, "step", _fake_step)
    monkeypatch.setattr(kimosoul_module, "wire_send", lambda _msg: None)

    step_task = asyncio.create_task(soul._step())
    await asyncio.wait_for(started.wait(), timeout=5)
    step_task.cancel()
    release.set()
    with pytest.raises(RuntimeError, match="context write failed"):
        await asyncio.wait_for(step_task, timeout=5)


@pytest.mark.asyncio
@pytest.mark.parametrize("stage", ["prompt", "checkpoint", "append", "usage"])
async def test_context_rebuild_cancellation_after_clear_is_not_partial(
    stage: str,
    runtime: Runtime,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    soul = _soul(runtime, tmp_path)
    await soul._context.append_message(Message(role="user", content="old history"))
    replacement = Message(role="user", content="complete rebuilt history")
    entered = asyncio.Event()
    release = asyncio.Event()

    async def _noop_checkpoint() -> None:
        return None

    monkeypatch.setattr(soul, "_checkpoint", _noop_checkpoint)

    def _block(method):
        async def _wrapped(*args, **kwargs):
            entered.set()
            await release.wait()
            return await method(*args, **kwargs)

        return _wrapped

    if stage == "prompt":
        monkeypatch.setattr(
            soul._context,
            "write_system_prompt",
            _block(soul._context.write_system_prompt),
        )
    elif stage == "checkpoint":
        monkeypatch.setattr(soul, "_checkpoint", _block(_noop_checkpoint))
    elif stage == "append":
        monkeypatch.setattr(soul._context, "append_message", _block(soul._context.append_message))
    else:
        monkeypatch.setattr(
            soul._context,
            "update_token_count",
            _block(soul._context.update_token_count),
        )

    replace_task = asyncio.create_task(soul._replace_context_history([replacement]))
    await asyncio.wait_for(entered.wait(), timeout=5)
    assert all(message.content != "old history" for message in soul._context.history)
    replace_task.cancel()
    await asyncio.sleep(0)
    assert not replace_task.done()

    release.set()
    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(replace_task, timeout=5)

    assert soul._context.history[-1] == replacement
    assert soul._context.system_prompt == soul._agent.system_prompt
    restored = type(soul._context)(soul._context.file_backend)
    assert await restored.restore()
    assert restored.history[-1] == replacement


class _StubCompaction:
    def __init__(self) -> None:
        self.request_history: list[Message] = []

    async def compact(self, history, _llm, *, custom_instruction=None) -> CompactionResult:
        self.request_history = list(history)
        return CompactionResult(
            messages=[Message(role="user", content=[TextPart(text="summary")])],
            usage=None,
        )


@pytest.mark.asyncio
async def test_compaction_archive_keeps_images_stripped_only_from_request(
    runtime: Runtime, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    soul = _soul(runtime, tmp_path)
    image_url = "data:image/png;base64,ORIGINAL"
    image = ImageURLPart(image_url=ImageURLPart.ImageURL(url=image_url))
    await soul._context.append_message(
        [
            Message(role="user", content=[TextPart(text="old"), image]),
            Message(role="user", content=[TextPart(text="latest")]),
        ]
    )
    soul._loop_control.max_request_bytes = 1
    compaction = _StubCompaction()
    soul._compaction = compaction  # type: ignore[assignment]
    monkeypatch.setattr(kimosoul_module, "wire_send", lambda _msg: None)

    async def _noop_checkpoint() -> None:
        return None

    monkeypatch.setattr(soul, "_checkpoint", _noop_checkpoint)
    await asyncio.wait_for(soul.compact_context(manual=True), timeout=5)

    assert not any(
        isinstance(part, ImageURLPart)
        for message in compaction.request_history
        for part in message.content
    )
    archives = list((runtime.session.dir / "archive").glob("compaction-*.jsonl"))
    assert len(archives) == 1
    assert image_url in archives[0].read_text(encoding="utf-8")
