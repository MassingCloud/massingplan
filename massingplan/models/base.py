"""Column conventions, applied everywhere so they cannot drift per table.

Three invariants the whole schema depends on:

**Dates are DATE, never DATETIME.** The engine works in whole days;
``date(2026, 6, 1) == datetime(2026, 6, 1, 0, 0)`` is ``False``, and a single
datetime leaking in from a driver makes an activity silently unmatched in
``compare()``. ``core/units.as_date`` guards the boundary; this guards the store.

**Durations are integer days.** Hours belong to the file formats, and the one
conversion site is ``core/units.days_from_hours``.

**Every domain row carries ``organization_id``.** There is no auth yet, and a
single default organisation is created at migration time -- but the column is
here from the first migration and every query goes through ``scoped()``.
Retrofitting a tenancy column means a migration over live data plus an audit of
every query written before it existed, and one missed filter is a data breach.
"""

from __future__ import annotations

import uuid
from datetime import date, datetime, timezone

from sqlalchemy import Date, DateTime, Integer, String
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


def new_id() -> str:
    """A 32-character hex id.

    Opaque and non-sequential: a sequential integer in a URL tells a competitor
    how many projects exist and lets them walk the range.
    """
    return uuid.uuid4().hex


def utcnow() -> datetime:
    return datetime.now(tz=timezone.utc)


def pk_column() -> Mapped[str]:
    return mapped_column(String(32), primary_key=True, default=new_id)


def org_column() -> Mapped[str]:
    """The tenant boundary. Indexed because every query filters on it."""
    return mapped_column(String(32), index=True, nullable=False)


def date_column(*, nullable: bool = True) -> Mapped[date | None]:
    return mapped_column(Date, nullable=nullable)


def days_column(*, default: int | None = 0, nullable: bool = False) -> Mapped[int]:
    return mapped_column(Integer, default=default, nullable=nullable)


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )
