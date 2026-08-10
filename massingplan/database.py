"""Engine and session lifecycle.

Plain SQLAlchemy 2.0 rather than Flask-SQLAlchemy: the session is handed to
service functions as an argument, so `services/` stays importable without a
Flask application context -- which is what lets the CLI and the test suite use
the same code the web layer does.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from .models import Base

_engine: Engine | None = None
_Session: sessionmaker[Session] | None = None


def _configure_sqlite(dbapi_connection: object, _record: object) -> None:
    """Two pragmas SQLite needs before it behaves like a database.

    Foreign keys are **off by default** in SQLite. Without this, every
    `ondelete="CASCADE"` in the schema is decoration: deleting a project leaves
    orphaned activities and relationships, and the next schedule run fails
    validation on a project nobody knowingly broke.

    WAL lets a read proceed while a write is in flight, which is the difference
    between a Gantt page that loads during an import and one that times out.
    """
    cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
    cursor.execute("PRAGMA foreign_keys=ON")
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.close()


def init_engine(url: str, *, echo: bool = False) -> Engine:
    global _engine, _Session
    connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
    _engine = create_engine(url, echo=echo, future=True, connect_args=connect_args)
    if url.startswith("sqlite"):
        event.listen(_engine, "connect", _configure_sqlite)
    _Session = sessionmaker(bind=_engine, expire_on_commit=False, future=True)
    return _engine


def get_engine() -> Engine:
    if _engine is None:
        raise RuntimeError("the database engine is not initialised; call init_engine first")
    return _engine


def create_all() -> None:
    """Build the schema directly.

    For tests and a first local run. Production uses Alembic -- `flask db
    upgrade` -- because `create_all` cannot alter an existing table, so a schema
    change on a database with data in it silently does nothing.
    """
    Base.metadata.create_all(get_engine())


@contextmanager
def session_scope() -> Iterator[Session]:
    """A transaction that commits on success and rolls back on anything else.

    The rollback is the point. A service that raises halfway through leaves a
    partially written project otherwise, and the next read treats it as truth.
    """
    if _Session is None:
        raise RuntimeError("the database is not initialised; call init_engine first")
    session = _Session()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def new_session() -> Session:
    """A bare session, for a caller managing its own transaction."""
    if _Session is None:
        raise RuntimeError("the database is not initialised; call init_engine first")
    return _Session()
