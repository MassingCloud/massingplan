"""The persistence layer.

Every domain row carries `organization_id`, and every read goes through
`services/repository.scoped`, which fails closed when no organisation is active.
There is no auth yet -- a single default organisation is created by the first
migration -- but the mechanism is exercised by the default path, because a
tenancy filter first used the day auth lands is a tenancy filter nobody has
tested.
"""

from .base import Base, TimestampMixin, new_id, utcnow
from .identity import (
    ROLE_PERMISSIONS,
    ApiKey,
    AuditEvent,
    Membership,
    Permission,
    Role,
    User,
)
from .locations import LinearActivity, LinearQuantity, ProjectLocation
from .schedule import (
    Activity,
    Assignment,
    Baseline,
    BaselineActivity,
    Calendar,
    CalendarException,
    ImportJob,
    Organization,
    Project,
    Relationship,
    Resource,
)
from .webhooks import DeliveryStatus, Webhook, WebhookDelivery, WebhookEvent

__all__ = [
    "ROLE_PERMISSIONS",
    "Activity",
    "ApiKey",
    "Assignment",
    "AuditEvent",
    "Base",
    "Baseline",
    "BaselineActivity",
    "Calendar",
    "CalendarException",
    "DeliveryStatus",
    "ImportJob",
    "LinearActivity",
    "LinearQuantity",
    "Membership",
    "Organization",
    "Permission",
    "Project",
    "ProjectLocation",
    "Relationship",
    "Resource",
    "Role",
    "TimestampMixin",
    "User",
    "Webhook",
    "WebhookDelivery",
    "WebhookEvent",
    "new_id",
    "utcnow",
]
