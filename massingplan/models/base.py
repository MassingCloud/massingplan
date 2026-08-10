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

from sqlalchemy import Date, Integer, String, TypeDecorator
from sqlalchemy import DateTime as SaDateTime
from sqlalchemy.engine import Dialect
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class UtcDateTime(TypeDecorator[datetime]):
    """A timestamp that is always timezone-aware UTC on the way out.

    SQLite has no timestamp type and hands back a *naive* datetime whatever
    `DateTime(timezone=True)` claims. Every comparison against
    `datetime.now(tz=utc)` then raises `TypeError: can't compare offset-naive
    and offset-aware datetimes` -- and it raises only after a round trip through
    the database, so it passes every test that keeps the object in memory.

    Found by the account-lockout check, where the consequence is a 500 on the
    sign-in page for exactly the users who most need to get in.
    """

    impl = SaDateTime(timezone=True)
    cache_ok = True

    def process_bind_param(self, value: datetime | None, _dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        # Naive in means "the caller meant UTC". Storing it unmarked is how the
        # ambiguity gets baked in.
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    def process_result_value(self, value: datetime | None, _dialect: Dialect) -> datetime | None:
        if value is None:
            return None
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)


#: Used everywhere a timestamp is stored. Aliased so a model reads naturally.
DateTime = UtcDateTime


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
