"""Progress-render cancellation must not suppress the final authorization check."""

import asyncio

import pytest
from daimon.core.turn.errors import AdmissionDenied
from daimon.core.turn.output import OutputAuthorization


@pytest.mark.parametrize("revoked", [False, True])
async def test_cancelled_progress_rechecks_access_for_final_delivery(revoked):
    started = asyncio.Event()
    finish = asyncio.Event()
    calls = 0

    async def authorize():
        nonlocal calls
        calls += 1
        started.set()
        await finish.wait()
        if revoked:
            raise AdmissionDenied(reason="invoker_not_allowed")

    gate = OutputAuthorization(authorize)
    progress = asyncio.create_task(gate.check())
    await started.wait()
    progress.cancel()
    with pytest.raises(asyncio.CancelledError):
        await progress
    assert not gate.failed
    finish.set()
    if revoked:
        with pytest.raises(AdmissionDenied):
            await gate.check()
        assert gate.failed
        with pytest.raises(AdmissionDenied):
            await gate.check()
        assert calls == 2  # A proved denial remains latched.
    else:
        await gate.check()
        assert calls == 2
        assert not gate.failed
