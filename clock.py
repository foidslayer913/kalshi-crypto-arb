from __future__ import annotations

import asyncio
import heapq
import time
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """Time source for anything that must run identically live and in simulation.

    Only *simulated* time goes through this protocol. A bare `await asyncio.sleep(0)` used to
    yield to the event loop is a scheduling concern, not a time delay, and stays as-is.
    """

    def now(self) -> float: ...

    async def sleep(self, seconds: float) -> None: ...


class LiveClock:
    """Wall-clock time. The production implementation."""

    def now(self) -> float:
        return time.time()

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(seconds)


class VirtualClock:
    """Simulated time that advances instantly, so a 60-second window replays in microseconds.

    Sleepers are held on a heap keyed by wake time and released in wake-time order, with a yield
    to the event loop between wake times so each sleeper observes its own wake time rather than
    whatever the clock later reached.

    Two drive modes:

    * `autoadvance=True` (default) — a sleeping task pulls time forward to the next wake time
      itself. Self-contained, and what scanner tests want.
    * `autoadvance=False` — `sleep()` blocks until an external driver calls `await advance_to()`.
      This is the mode a replay harness needs, so recorded messages set the pace instead of the
      strategy pulling time forward past events that have not been delivered yet.
    """

    def __init__(self, start: float = 0.0, *, autoadvance: bool = True) -> None:
        self._now = start
        self._autoadvance = autoadvance
        self._waiters: list[tuple[float, int, asyncio.Future[None]]] = []
        self._sequence = 0

    def now(self) -> float:
        return self._now

    async def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            await asyncio.sleep(0)
            return
        loop = asyncio.get_running_loop()
        future: asyncio.Future[None] = loop.create_future()
        self._sequence += 1
        heapq.heappush(self._waiters, (self._now + seconds, self._sequence, future))

        if not self._autoadvance:
            await future
            return

        # Yield before each advance so tasks released by the previous step run — and observe
        # their own wake time — before time moves again.
        while not future.done():
            await asyncio.sleep(0)
            if not future.done():
                self._release_next()
        await future

    def _release_next(self) -> None:
        """Jump to the earliest pending wake time and release everything due at that instant."""
        if not self._waiters:
            return
        self._now = max(self._now, self._waiters[0][0])
        while self._waiters and self._waiters[0][0] <= self._now:
            _, _, future = heapq.heappop(self._waiters)
            if not future.done():
                future.set_result(None)

    async def advance_to(self, timestamp: float) -> None:
        """Drive time forward to `timestamp`, releasing sleepers at their own wake times.

        Used by an external driver (replay) to pace simulated time off recorded event timestamps.
        """
        while self._waiters and self._waiters[0][0] <= timestamp:
            self._release_next()
            await asyncio.sleep(0)
        self._now = max(self._now, timestamp)
