"""Engines are disposed when they are replaced.

`init_engine` used to rebind the module global and abandon the previous engine
with a live connection in its pool. The engine was then garbage collected and
took the still-open connection with it.

Nothing said so. A DBAPI connection collected while open is silent on Python
3.11 and 3.12; **Python 3.13 added a `ResourceWarning` for it**, and the leak
surfaced only when warnings became errors — `test (3.13)` failed while the other
two interpreters passed, which is the signature of a real defect the older
runtimes simply did not mention.

These tests detect it directly rather than relying on the interpreter to
volunteer it, so they fail on 3.11 too.
"""

from __future__ import annotations

import sqlite3

import pytest
from sqlalchemy import text

from massingplan import database
from massingplan.app import create_app
from massingplan.config import Settings


def _is_open(connection: sqlite3.Connection) -> bool:
    """A closed sqlite3 connection raises on use; an open one does not."""
    try:
        connection.execute("SELECT 1")
    except sqlite3.ProgrammingError:
        return False
    return True


@pytest.fixture
def tracked(monkeypatch):  # type: ignore[no-untyped-def]
    """Every sqlite connection SQLAlchemy opens, in order."""
    opened: list[sqlite3.Connection] = []
    real_connect = sqlite3.connect

    def tracking_connect(*args, **kwargs):  # type: ignore[no-untyped-def]
        connection = real_connect(*args, **kwargs)
        opened.append(connection)
        return connection

    # Both modules. SQLAlchemy's pysqlite dialect does
    # `from sqlite3 import dbapi2 as sqlite`, and `sqlite3.dbapi2` is a
    # *separate module object* from `sqlite3` -- `sqlite3/__init__.py` merely
    # re-exports its names. Patching only `sqlite3.connect` tracks nothing,
    # which is how the first version of this fixture recorded zero connections
    # and looked like a passing test.
    monkeypatch.setattr(sqlite3.dbapi2, "connect", tracking_connect)  # type: ignore[attr-defined]
    monkeypatch.setattr(sqlite3, "connect", tracking_connect)
    return opened


def _build(tmp_path, index: int) -> None:  # type: ignore[no-untyped-def]
    create_app(
        Settings(
            env="testing",
            secret_key="test-key",
            database_url=f"sqlite:///{tmp_path / f'db{index}.sqlite'}",
        )
    )
    database.create_all()
    with database.session_scope() as session:
        session.execute(text("SELECT 1"))


def test_replacing_the_engine_closes_the_one_it_replaces(tracked, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """The regression, stated as the property that was violated.

    Five apps, five databases, five connections opened. Four of those engines
    have been replaced, so four connections must be closed and only the current
    one may still be open.
    """
    for index in range(5):
        _build(tmp_path, index)

    assert len(tracked) >= 5, f"expected a connection per app, saw {len(tracked)}"
    still_open = [connection for connection in tracked if _is_open(connection)]
    assert len(still_open) <= 1, (
        f"{len(still_open)} of {len(tracked)} sqlite connections are still open. "
        "Replacing the global engine must dispose the one it replaces -- on "
        "Postgres each of these is a server-side session that survives until "
        "the process exits."
    )


def test_the_leak_does_not_grow_with_the_number_of_apps(tracked, tmp_path) -> None:  # type: ignore[no-untyped-def]
    """A ratio, so this holds regardless of how many connections a pool opens
    for its own reasons. What must not happen is one more leak per app.
    """
    for index in range(3):
        _build(tmp_path, index)
    after_three = sum(1 for connection in tracked if _is_open(connection))

    for index in range(3, 12):
        _build(tmp_path, index)
    after_twelve = sum(1 for connection in tracked if _is_open(connection))

    assert after_twelve <= after_three, (
        f"open connections grew from {after_three} to {after_twelve} as apps "
        "were built -- the count must not scale with the number of engines"
    )


def test_dispose_is_what_closes_it() -> None:
    """Guard the guard: confirm the mechanism, so a future change that drops
    the `dispose()` call fails here with an explanation rather than only as an
    unrelated-looking ResourceWarning on one interpreter.
    """
    engine = database.init_engine("sqlite:///:memory:")
    raw = engine.raw_connection().dbapi_connection
    assert raw is not None
    assert _is_open(raw)
    engine.dispose()
    assert not _is_open(raw)
