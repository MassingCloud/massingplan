"""One row per (key, window) for the shared rate-limit counter.

Deliberately not carrying `organization_id`, unlike every other table here: a
rate-limit key is often a client address on an *unauthenticated* endpoint, and
there is no tenant to attribute it to. Making the column nullable to fit the
convention would put a nullable tenant id on a table `repository.scoped` must
never touch, which is a worse shape than an honest exception.

Nothing in it is durable data. It is a counter with a lifetime measured in
minutes, and `DatabaseStore.prune` deletes windows that have closed.
"""

from __future__ import annotations

from sqlalchemy import BigInteger, Index, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from .base import Base, pk_column


class RateLimitHit(Base):
    """Hits against one key inside one fixed window."""

    __tablename__ = "rate_limit_hits"
    __table_args__ = (
        # The upsert target. Without the unique constraint two workers racing
        # the same key insert two rows and each counts half the traffic --
        # which is the per-process bug this table exists to remove, wearing a
        # different hat.
        UniqueConstraint("key", "window_start", name="uq_rate_limit_window"),
        Index("ix_rate_limit_window_start", "window_start"),
    )

    id: Mapped[str] = pk_column()
    #: `endpoint:identity`, built by `RateLimiter.check`.
    key: Mapped[str] = mapped_column(String(320), nullable=False)
    #: The window's start as whole seconds of **wall-clock** time. Monotonic
    #: clocks have a per-process origin, so two workers would disagree about
    #: which window a given instant belongs to -- see `RateLimiter.check`.
    window_start: Mapped[int] = mapped_column(BigInteger, nullable=False)
    hits: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
