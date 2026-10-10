"""Clock identity is diagnostic; equal times still have one global order."""

import pytest
from daimon.testing.host_clock import ClockSample, HostClock


def test_shared_clock_orders_equal_time_observations_across_boundaries() -> None:
    clock = HostClock(lambda: 2.0)
    source = clock.sample()
    assert clock() == 2.0
    post = clock.last
    terminal = clock.sample()
    assert source.clock_id == post.clock_id == terminal.clock_id
    assert (source.order, post.order, terminal.order) == (1, 2, 3)
    assert source.observed_s == post.observed_s == terminal.observed_s
    assert ClockSample.model_validate_json(terminal.model_dump_json()) == terminal
    assert HostClock(lambda: 2.0).sample().clock_id != terminal.clock_id


@pytest.mark.parametrize("value", [-1.0, float("nan"), float("inf"), True])
def test_clock_rejects_invalid_time_before_recording(value: float) -> None:
    clock = HostClock(lambda: value)
    with pytest.raises(ValueError):
        clock.sample()
    with pytest.raises(ValueError, match="no observation"):
        _ = clock.last


def test_backwards_clock_preserves_the_last_actual_sample() -> None:
    values = iter([2.0, 1.0, 3.0])
    clock = HostClock(lambda: next(values))
    opening = clock.sample()
    with pytest.raises(ValueError, match="monotonic"):
        clock.sample()
    assert clock.last == opening
    following = clock.sample()
    assert following.order == 2 and following.observed_s == 3.0
