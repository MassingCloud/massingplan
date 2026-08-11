"""Rate limiting, with an honest account of what it does and does not cover.

A fixed-window counter rather than a token bucket: the window boundary lets
twice the limit through across two adjacent windows, which is a real weakness
and an acceptable one here. What matters is stopping a script, and a script that
carefully straddles a window boundary to get 2N instead of N is a script that
has already been slowed to the point of uselessness.

**The limitation that matters, stated loudly.** The default store is in-process.
With `WEB_CONCURRENCY=4` the effective limit is four times what you configured,
because each worker counts separately. `describe()` says so, `massingplan check`
prints it, and the app logs a warning at startup when it detects more than one
worker. A rate limiter that silently multiplies by the worker count is worse
than none, because it is believed.

For a real limit across replicas, put one at your ingress, or supply a shared
store.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Protocol

logger = logging.getLogger("massingplan.ratelimit")


@dataclass(frozen=True)
class Limit:
    """``count`` requests per ``per_seconds``."""

    count: int
    per_seconds: int

    def __str__(self) -> str:
        return f"{self.count}/{self.per_seconds}s"


@dataclass(frozen=True)
class Decision:
    allowed: bool
    limit: Limit
    remaining: int
    retry_after_seconds: int


#: Per-endpoint limits. Anything not named here is unlimited by this module --
#: deliberately, so adding an endpoint does not silently inherit a number
#: somebody chose for a different one.
#:
#: The two groups are different problems. Credential endpoints are valuable to
#: an attacker, so they are limited hard. The compute endpoints are legitimate
#: and expensive enough that a handful of concurrent Monte Carlo runs is a
#: denial of service by accident.
#:
#: This comment used to say credential endpoints were "cheap for the server",
#: which a load test disproved: `auth.sign_in` runs argon2id at 64MiB, making
#: it the single most expensive thing the app does per request. That matters
#: here because a limit of twenty per fifteen minutes does **not** bound twenty
#: arriving at the same instant -- all twenty pass, and all twenty allocate.
#: The concurrency bound that closes that gap is
#: `accounts.MAX_CONCURRENT_HASHES`; this table is the rate half, not the
#: simultaneity half, and neither substitutes for the other.
LIMITS: dict[str, Limit] = {
    "auth.sign_in": Limit(20, 900),
    # Tighter than sign-in, because the search space is smaller. A six-digit
    # code is one in a million per guess, but a guess costs nothing and the
    # window is thirty seconds wide: unlimited attempts make the second factor
    # decorative. Ten per fifteen minutes leaves a fat-fingered user room and an
    # attacker none.
    "auth.mfa_challenge": Limit(10, 900),
    "auth.register": Limit(10, 3600),
    "main.create_key": Limit(20, 3600),
    "schedule_api.risk": Limit(30, 60),
    "schedule_api.level": Limit(60, 60),
    "schedule_api.compare": Limit(60, 60),
    "schedule_api.import_schedule": Limit(30, 60),
    "main.upload": Limit(30, 60),
}


class Store(Protocol):
    def hit(self, key: str, limit: Limit, now: float) -> tuple[int, float]:
        """Record a hit. Returns ``(count in window, window ends at)``."""


class MemoryStore:
    """Per-process counters.

    Correct for one worker and wrong by a factor of N for N workers. That is why
    nothing here pretends otherwise: see the module docstring.
    """

    name = "memory"

    def __init__(self) -> None:
        self._windows: dict[str, tuple[int, float]] = {}
        self._lock = threading.Lock()

    def hit(self, key: str, limit: Limit, now: float) -> tuple[int, float]:
        with self._lock:
            count, ends_at = self._windows.get(key, (0, 0.0))
            if now >= ends_at:
                count, ends_at = 0, now + limit.per_seconds
            count += 1
            self._windows[key] = (count, ends_at)
            # Opportunistic sweep. Without it the dict grows by one entry per
            # distinct client forever, which on a public endpoint is a slow
            # memory leak that looks like nothing until it is a page of RSS.
            if len(self._windows) > 10_000:
                self._windows = {k: v for k, v in self._windows.items() if v[1] > now}
            return count, ends_at


class RateLimiter:
    def __init__(self, store: Store | None = None, *, enabled: bool = True) -> None:
        self.store: Store = store or MemoryStore()
        self.enabled = enabled

    def check(self, endpoint: str, identity: str, *, now: float | None = None) -> Decision | None:
        """``None`` when this endpoint is not limited."""
        limit = LIMITS.get(endpoint)
        if limit is None or not self.enabled:
            return None
        moment = time.monotonic() if now is None else now
        count, ends_at = self.store.hit(f"{endpoint}:{identity}", limit, moment)
        return Decision(
            allowed=count <= limit.count,
            limit=limit,
            remaining=max(0, limit.count - count),
            retry_after_seconds=max(1, int(ends_at - moment)),
        )

    def describe(self) -> dict[str, object]:
        store_name = getattr(self.store, "name", type(self.store).__name__)
        return {
            "enabled": self.enabled,
            "store": store_name,
            "scope": (
                "per process -- with N workers the effective limit is N times "
                "the configured one; put a limit at your ingress for a real one"
                if store_name == "memory"
                else "shared"
            ),
            "limits": {endpoint: str(limit) for endpoint, limit in sorted(LIMITS.items())},
        }


def warn_if_multi_worker(limiter: RateLimiter, workers: int) -> None:
    """Say it out loud at startup, once.

    A limiter that silently multiplies by the worker count is worse than none,
    because it is believed.
    """
    if workers > 1 and getattr(limiter.store, "name", "") == "memory":
        logger.warning(
            "rate limiting uses a per-process store and this deployment has "
            "%d workers, so every configured limit is effectively %d times "
            "larger. Put a rate limit at your ingress for a real one.",
            workers,
            workers,
            extra={"context": {"workers": workers, "store": "memory"}},
        )
