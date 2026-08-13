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

**The shared store closes that**, and is one setting:
`MASSINGPLAN_RATE_LIMIT_STORE=database` counts in the database, so every worker
and every replica pointed at it shares one counter. Memory stays the default
because it needs nothing configured and is exactly right for one worker. An
ingress limit is still the better control where you have one -- it stops the
traffic before it reaches Python at all -- but "put one at your ingress" is not
an answer for a deployment that has no ingress to put it at.
"""

from __future__ import annotations

import logging
import secrets
import threading
import time
from dataclasses import dataclass
from typing import Any, Protocol

from ..models.base import new_id

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


class DatabaseStore:
    """Counters in the database, so N workers share one limit.

    The fix for the module docstring's loudest caveat. Everything about it is
    shaped by three facts that a per-process counter never has to face.

    **The clock has to be shared.** `time.monotonic()` has a per-process
    origin, so two workers asked about the same instant would place it in
    different windows and each keep its own count -- the multiply-by-N bug,
    reintroduced through the back door. Windows are therefore keyed on
    wall-clock seconds. Wall clocks can step under NTP; a step forward closes a
    window early and a step back reopens one, both of which cost at most one
    window of accuracy, and neither is as bad as not sharing a clock at all.

    **The increment has to be atomic.** Read-then-write would let two workers
    both read 19, both write 20, and let 21 requests through a limit of 20.
    `INSERT ... ON CONFLICT DO UPDATE SET hits = hits + 1` is one statement and
    both supported dialects have it.

    **It has to commit outside the request's transaction.** A failed sign-in
    rolls its request back; if the counter rode along it would roll back too,
    and failed attempts -- the exact thing being limited -- would not count.
    So this opens its own session and commits before returning.
    """

    name = "database"

    #: Rows are swept probabilistically rather than on every hit, because a
    #: DELETE per request to buy tidiness on a table that is already small is a
    #: poor trade. One in fifty keeps it bounded.
    PRUNE_ODDS = 50

    def __init__(self, session_factory: Any = None) -> None:
        self._session_factory = session_factory

    def _session(self) -> Any:
        if self._session_factory is not None:
            return self._session_factory()
        from .. import database

        return database.new_session()

    def hit(self, key: str, limit: Limit, now: float) -> tuple[int, float]:
        from sqlalchemy import delete, select

        from ..models import RateLimitHit

        window_start = int(now) - (int(now) % limit.per_seconds)
        ends_at = float(window_start + limit.per_seconds)

        session = self._session()
        try:
            dialect = session.bind.dialect.name if session.bind is not None else ""
            # Both dialects spell the upsert the same way and mypy sees two
            # different `Insert` types, so the choice is made as a value rather
            # than as two conditional imports bound to one name.
            if dialect == "postgresql":
                from sqlalchemy.dialects.postgresql import insert as pg_insert

                builder: Any = pg_insert
            else:
                from sqlalchemy.dialects.sqlite import insert as sqlite_insert

                builder = sqlite_insert

            statement = builder(RateLimitHit).values(
                id=new_id(), key=key[:320], window_start=window_start, hits=1
            )
            statement = statement.on_conflict_do_update(
                index_elements=["key", "window_start"],
                set_={"hits": RateLimitHit.hits + 1},
            )
            session.execute(statement)

            count = session.scalar(
                select(RateLimitHit.hits).where(
                    RateLimitHit.key == key[:320],
                    RateLimitHit.window_start == window_start,
                )
            )
            if secrets.randbelow(self.PRUNE_ODDS) == 0:
                # Anything whose window closed cannot affect a decision again.
                session.execute(
                    delete(RateLimitHit).where(RateLimitHit.window_start < window_start)
                )
            session.commit()
            return int(count or 1), ends_at
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


class RateLimiter:
    def __init__(self, store: Store | None = None, *, enabled: bool = True) -> None:
        self.store: Store = store or MemoryStore()
        self.enabled = enabled

    def check(self, endpoint: str, identity: str, *, now: float | None = None) -> Decision | None:
        """``None`` when this endpoint is not limited."""
        limit = LIMITS.get(endpoint)
        if limit is None or not self.enabled:
            return None
        # Wall clock, not `time.monotonic()`. A monotonic clock's origin is
        # per-process, so a shared store would see two workers disagree about
        # which window "now" falls in -- and each would keep its own count,
        # which is the very multiplication a shared store exists to remove.
        # `MemoryStore` only ever takes differences, so it is unaffected.
        moment = time.time() if now is None else now
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
            "larger. Set MASSINGPLAN_RATE_LIMIT_STORE=database to share one "
            "counter across workers, or put a rate limit at your ingress.",
            workers,
            workers,
            extra={"context": {"workers": workers, "store": "memory"}},
        )
