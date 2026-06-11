"""
signalkit/ratelimit.py
======================
Per-client sliding-window rate limiter for the public /ask endpoint.

/ask is the expensive route: it can trigger LLM spend and every call writes
an audit entry to the decision Volume. Unthrottled, a single client could
bloat the log or drain the token budget.

In-process state is intentional: the demo runs as a single container. If
the app ever scales out, each container enforces its own window, so the
effective global limit is (limit x containers) — acceptable for a demo,
documented here so nobody mistakes it for a distributed limiter.
"""

from __future__ import annotations

import os
import threading
import time
from collections import deque
from typing import Callable

DEFAULT_LIMIT = 20
DEFAULT_WINDOW_SECONDS = 60.0


class RateLimiter:
    """Sliding-window counter per client key (typically an IP address)."""

    def __init__(
        self,
        limit: int | None = None,
        window_seconds: float | None = None,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self.limit = limit if limit is not None else int(
            os.environ.get("SIGNAL_RATE_LIMIT", DEFAULT_LIMIT)
        )
        self.window = window_seconds if window_seconds is not None else float(
            os.environ.get("SIGNAL_RATE_WINDOW", DEFAULT_WINDOW_SECONDS)
        )
        self._now = now
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def check(self, key: str) -> float | None:
        """Record a hit for ``key``. Returns None if allowed, or the number
        of seconds until the next slot frees up if the limit is exceeded.

        A limit of 0 or below disables limiting entirely.
        """
        if self.limit <= 0:
            return None
        now = self._now()
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and now - hits[0] >= self.window:
                hits.popleft()
            if len(hits) >= self.limit:
                return round(self.window - (now - hits[0]), 1)
            hits.append(now)
            # Opportunistic cleanup so idle clients don't accumulate forever.
            if len(self._hits) > 10_000:
                for k in [k for k, v in self._hits.items() if not v]:
                    del self._hits[k]
            return None
