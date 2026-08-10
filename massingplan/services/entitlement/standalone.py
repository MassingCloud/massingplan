"""The default: everything is allowed, because you are running it yourself.

Standalone is the product, not a trial. A self-hoster who has the source has
already got every feature; metering them would be theatre, and code that exists
only to be bypassed rots.
"""

from __future__ import annotations

from .base import UNLIMITED, Entitlement, EntitlementProvider


class StandaloneProvider(EntitlementProvider):
    name = "standalone"

    def current(self, organization_id: str | None = None) -> Entitlement:
        return Entitlement(
            tier="standalone",
            entitled=True,
            status="active",
            expires_at=None,
            seats={"limit": UNLIMITED, "used": 0},
            limits={},
        )
