"""Entitlement adapters, resolved by name.

The remote adapter is imported lazily inside the branch that needs it, so a
minimal install never touches it -- and the `no-adapters` CI job can delete the
file and watch the suite stay green.
"""

from __future__ import annotations

from .base import UNLIMITED, AdapterUnavailableError, Entitlement, EntitlementProvider
from .standalone import StandaloneProvider

__all__ = [
    "UNLIMITED",
    "AdapterUnavailableError",
    "Entitlement",
    "EntitlementProvider",
    "StandaloneProvider",
    "resolve",
]


def resolve(backend: str) -> EntitlementProvider:
    if backend == "standalone":
        return StandaloneProvider()
    if backend == "massing_cloud":
        try:
            from .massing_cloud import MassingCloudProvider
        except ImportError as exc:
            raise AdapterUnavailableError(
                "The massing.cloud entitlement adapter is not installed. "
                "Install it with: pip install 'massingplan[oidc]'"
            ) from exc
        return MassingCloudProvider()
    raise AdapterUnavailableError(
        f"unknown entitlement backend {backend!r}; expected 'standalone' or 'massing_cloud'"
    )
