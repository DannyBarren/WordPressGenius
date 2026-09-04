"""Small in-process TTL cache for WordPress REST reads."""

from __future__ import annotations

import time
from dataclasses import dataclass
from threading import Lock
from typing import Any, Hashable


@dataclass
class _Entry:
    value: Any
    expires_at: float


class TTLCache:
    """Thread-safe TTL cache for single-process Streamlit deployments."""

    def __init__(self, ttl_seconds: int = 60) -> None:
        self.ttl_seconds = max(0, ttl_seconds)
        self._items: dict[Hashable, _Entry] = {}
        self._lock = Lock()

    def get(self, key: Hashable) -> Any | None:
        """Return cached value or None when missing/expired."""

        if self.ttl_seconds == 0:
            return None
        now = time.monotonic()
        with self._lock:
            entry = self._items.get(key)
            if not entry:
                return None
            if entry.expires_at <= now:
                self._items.pop(key, None)
                return None
            return entry.value

    def set(self, key: Hashable, value: Any) -> None:
        """Store value until the configured TTL expires."""

        if self.ttl_seconds == 0:
            return
        with self._lock:
            self._items[key] = _Entry(
                value=value,
                expires_at=time.monotonic() + self.ttl_seconds,
            )

    def clear(self) -> None:
        """Remove all cached entries."""

        with self._lock:
            self._items.clear()
