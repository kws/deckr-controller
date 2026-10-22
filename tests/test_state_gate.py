"""Admission remains exclusive across cancellation, reentrancy and handoff."""

import anyio
import pytest

from deckr.controller._state_gate import StateGate


@pytest.mark.asyncio
async def test_cancelled_waiter_cannot_strand_a_new_transition():
    boundary = StateGate()
    for _ in range(20):
        cancelled = anyio.CancelScope()
        entered, finished = anyio.Event(), anyio.Event()

        async def retiring(cancelled=cancelled):
            with cancelled:
                async with boundary:
                    pytest.fail("retired waiter must not enter")

        async def next_transition(entered=entered, finished=finished):
            async with boundary:
                entered.set()
            finished.set()

        with anyio.fail_after(1):
            async with anyio.create_task_group() as tg:
                async with boundary:
                    tg.start_soon(retiring)
                    while boundary.statistics().tasks_waiting != 1:
                        await anyio.sleep(0)
                    cancelled.cancel()
                # Reacquire immediately, before the cancelled waiter unwinds.
                async with boundary:
                    tg.start_soon(next_transition)
                    await anyio.sleep(0)
                    assert not entered.is_set()
                await finished.wait()


@pytest.mark.asyncio
async def test_immediate_reentry_never_overlaps_another_owner():
    boundary = StateGate()
    inside = 0

    async def transition():
        nonlocal inside
        for _ in range(30):
            async with boundary:
                inside += 1
                assert inside == 1
                async with boundary:
                    await anyio.sleep(0)
                    assert inside == 1
                inside -= 1

    with anyio.fail_after(1):
        async with anyio.create_task_group() as tg:
            for _ in range(4):
                tg.start_soon(transition)
