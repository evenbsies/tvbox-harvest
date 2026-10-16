"""Async token-bucket rate limiter, keyed per site.

Politeness is a hard requirement of this project: we hammer nobody. Every outbound
request acquires a token from that site's bucket, and an optional random jitter is added
on top so traffic does not look like a metronome.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass, field


@dataclass
class _Bucket:
    rate: float  # tokens per second
    capacity: float
    tokens: float = 0.0
    updated: float = field(default_factory=time.monotonic)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def _refill(self) -> None:
        now = time.monotonic()
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
        self.updated = now

    async def acquire(self) -> float:
        """Wait until a token is available; return the seconds actually slept."""
        slept = 0.0
        async with self.lock:
            while True:
                self._refill()
                if self.tokens >= 1.0:
                    self.tokens -= 1.0
                    return slept
                deficit = (1.0 - self.tokens) / self.rate
                await asyncio.sleep(deficit)
                slept += deficit


class RateLimiter:
    def __init__(self, default_rate: float = 1.0, jitter: float = 0.0) -> None:
        self.default_rate = max(default_rate, 0.01)
        self.jitter = max(jitter, 0.0)
        self._buckets: dict[str, _Bucket] = {}

    def configure(self, key: str, rate: float) -> None:
        rate = max(min(rate, 10.0), 0.01)
        self._buckets[key] = _Bucket(rate=rate, capacity=max(rate, 1.0), tokens=1.0)

    async def acquire(self, key: str) -> None:
        bucket = self._buckets.get(key)
        if bucket is None:
            bucket = _Bucket(rate=self.default_rate, capacity=max(self.default_rate, 1.0), tokens=1.0)
            self._buckets[key] = bucket
        await bucket.acquire()
        if self.jitter:
            await asyncio.sleep(random.uniform(0, self.jitter))


_limiter: RateLimiter | None = None


def get_limiter() -> RateLimiter:
    global _limiter
    if _limiter is None:
        _limiter = RateLimiter()
    return _limiter
