"""`RunningTurns`: the process registry shutdown drains read."""

from __future__ import annotations

import asyncio

import pytest
from daimon.core.turn.running import RunningTurns


async def test_a_task_is_counted_until_its_outermost_block_exits() -> None:
    running = RunningTurns()
    with running.track():
        assert running.tasks() == {asyncio.current_task()}
        with running.track():
            assert running.count() == 1, "a nested turn in one task counts once"
        assert running.count() == 1, "the outer block still holds it"
    assert running.count() == 0


async def test_an_exception_unregisters_the_task() -> None:
    running = RunningTurns()
    with pytest.raises(RuntimeError), running.track():
        raise RuntimeError
    assert running.count() == 0


async def test_concurrent_turns_are_counted_separately_and_finished_ones_are_not() -> None:
    running = RunningTurns()
    release = asyncio.Event()
    entered = asyncio.Event()

    async def turn() -> None:
        with running.track():
            entered.set()
            await release.wait()

    first = asyncio.create_task(turn())
    await entered.wait()
    entered.clear()
    second = asyncio.create_task(turn())
    await entered.wait()
    assert running.tasks() == {first, second}

    release.set()
    await asyncio.gather(first, second)
    assert running.count() == 0


async def test_a_loop_callback_outside_any_task_is_not_counted() -> None:
    running = RunningTurns()
    counts: list[int] = []

    def callback() -> None:
        with running.track():
            counts.append(running.count())

    asyncio.get_running_loop().call_soon(callback)
    await asyncio.sleep(0)
    assert counts == [0]
