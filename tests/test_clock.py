import asyncio
import time

from clock import Clock, LiveClock, VirtualClock


def test_live_clock_satisfies_protocol():
    assert isinstance(LiveClock(), Clock)
    assert isinstance(VirtualClock(), Clock)


def test_live_clock_now_tracks_wall_clock():
    assert abs(LiveClock().now() - time.time()) < 1.0


def test_virtual_clock_starts_at_given_time():
    assert VirtualClock(start=1_700_000_000.0).now() == 1_700_000_000.0


def test_virtual_clock_sleep_advances_time_without_waiting():
    clock = VirtualClock()

    async def scenario():
        started = time.monotonic()
        await clock.sleep(3600)
        return time.monotonic() - started

    real_elapsed = asyncio.run(scenario())
    assert clock.now() == 3600
    assert real_elapsed < 1.0  # an hour of simulated time, effectively instant


def test_virtual_clock_non_positive_sleep_does_not_advance():
    clock = VirtualClock(start=100.0)
    asyncio.run(clock.sleep(0))
    assert clock.now() == 100.0


def test_virtual_clock_releases_sleepers_in_wake_order():
    clock = VirtualClock()
    wake_order = []

    async def sleeper(name, seconds):
        await clock.sleep(seconds)
        wake_order.append((name, clock.now()))

    async def scenario():
        await asyncio.gather(sleeper("c", 30), sleeper("a", 10), sleeper("b", 20))

    asyncio.run(scenario())
    assert [name for name, _ in wake_order] == ["a", "b", "c"]
    assert [at for _, at in wake_order] == [10, 20, 30]


def test_externally_driven_clock_does_not_advance_on_its_own():
    # With autoadvance off a sleeper must wait for the driver, so replayed events set the pace
    # instead of the strategy pulling time past messages that have not been delivered yet.
    clock = VirtualClock(start=0.0, autoadvance=False)
    woke = []

    async def scenario():
        async def sleeper():
            await clock.sleep(5)
            woke.append(clock.now())

        task = asyncio.create_task(sleeper())
        await asyncio.sleep(0)
        assert woke == []
        assert clock.now() == 0.0
        await clock.advance_to(10.0)
        await task

    asyncio.run(scenario())
    assert woke == [5]  # released at its own wake time, not at the driver's target
    assert clock.now() == 10.0


def test_externally_driven_clock_leaves_future_sleepers_pending():
    clock = VirtualClock(start=0.0, autoadvance=False)
    woke = []

    async def scenario():
        async def sleeper(seconds):
            await clock.sleep(seconds)
            woke.append(seconds)

        near = asyncio.create_task(sleeper(5))
        far = asyncio.create_task(sleeper(500))
        await asyncio.sleep(0)
        await clock.advance_to(10.0)
        await near
        assert woke == [5]
        assert not far.done()
        await clock.advance_to(1000.0)
        await far

    asyncio.run(scenario())
    assert woke == [5, 500]
