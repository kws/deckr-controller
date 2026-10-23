"""Cancellation at worker handoff must not strand threads or cache handles."""

import contextvars
import queue
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor

import anyio
import pytest
from anyio._backends._asyncio import WorkerThread

from deckr.controller import _render_workers as workers


@pytest.mark.asyncio
async def test_cancel_after_dequeue_reuses_worker_and_its_cache_connection(
    monkeypatch, tmp_path
):
    dequeued, release = threading.Event(), threading.Event()
    pause = threading.Event()

    class GatedQueue(queue.SimpleQueue):
        def get(self, *args, **kwargs):
            item = super().get(*args, **kwargs)
            if item is not None and pause.is_set():
                dequeued.set()
                assert release.wait(5), "test did not release worker handoff"
            return item

    # Instrument the former AnyIO backend too, so this test reproduces the
    # original leak instead of merely asserting the replacement pool's shape.
    original_run = WorkerThread.run

    def gated_anyio_run(self):
        original_get = self.queue.get

        def get(*args, **kwargs):
            item = original_get(*args, **kwargs)
            if item is not None and pause.is_set():
                dequeued.set()
                assert release.wait(5), "test did not release worker handoff"
            return item

        self.queue.get = get
        original_run(self)

    monkeypatch.setattr(WorkerThread, "run", gated_anyio_run)
    pool = ThreadPoolExecutor(max_workers=1)
    pool._work_queue = GatedQueue()
    monkeypatch.setattr(workers, "_THREAD_POOL", pool, raising=False)
    slots = threading.BoundedSemaphore(1)
    local = threading.local()
    connections = []
    executed = []

    def cache_access():
        if not hasattr(local, "connection"):
            local.connection = sqlite3.connect(tmp_path / "cache.sqlite3")
            connections.append(local.connection)
        assert local.connection.execute("SELECT 1").fetchone() == (1,)
        return threading.get_ident()

    try:
        worker = await workers.run_thread(cache_access, slots=slots)
        for _ in range(20):
            dequeued.clear()
            release.clear()
            pause.set()
            done = anyio.Event()
            scope = anyio.CancelScope()

            async def job(scope, done):
                with scope:
                    await workers.run_thread(executed.append, "obsolete", slots=slots)
                done.set()

            async with anyio.create_task_group() as group:
                group.start_soon(job, scope, done)
                with anyio.fail_after(2):
                    while not dequeued.is_set():
                        await anyio.sleep(0.001)
                    scope.cancel()
                    await done.wait()
                pause.clear()
                release.set()
            assert await workers.run_thread(cache_access, slots=slots) == worker
        assert executed == []
        assert len(connections) == 1
        assert len(pool._threads) == 1
        await workers.run_thread(connections[0].close, slots=slots)
    finally:
        pause.clear()
        release.set()
        pool.shutdown(wait=True, cancel_futures=True)


@pytest.mark.asyncio
async def test_thread_job_preserves_context_and_releases_capacity_on_error():
    variable = contextvars.ContextVar("render_context", default="missing")
    token = variable.set("binding")
    slots = threading.BoundedSemaphore(1)
    try:
        assert await workers.run_thread(variable.get, slots=slots) == "binding"

        def fail():
            raise ValueError("render failed")

        with pytest.raises(ValueError, match="render failed"):
            await workers.run_thread(fail, slots=slots)
        assert await workers.run_thread(str, "next frame", slots=slots) == "next frame"
    finally:
        variable.reset(token)
