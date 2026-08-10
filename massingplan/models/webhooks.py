"""Outbound webhooks: the endpoints, and the outbox that feeds them.

Three decisions worth naming.

**Delivery goes through a table, not straight out of the request.** Posting
inline means a subscriber whose endpoint hangs for thirty seconds makes *your*
`set baseline` take thirty seconds, and a subscriber who is down loses the event
entirely with nothing recording that it happened. The row is written in the same
transaction as the change it describes, so an event cannot exist for a baseline
that rolled back, nor a baseline exist with no event queued.

**Attempts are recorded, not just outcomes.** "It was delivered" and "it was
delivered on the fourth try, ninety minutes late" are different facts, and the
second is the one that explains why a subscriber's data looked stale.

**The response body is truncated hard.** A subscriber's error page can contain
their own session cookie, an internal hostname or a stack trace. Storing the
whole thing turns our audit trail into a copy of their leak.
"""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from sqlalchemy import JSON, Boolean, ForeignKey, Index, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .base import Base, DateTime, TimestampMixin, org_column, pk_column
from .schedule import enum_column

#: How much of a subscriber's response we keep. Enough to tell "404 not found"
#: from "500 template error"; not enough to be a copy of their stack trace.
RESPONSE_EXCERPT_CHARS = 500


class WebhookEvent(str, Enum):
    """What a subscriber can ask for.

    A closed set, checked on subscribe. An open string field means a typo
    produces a subscription that silently never fires, and the subscriber blames
    the sender.
    """

    PROJECT_CREATED = "project.created"
    PROJECT_DELETED = "project.deleted"
    BASELINE_SET = "baseline.set"
    IMPORT_COMPLETED = "import.completed"
    SCHEDULE_SLIPPED = "schedule.slipped"


class DeliveryStatus(str, Enum):
    PENDING = "pending"
    DELIVERED = "delivered"
    #: Retries exhausted. Terminal, and deliberately distinct from `pending`:
    #: a queue where failures stay pending forever is a queue nobody can drain.
    FAILED = "failed"


class Webhook(Base, TimestampMixin):
    """One subscriber endpoint.

    The signing secret is stored encrypted where a key is configured and in the
    clear where it is not, rather than refusing to work at all -- see
    `services/webhooks.py`, which owns that decision and says why.
    """

    __tablename__ = "webhooks"
    __table_args__ = (Index("ix_webhook_org_active", "organization_id", "is_active"),)

    id: Mapped[str] = pk_column()
    organization_id: Mapped[str] = org_column()
    created_by_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    name: Mapped[str] = mapped_column(String(120), default="", nullable=False)
    url: Mapped[str] = mapped_column(String(2000), nullable=False)
    #: The signing secret, as stored. Read it through `webhooks.secret_of()`,
    #: never directly -- it may be ciphertext.
    secret_stored: Mapped[str] = mapped_column(Text, nullable=False)
    #: Whether `secret_stored` is ciphertext. Recorded rather than inferred,
    #: because inferring it from the shape of the string is how a plaintext
    #: secret that happens to look like a Fernet token gets "decrypted".
    secret_encrypted: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    #: Subscribed event names. A list, not a bitmask: adding an event must not
    #: renumber the ones already stored.
    events: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    #: Set when deliveries keep failing. The endpoint is not deleted -- a
    #: subscriber who fixes their server wants their subscription back, and
    #: silently dropping it means they never learn it was dropped.
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    disabled_reason: Mapped[str] = mapped_column(String(300), default="", nullable=False)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    deliveries: Mapped[list[WebhookDelivery]] = relationship(
        back_populates="webhook", cascade="all, delete-orphan", passive_deletes=True
    )

    def wants(self, event: str) -> bool:
        return self.is_active and event in (self.events or [])


class WebhookDelivery(Base, TimestampMixin):
    """One event, queued for one endpoint.

    Rows per (event x endpoint) rather than per event: two subscribers to the
    same event fail independently, and a shared row cannot express "delivered to
    one, still retrying the other".
    """

    __tablename__ = "webhook_deliveries"
    __table_args__ = (
        # The drain query. Without it, picking up work is a full scan that gets
        # slower every day the table grows.
        Index("ix_delivery_due", "status", "next_attempt_at"),
        Index("ix_delivery_org_created", "organization_id", "created_at"),
    )

    id: Mapped[str] = pk_column()
    organization_id: Mapped[str] = org_column()
    webhook_id: Mapped[str] = mapped_column(
        ForeignKey("webhooks.id", ondelete="CASCADE"), index=True, nullable=False
    )
    event: Mapped[str] = mapped_column(String(60), nullable=False)
    #: The body, stored as sent. Regenerating it at delivery time would mean a
    #: retry can carry different content from the attempt it is retrying.
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    status: Mapped[DeliveryStatus] = mapped_column(
        enum_column(DeliveryStatus, "delivery_status"),
        default=DeliveryStatus.PENDING,
        nullable=False,
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    response_status: Mapped[int | None] = mapped_column(Integer, nullable=True)
    #: Truncated to RESPONSE_EXCERPT_CHARS. See the module docstring.
    response_excerpt: Mapped[str] = mapped_column(String(600), default="", nullable=False)
    error: Mapped[str] = mapped_column(String(300), default="", nullable=False)

    webhook: Mapped[Webhook] = relationship(back_populates="deliveries")
