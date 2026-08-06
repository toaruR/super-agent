"""In-memory cache with TTL, built on the retry helper."""
from __future__ import annotations

import time
from typing import Any

from .retry import retry


class TTLCache:
    def __init__(self, ttl: float = 60.0) -> None:
        self.ttl = ttl
        self._data: dict[str, tuple[float, Any]] = {}

    def put(self, key: str, value: Any) -> None:
        self._data[key] = (time.time(), value)

    def get(self, key: str) -> Any | None:
        hit = self._data.get(key)
        if hit is None:
            return None
        ts, value = hit
        if time.time() - ts > self.ttl:
            del self._data[key]
            return None
        return value

    def get_or_load(self, key: str, loader) -> Any:
        cached = self.get(key)
        if cached is not None:
            return cached
        value = retry(loader, attempts=3)
        self.put(key, value)
        return value

    def size(self) -> int:
        return len(self._data)
