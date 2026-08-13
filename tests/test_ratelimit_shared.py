"""The shared rate-limit store, and the three things that make it worth having.

`SECURITY.md` carried this as its loudest limitation: the in-process counter is
wrong by a factor of N for N workers, and a limiter that silently multiplies is
worse than none because it is believed. This is the fix, so what follows is
mostly about the ways a shared counter can fail to actually be shared.

* **One counter across processes.** Two stores, no shared memory, one limit --
  which is the whole claim.
* **An atomic increment.** Read-then-write lets two workers both read 19, both
  write 20, and let 21 through a limit of 20.
* **A shared clock.** `time.monotonic()` has a per-process origin, so two
  workers would place the same instant in different windows and keep separate
  counts: the multiply-by-N bug, reintroduced underneath the fix.
"""

from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor

import pytest

from massingplan import database
from massingplan.config import Settings
from massingplan.services.ratelimit import (
    DatabaseStore,
    Limit,
    MemoryStore,
    RateLimiter,
    warn_if_multi_worker,
)


@pytest.fixture
def shared_db(tmp_path):  # type: ignore[no-untyped-def]
    """A database, and nothing else. No app: the store is the unit under test."""
    from massingplan.app import create_app

    create_app(
        Settings(
            env="testing",
            secret_key="k",
            database_url=f"sqlite:///{tmp_path / 'limits.db'}",
            rate_limit_enabled=False,
        )
    )
    database.create_all()
    return tmp_path


# -- the claim --------------------------------------------------------------


def test_two_stores_share_one_counter(shared_db) -> None:  # type: ignore[no-untyped-def]
    """Two `DatabaseStore` instances stand in for two workers: separate
    objects, no shared memory, one limit between them.

    The same test against `MemoryStore` is the bug being fixed, and it is
    asserted below so the difference is shown rather than claimed.
    """
    limit = Limit(count=5, per_seconds=60)
    now = time.time()
    workers = [DatabaseStore(), DatabaseStore(), DatabaseStore()]

    counts = [workers[n % 3].hit("auth.sign_in:1.2.3.4", limit, now)[0] for n in range(6)]
    assert counts == [1, 2, 3, 4, 5, 6], "each worker must see the running total"


def test_the_memory_store_is_the_bug_this_replaces(shared_db) -> None:  # type: ignore[no-untyped-def]
    """Stated as a test rather than as prose in a docstring.

    Three in-process stores let three times the limit through, which is exactly
    what `WEB_CONCURRENCY=3` does to a configured limit today.
    """
    limit = Limit(count=5, per_seconds=60)
    now = time.time()
    workers = [MemoryStore(), MemoryStore(), MemoryStore()]

    counts = [workers[n % 3].hit("auth.sign_in:1.2.3.4", limit, now)[0] for n in range(6)]
    assert counts == [1, 1, 1, 2, 2, 2], "each process counts alone -- the N-times bug"


def test_different_keys_do_not_share_a_counter(shared_db) -> None:  # type: ignore[no-untyped-def]
    """The other direction. A shared counter that shares too much would limit
    every user because one of them was noisy.
    """
    limit = Limit(count=5, per_seconds=60)
    now = time.time()
    store = DatabaseStore()

    assert store.hit("auth.sign_in:1.1.1.1", limit, now)[0] == 1
    assert store.hit("auth.sign_in:2.2.2.2", limit, now)[0] == 1
    assert store.hit("auth.register:1.1.1.1", limit, now)[0] == 1


# -- atomicity --------------------------------------------------------------


def test_concurrent_hits_are_counted_exactly_once_each(shared_db) -> None:  # type: ignore[no-untyped-def]
    """Read-then-write loses increments under a race, and a lost increment is a
    request through a limit that should have stopped it.

    Each thread gets its own store, as each worker would.
    """
    limit = Limit(count=1000, per_seconds=60)
    now = time.time()
    attempts = 40

    def one(_n: int) -> int:
        return DatabaseStore().hit("auth.sign_in:racer", limit, now)[0]

    with ThreadPoolExecutor(max_workers=8) as pool:
        seen = sorted(pool.map(one, range(attempts)))

    assert seen == list(range(1, attempts + 1)), (
        f"every hit must see a distinct running total -- a repeat is a lost increment: {seen}"
    )


# -- the clock --------------------------------------------------------------


def test_the_window_is_shared_wall_clock_not_a_per_process_origin(shared_db) -> None:  # type: ignore[no-untyped-def]
    """Two processes must agree which window an instant falls in.

    `time.monotonic()` counts from an arbitrary per-process origin, so worker A
    at monotonic 12.0 and worker B at monotonic 4000.0 can be the same wall
    second -- and would land in different windows, each with its own count.
    Windows are therefore floored wall-clock seconds, which every process
    computes identically.
    """
    limit = Limit(count=5, per_seconds=60)
    store = DatabaseStore()

    base = 1_800_000_000.0  # a wall-clock instant, floored to a window boundary
    inside = base + 59.0
    assert store.hit("k", limit, base)[0] == 1
    assert store.hit("k", limit, inside)[0] == 2, "still the same window"

    _count, ends_at = store.hit("k", limit, base)
    assert ends_at == base + 60, "the window boundary is derived, not remembered"


def test_a_new_window_starts_the_count_again(shared_db) -> None:  # type: ignore[no-untyped-def]
    limit = Limit(count=5, per_seconds=60)
    store = DatabaseStore()
    base = 1_800_000_000.0

    assert store.hit("k", limit, base)[0] == 1
    assert store.hit("k", limit, base + 60)[0] == 1, "the next window is a fresh count"


def test_closed_windows_are_swept(shared_db) -> None:  # type: ignore[no-untyped-def]
    """Otherwise the table grows by one row per distinct client per window,
    forever -- the same slow leak the memory store sweeps for.
    """
    from sqlalchemy import func, select

    from massingplan.models import RateLimitHit

    limit = Limit(count=5, per_seconds=60)
    store = DatabaseStore()
    store.PRUNE_ODDS = 1  # sweep on every hit, so the test is not probabilistic
    base = 1_800_000_000.0

    for n in range(20):
        store.hit(f"key-{n}", limit, base)
    with database.session_scope() as session:
        assert session.scalar(select(func.count()).select_from(RateLimitHit)) == 20

    store.hit("later", limit, base + 600)
    with database.session_scope() as session:
        remaining = session.scalar(select(func.count()).select_from(RateLimitHit))
    assert remaining == 1, "windows that have closed cannot affect a decision again"


# -- through the limiter ----------------------------------------------------


def test_the_limiter_refuses_once_the_shared_count_passes(shared_db) -> None:  # type: ignore[no-untyped-def]
    """End to end, and across two limiters -- the shape of two workers."""
    a = RateLimiter(DatabaseStore(), enabled=True)
    b = RateLimiter(DatabaseStore(), enabled=True)
    now = 1_800_000_000.0

    allowed = 0
    for n in range(25):
        limiter = a if n % 2 == 0 else b
        decision = limiter.check("auth.sign_in", "9.9.9.9", now=now)
        assert decision is not None
        allowed += decision.allowed

    assert allowed == 20, f"the configured limit is 20 per window, not 40: {allowed}"


def test_describe_says_shared_rather_than_per_process(shared_db) -> None:  # type: ignore[no-untyped-def]
    """`massingplan check` prints this. Saying "per process" while running a
    shared store is the same class of lie as the reverse.
    """
    described = RateLimiter(DatabaseStore(), enabled=True).describe()
    assert described["store"] == "database"
    assert described["scope"] == "shared"


def test_the_multi_worker_warning_is_silent_on_a_shared_store(shared_db, caplog) -> None:  # type: ignore[no-untyped-def]
    """The warning exists because the memory store multiplies. Shouting it at a
    deployment that has already fixed the problem is how warnings get ignored.
    """
    import logging

    with caplog.at_level(logging.WARNING):
        warn_if_multi_worker(RateLimiter(DatabaseStore(), enabled=True), workers=4)
    assert "per-process store" not in caplog.text

    with caplog.at_level(logging.WARNING):
        warn_if_multi_worker(RateLimiter(MemoryStore(), enabled=True), workers=4)
    assert "per-process store" in caplog.text
    assert "MASSINGPLAN_RATE_LIMIT_STORE=database" in caplog.text


def test_the_store_is_chosen_by_configuration(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """Off by default, because it needs a migration run and a database that can
    take the write; on with one setting.
    """
    from massingplan.app import create_app

    for value, expected in (("memory", "memory"), ("database", "database"), ("", "memory")):
        app = create_app(
            Settings(
                env="testing",
                secret_key="k",
                database_url=f"sqlite:///{tmp_path / f'{expected}-{value}.db'}",
                rate_limit_store=value,
            )
        )
        limiter = app.extensions["massingplan_ratelimit"]
        assert limiter.describe()["store"] == expected
