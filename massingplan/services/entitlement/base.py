"""What a deployment is allowed to do, and how much of it.

The ``Entitlement`` shape mirrors massing.cloud's object field for field
(SPEC.md 3.2). That costs nothing to honour now and is the whole integration
surface later: a massing.cloud adapter fills the same dataclass from an HTTP
call, and nothing above this line changes.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import date

#: Following the massing convention: -1 is unlimited, not "unset".
UNLIMITED = -1


@dataclass(frozen=True)
class Entitlement:
    tier: str
    entitled: bool
    status: str
    expires_at: date | None = None
    seats: dict[str, int] = field(default_factory=lambda: {"limit": UNLIMITED, "used": 0})
    limits: dict[str, int] = field(default_factory=dict)

    def allows(self, feature: str) -> bool:
        """``UNLIMITED`` and an absent key both mean yes.

        Absent means the tier does not meter that feature. Treating an unknown
        key as a denial would make every new feature invisible to every existing
        deployment until its config was updated.
        """
        limit = self.limits.get(feature, UNLIMITED)
        return self.entitled and limit != 0

    def remaining(self, feature: str) -> int:
        return self.limits.get(feature, UNLIMITED)

    def to_dict(self) -> dict[str, object]:
        return {
            "tier": self.tier,
            "entitled": self.entitled,
            "status": self.status,
            "expires_at": self.expires_at.isoformat() if self.expires_at else None,
            "seats": dict(self.seats),
            "limits": dict(self.limits),
        }


class AdapterUnavailableError(RuntimeError):
    """An adapter was selected that this install does not have."""


class EntitlementProvider(ABC):
    """The seam. Two implementations: local, and massing.cloud."""

    name: str = "base"

    @abstractmethod
    def current(self, organization_id: str | None = None) -> Entitlement:
        """What this organisation may do right now."""
