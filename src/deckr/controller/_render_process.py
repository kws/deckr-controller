"""Owned render workers with cancellation-safe startup and bounded reuse."""

from __future__ import annotations

import multiprocessing
import pickle
from collections import deque

import anyio
from anyio.lowlevel import RunVar


def _serialize_result(ok, value):
    try:
        payload = pickle.dumps((ok, value))
        if not ok:
            # Some exceptions can be pickled but cannot be reconstructed (for
            # example HTTPStatusError requires keyword-only request/response).
            # Check inside the worker so a failed download does not destroy a
            # healthy, warmed renderer when the parent receives its error.
            pickle.loads(payload)
        return payload
    except Exception as exc:
        error = value if not ok else exc
        return pickle.dumps(
            (False, RuntimeError(f"{type(error).__name__}: {error}"))
        )


# Only trusted Python callables and their already validated render inputs cross
# this private pipe. No network data is unpickled here.
def _worker(pipe):
    try:
        while True:
            function, args = pickle.loads(pipe.recv_bytes())
            try:
                result = (True, function(*args))
            except BaseException as exc:
                result = (False, exc)
            pipe.send_bytes(_serialize_result(*result))
    except (EOFError, BrokenPipeError):
        pass
    finally:
        pipe.close()


class Worker:
    def __init__(self):
        context = multiprocessing.get_context("spawn")
        self.pipe, child = context.Pipe()
        self.process = context.Process(target=_worker, args=(child,), daemon=True)
        # No await separates creation from ownership: cancellation cannot leave
        # a child outside the pool's cleanup responsibility.
        try:
            self.process.start()
        except BaseException:
            self.pipe.close()
            raise
        finally:
            child.close()

    async def close(self):
        if self.process.is_alive():
            self.process.kill()
        self.pipe.close()
        with anyio.CancelScope(shield=True):
            await anyio.to_thread.run_sync(self.process.join)
        self.process.close()


_idle: RunVar[deque[Worker]] = RunVar("deckr_render_idle")


def _pool():
    try:
        return _idle.get()
    except LookupError:
        pool = deque()
        _idle.set(pool)
        return pool


async def run(function, *args):
    await anyio.lowlevel.checkpoint()
    pool = _pool()
    worker = pool.pop() if pool else Worker()
    try:
        # Pipes have finite capacity; perform writes outside the event loop.
        await anyio.to_thread.run_sync(
            worker.pipe.send_bytes,
            pickle.dumps((function, args)),
            abandon_on_cancel=True,
        )
        # Polling avoids a cancelled blocking reader retaining a thread/pipe.
        while not worker.pipe.poll():
            if not worker.process.is_alive():
                raise RuntimeError("Render worker exited without a result")
            await anyio.sleep(0.005)
        ok, value = pickle.loads(worker.pipe.recv_bytes())
    except BaseException:
        await worker.close()
        raise
    else:
        pool.append(worker)
        if not ok:
            raise value
        return value


async def close_idle():
    pool = _pool()
    while pool:
        await pool.pop().close()
