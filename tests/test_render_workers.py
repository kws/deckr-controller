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

    monkeypatch.setattr(workers._render_process, "run", blocked)
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


@pytest.mark.asyncio
async def test_page_churn_cancels_workers_during_startup_without_orphans(monkeypatch):
    from deckr.controller import _render_process

    await _render_process.close_idle()
    original = _render_process.Worker
    children = []

    def starting():
        worker = original()
        children.append(worker.process.pid)
        scope.cancel()
        return worker

    monkeypatch.setattr(_render_process, "Worker", starting)
    for _ in range(8):
        with anyio.CancelScope() as scope:
            await workers.run_process(str, "obsolete page")
        assert scope.cancel_called
    for pid in children:
        with pytest.raises(ProcessLookupError):
            os.kill(pid, 0)
    assert not _render_process._pool()


@pytest.mark.asyncio
async def test_completed_render_reuses_worker_and_shutdown_reaps_it():
    from deckr.controller import _render_process

    first = await workers.run_process(os.getpid)
    assert await workers.run_process(os.getpid) == first
    await _render_process.close_idle()
    with pytest.raises(ProcessLookupError):
        os.kill(first, 0)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("probe", "message"),
    [
        ("fail_http_in_process", "HTTPStatusError.*503"),
        ("unpicklable_result_in_process", "lambda"),
    ],
)
async def test_untransportable_result_does_not_destroy_warm_worker(probe, message):
    import test_support.render_workers as probes
    from deckr.controller import _render_process

    await _render_process.close_idle()
    try:
        pid = await workers.run_process(os.getpid)
        for _ in range(3):
            with pytest.raises(RuntimeError, match=message):
                await workers.run_process(getattr(probes, probe))
            assert await workers.run_process(os.getpid) == pid
    finally:
        await _render_process.close_idle()


@pytest.mark.asyncio
async def test_workload_waiters_do_not_reserve_idle_process_capacity(monkeypatch):
    slots = threading.BoundedSemaphore(2)
    monkeypatch.setattr(workers, "_PROCESS_SLOTS", slots)
    limiter = anyio.CapacityLimiter(1)
    entered, release, composition = anyio.Event(), anyio.Event(), anyio.Event()

    async def execute(name):
        if name == "graph":
            entered.set()
            await release.wait()
        else:
            composition.set()

    monkeypatch.setattr(workers._render_process, "run", execute)

    async def graph():
        await workers.run_process("graph", limiter=limiter)

    async with anyio.create_task_group() as group:
        group.start_soon(graph)
        await entered.wait()
        group.start_soon(graph)
        with anyio.fail_after(1):
            while not limiter.statistics().tasks_waiting:
                await anyio.sleep(0)
            group.start_soon(workers.run_process, "composition")
            await composition.wait()
        release.set()


@pytest.mark.asyncio
async def test_superseded_graph_waiter_never_starts(monkeypatch):
    import time
    from types import SimpleNamespace

    from deckr.controller._bindings._render_execution import RenderExecution
    from deckr.controller._bindings._render_policy import DEFAULT_LIMITS
    from deckr.controller._render_graph import INVARIANT_FORMAT

    slots = threading.BoundedSemaphore(1)
    slots.acquire()
    monkeypatch.setattr(workers, "_PROCESS_SLOTS", slots)
    execute_process = AsyncMock(return_value=b"unused")
    monkeypatch.setattr(workers._render_process, "run", execute_process)
    job = SimpleNamespace(
        lease=SimpleNamespace(context=SimpleNamespace(presentation=object())),
        body=SimpleNamespace(
            operation="set",
            content=SimpleNamespace(format=INVARIANT_FORMAT, data={}),
        ),
        target=SimpleNamespace(width=72, height=72, limits=DEFAULT_LIMITS),
        direct=False,
        timings={},
        done=False,
        deadline=time.monotonic() + 2,
        scope=anyio.CancelScope(),
    )
    monkeypatch.setattr(
        "deckr.controller._bindings._render_execution.graph_resource_urls",
        lambda *args: (),
    )
    execution = RenderExecution(
        ports=lambda: SimpleNamespace(
            dispatcher=object(),
            backend=SimpleNamespace(
                resources=SimpleNamespace(acquire=AsyncMock(return_value={}))
            ),
        ),
        authorize=AsyncMock(),
        finish=AsyncMock(),
        hardware_rejection=AsyncMock(),
    )
    done = anyio.Event()

    async def run():
        with job.scope:
            await execution.execute(job)
        done.set()

    async with anyio.create_task_group() as group:
        group.start_soon(run)
        await anyio.sleep(0.02)
        job.done = True
        job.scope.cancel()
        with anyio.fail_after(0.5):
            await done.wait()
        slots.release()
    execute_process.assert_not_awaited()
