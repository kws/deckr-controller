"""Render execution has bounded capacity and cancellable ownership."""

import os
import threading
from unittest.mock import AsyncMock

import anyio
import pytest

from deckr.controller import _render_workers as workers
from test_support.render_workers import block_in_process


@pytest.mark.asyncio
async def test_process_capacity_wait_is_cancellable_and_does_not_release_unowned_slot(
    monkeypatch,
):
    monkeypatch.setattr(workers, "_PROCESS_SLOTS", threading.BoundedSemaphore(1))
    entered, release = anyio.Event(), anyio.Event()
    calls = []

    async def blocked(*args, **kwargs):
        calls.append(args)
        entered.set()
        await release.wait()

    monkeypatch.setattr(anyio.to_process, "run_sync", blocked)
    async with anyio.create_task_group() as tg:
        tg.start_soon(workers.run_process, str)
        await entered.wait()
        with anyio.move_on_after(0.03) as scope:
            await workers.run_process(str)
        assert scope.cancel_called
        assert len(calls) == 1
        assert not workers._PROCESS_SLOTS.acquire(blocking=False)
        release.set()
    await workers.run_process(str)
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_abandoned_thread_keeps_its_slot_until_it_exits():
    slots = threading.BoundedSemaphore(1)
    entered, release, exited = threading.Event(), threading.Event(), threading.Event()
    done = anyio.Event()
    scope = anyio.CancelScope()

    def blocking():
        entered.set()
        release.wait(2)
        exited.set()

    async def render():
        with scope:
            await workers.run_thread(blocking, slots=slots)
        done.set()

    async with anyio.create_task_group() as tg:
        try:
            tg.start_soon(render)
            with anyio.fail_after(1):
                while not entered.is_set():
                    await anyio.sleep(0.001)
                scope.cancel()
                await done.wait()
            with anyio.move_on_after(0.03) as waiting:
                await workers.run_thread(str, slots=slots)
            assert waiting.cancel_called
            assert not slots.acquire(blocking=False)
        finally:
            release.set()
        with anyio.fail_after(1):
            while not exited.is_set():
                await anyio.sleep(0.001)
    assert await workers.run_thread(str, "recovered", slots=slots) == "recovered"


@pytest.mark.asyncio
async def test_cancel_terminates_a_running_render_process(tmp_path):
    started = tmp_path / "process"
    done, scope = anyio.Event(), anyio.CancelScope()

    async def render():
        with scope:
            await workers.run_process(block_in_process, str(started))
        done.set()

    async with anyio.create_task_group() as tg:
        try:
            tg.start_soon(render)
            with anyio.fail_after(5):
                while not started.exists():
                    await anyio.sleep(0.01)
            pid = int(started.read_text())
            with anyio.fail_after(1):
                scope.cancel()
                await done.wait()
            with pytest.raises(ProcessLookupError):
                os.kill(pid, 0)
        finally:
            scope.cancel()


@pytest.mark.asyncio
async def test_controller_cleanup_attempts_every_owned_resource():
    from types import SimpleNamespace

    from deckr.controller._controller_service import ControllerService

    calls = []

    def resource(name, fail=False):
        async def close():
            calls.append(name)
            if fail:
                raise RuntimeError(name)

        return SimpleNamespace(aclose=close)

    service = object.__new__(ControllerService)
    service._stopping = None
    service._hardware = resource("hardware", True)
    service._controller_contexts = SimpleNamespace(
        values=AsyncMock(return_value=[resource("session", True)]), clear=AsyncMock()
    )
    service._action_service = resource("actions", True)
    service._render_backend = resource("renderer")
    with pytest.raises(ExceptionGroup) as error:
        await service.stop()
    assert calls == ["hardware", "session", "actions", "renderer"]
    service._controller_contexts.clear.assert_awaited_once()
    assert len(error.value.exceptions) == 3
