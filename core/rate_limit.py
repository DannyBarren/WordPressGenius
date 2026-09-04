"""Thread-safe sliding-window rate limiting.

Used to cap how many workflow runs a single app user (or client) can trigger per
minute, protecting both the WordPress site and any paid LLM/API quotas from
runaway loops or abuse. In-memory by design for the self-hosted single-process
deployment; a multi-process/SaaS deployment should back this with Redis.
"""

from __future__ import annotations

import time
from collections import defaultdict, deque
from dataclasses import dataclass
from threading import Lock


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    remaining: int
    retry_after_seconds: float


class SlidingWindowRateLimiter:
    """Allow at most ``max_events`` per ``window_seconds`` for each key."""

    def __init__(self, max_events: int, window_seconds: float = 60.0) -> None:
        self.max_events = max(1, int(max_events))
        self.window_seconds = float(window_seconds)
        self._lock = Lock()
        self._events: dict[str, deque[float]] = defaultdict(deque)

    def check(self, key: str, *, now: float | None = None) -> RateLimitResult:
        """Record an attempt for *key* and report whether it is allowed."""

        timestamp = time.monotonic() if now is None else now
        cutoff = timestamp - self.window_seconds
        with self._lock:
            events = self._events[key]
            while events and events[0] <= cutoff:
                events.popleft()
            if len(events) >= self.max_events:
                retry_after = self.window_seconds - (timestamp - events[0])
                return RateLimitResult(allowed=False, remaining=0, retry_after_seconds=max(0.0, retry_after))
            events.append(timestamp)
            return RateLimitResult(
                allowed=True,
                remaining=self.max_events - len(events),
                retry_after_seconds=0.0,
            )

    def reset(self, key: str | None = None) -> None:
        with self._lock:
            if key is None:
                self._events.clear()
            else:
                self._events.pop(key, None)


@dataclass(frozen=True)
class RateLimitDecision:
    allowed: bool
    message: str
    retry_after_seconds: float


class RateLimiter:
    """Per-key request limiter with a user-friendly decision message.

    Thin wrapper over :class:`SlidingWindowRateLimiter` used by the Streamlit UI,
    which expects ``RateLimiter(max_requests=...).check(key).allowed`` and a
    ``.message`` to surface to the user.
    """

    def __init__(self, max_requests: int, window_seconds: float = 60.0) -> None:
        self._limiter = SlidingWindowRateLimiter(max_requests, window_seconds=window_seconds)
        self.max_requests = self._limiter.max_events
        self.window_seconds = self._limiter.window_seconds

    def check(self, key: str, *, now: float | None = None) -> RateLimitDecision:
        result = self._limiter.check(key, now=now)
        if result.allowed:
            return RateLimitDecision(allowed=True, message="", retry_after_seconds=0.0)
        wait = int(result.retry_after_seconds) + 1
        return RateLimitDecision(
            allowed=False,
            message=(
                f"You're sending requests too quickly (limit {self.max_requests} per "
                f"minute). Please wait about {wait} second(s) and try again."
            ),
            retry_after_seconds=result.retry_after_seconds,
        )

    def reset(self, key: str | None = None) -> None:
        self._limiter.reset(key)
