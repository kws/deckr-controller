import asyncio

import pytest_asyncio


@pytest_asyncio.fixture
async def session_tasks():
    tasks = set()

    def start_soon(fn, *args):
        task = asyncio.create_task(fn(*args))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    try:
        yield start_soon
    finally:
        pending = tuple(tasks)
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
