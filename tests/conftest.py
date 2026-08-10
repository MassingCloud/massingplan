"""Test harness.

Two promises are defended here, and both have to be defended by machinery rather
than discipline:

* **The suite runs offline.** An accidental network call in a scheduling engine
  is not a slow test, it is a dependency nobody declared. Sockets raise.
* **The suite is deterministic.** Nothing reads the ambient environment, and the
  engine's own determinism is checked by running the same computation under a
  different hash seed in CI.
"""

from __future__ import annotations

import socket
from datetime import date

import pytest

from massingplan.core.timeaxis import WorkCalendar, WorkPattern, bind_window

_REAL_CONNECT = socket.socket.connect

#: The window every fixture calendar is bound over. Wide enough that a 600-day
#: DCMA probe and a Monte Carlo tail both stay inside it.
WINDOW_FIRST = date(2025, 1, 1)
WINDOW_LAST = date(2030, 12, 31)


def _blocked(self: socket.socket, address: object) -> None:
    host = address[0] if isinstance(address, tuple) else address
    if host in ("127.0.0.1", "::1", "localhost"):
        return _REAL_CONNECT(self, address)  # type: ignore[arg-type,return-value]
    raise RuntimeError(
        f"the test suite tried to reach {address!r}. massingplan's engine has no "
        "network dependencies; if a test needs one, it is testing the wrong thing."
    )


@pytest.fixture(autouse=True)
def block_network(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(socket.socket, "connect", _blocked)


@pytest.fixture(autouse=True, scope="session")
def cheap_password_hashing() -> None:
    """Turn argon2's cost down **for the suite only**.

    Production uses the OWASP floor: 64MiB and three passes, ~50ms per hash,
    slow on purpose. Several hundred registrations across the suite then
    allocate tens of gigabytes in total, and on a machine under memory pressure
    argon2 raises `HashingError: Memory allocation error` -- a failure that
    looks like a defect in the code under test and is not one. It happened twice
    while this suite was being run.

    The shipped parameters are still asserted, by
    `test_auth.py::test_the_shipped_hashing_cost_is_the_owasp_floor`, which
    reads `SHIPPED_PARAMETERS` -- captured at import, before this runs. So the
    suite is cheap and lowering the real cost still fails a test.
    """
    from massingplan.services import accounts

    accounts.TIME_COST = 1
    accounts.MEMORY_COST_KIB = 8
    accounts.PARALLELISM = 1


def test_the_network_guard_actually_blocks() -> None:
    """Guard the guard: if this stops raising, every other test's isolation is a lie."""
    with pytest.raises(RuntimeError, match="tried to reach"):
        socket.socket().connect(("example.com", 80))


@pytest.fixture
def five_day() -> WorkCalendar:
    cal = WorkCalendar(id="5D", name="Mon-Fri", pattern=WorkPattern(frozenset({0, 1, 2, 3, 4})))
    cal.bind(WINDOW_FIRST, WINDOW_LAST)
    return cal


@pytest.fixture
def six_day() -> WorkCalendar:
    cal = WorkCalendar(id="6D", name="Mon-Sat", pattern=WorkPattern(frozenset({0, 1, 2, 3, 4, 5})))
    cal.bind(WINDOW_FIRST, WINDOW_LAST)
    return cal


@pytest.fixture
def seven_day() -> WorkCalendar:
    cal = WorkCalendar(
        id="7D", name="Every day", pattern=WorkPattern(frozenset({0, 1, 2, 3, 4, 5, 6}))
    )
    cal.bind(WINDOW_FIRST, WINDOW_LAST)
    return cal


@pytest.fixture
def calendars(five_day: WorkCalendar, six_day: WorkCalendar, seven_day: WorkCalendar):  # type: ignore[no-untyped-def]
    cals = {c.id: c for c in (five_day, six_day, seven_day)}
    bind_window(cals, WINDOW_FIRST, WINDOW_LAST)
    return cals
