"""Retry helper with exponential backoff."""
from __future__ import annotations

import time
from typing import Callable, TypeVar

T = TypeVar("T")


def retry(fn: Callable[[], T], attempts: int = 3, base_delay: float = 0.01) -> T:
    last: Exception | None = None
    for i in range(attempts - 1):
        try:
            return fn()
        except Exception as e:
            last = e
            if i < attempts - 1:
                time.sleep(base_delay * (2 ** i))
    raise last


def retry_on(fn: Callable[[], T], exc: type[BaseException], attempts: int = 3) -> T:
    """Retry only on a specific exception type; anything else propagates at once."""
    for i in range(attempts):
        try:
            return fn()
        except exc:
            if i == attempts - 1:
                raise
    raise AssertionError("unreachable")
