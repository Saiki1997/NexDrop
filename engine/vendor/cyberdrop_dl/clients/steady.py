"""NEXDROP patch (3.2): steady download speed.

A connection that stops sending, or slows to a crawl while the server could do better, is closed
and the download carries on from the bytes already on disk over a fresh connection:

- Stall: no bytes for `STALL_SECONDS` (default 20 s, well under the 60 s read timeout).
- Speed drop: after a warm-up, the speed over the last `WINDOW` seconds stays under `DROP_RATIO`
  of the best speed this connection reached, for `WINDOW` seconds. Only when the server can resume
  (answered a range, or says it accepts ranges), and never with a speed cap set.

Reconnects that moved the file forward do not use up the file's attempts (up to `MAX_RECONNECTS`
per file), so a big file on a flaky server is not given up after two hiccups.

The bridge sets the module values from Settings > Speed ("Keep download speed steady").
"""

from __future__ import annotations

import asyncio
import collections
import time
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Callable, Mapping

ENABLED: bool = True
STALL_SECONDS: float = 20.0
WINDOW: float = 15.0  # seconds of speed history that decide a drop
WARMUP: float = 20.0  # no drop check in a connection's first seconds (TCP ramp-up, server warm-up)
DROP_RATIO: float = 0.25  # reconnect when the speed falls under a quarter of this connection's best
MIN_PEAK: int = 512 * 1024  # a connection that never beat 512 KB/s is not "dropping", it is just slow
MAX_RECONNECTS: int = 8  # free reconnects per file; later ones count as normal attempts

RECONNECT_STATUS = 999  # the engine's "moved forward, retry at once" status


class SpeedDropped(Exception):
    def __init__(self, now: float, best: float) -> None:
        self.now = now
        self.best = best
        super().__init__(f"Speed dropped to {_rate(now)} from {_rate(best)}. Reconnecting")


def _rate(bps: float) -> str:
    if bps >= 1024 * 1024:
        return f"{bps / 1024 / 1024:.1f} MB/s"
    return f"{bps / 1024:.0f} KB/s"


def can_resume(status: int, headers: Mapping[str, str]) -> bool:
    if status == 206:
        return True
    return "bytes" in str(headers.get("Accept-Ranges", headers.get("accept-ranges", ""))).lower()


class SpeedWatch:
    """Speed of one connection. `feed` after each chunk raises `SpeedDropped` when it should reconnect."""

    __slots__ = ("_clock", "_samples", "_start", "_window_bytes", "below_since", "best", "enabled")

    def __init__(self, *, enabled: bool = True, clock: Callable[[], float] = time.monotonic) -> None:
        self._clock = clock
        self._start = clock()
        self._samples: collections.deque[tuple[float, int]] = collections.deque()
        self._window_bytes = 0
        self.best = 0.0
        self.below_since: float | None = None
        self.enabled = enabled and ENABLED and DROP_RATIO > 0

    def speed(self, now: float | None = None) -> float:
        now = self._clock() if now is None else now
        self._trim(now)
        span = min(WINDOW, max(now - self._start, 1e-6))
        return self._window_bytes / span

    def _trim(self, now: float) -> None:
        while self._samples and self._samples[0][0] < now - WINDOW:
            self._window_bytes -= self._samples.popleft()[1]

    def feed(self, n_bytes: int) -> None:
        now = self._clock()
        self._samples.append((now, n_bytes))
        self._window_bytes += n_bytes
        if not self.enabled:
            return
        elapsed = now - self._start
        if elapsed < min(WINDOW, WARMUP):
            return
        speed = self.speed(now)
        self.best = max(self.best, speed)
        if elapsed < WARMUP or self.best < MIN_PEAK:
            return
        if speed >= self.best * DROP_RATIO:
            self.below_since = None
            return
        if self.below_since is None:
            self.below_since = now
        elif now - self.below_since >= WINDOW:
            raise SpeedDropped(speed, self.best)


async def chunks(resp: Any, chunk_size: int, *, stall: float | None = None) -> AsyncIterator[bytes]:
    """`resp.iter_chunked(chunk_size)`, but a connection silent for `stall` seconds raises TimeoutError.

    The engine turns that TimeoutError into a resume when bytes were saved (status 999)."""
    stall = STALL_SECONDS if stall is None else stall
    iterator = resp.iter_chunked(chunk_size).__aiter__()
    if not (ENABLED and stall and stall > 0):
        async for chunk in iterator:
            yield chunk
        return
    while True:
        try:
            async with asyncio.timeout(stall):
                chunk = await iterator.__anext__()
        except StopAsyncIteration:
            return
        except TimeoutError:
            raise TimeoutError(f"No data for {stall:g} s") from None
        yield chunk


def free_reconnect(media_item: Any, error: BaseException) -> bool:
    """A reconnect that moved the file forward: give the attempt back (up to MAX_RECONNECTS per file)."""
    if not ENABLED or getattr(error, "status", None) != RECONNECT_STATUS:
        return False
    used = int(media_item.extra_info.get("steady_reconnects", 0))
    if used >= MAX_RECONNECTS:
        return False
    media_item.extra_info["steady_reconnects"] = used + 1
    media_item.attempts = max(0, media_item.attempts - 1)
    return True
