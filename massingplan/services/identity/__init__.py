"""Identity adapters, resolved by name."""

from __future__ import annotations

from typing import Any

from ..entitlement.base import AdapterUnavailableError
from .base import IdentityError, IdentityProvider, Principal
from .local import LocalIdentityProvider

__all__ = [
    "AdapterUnavailableError",
    "IdentityError",
    "IdentityProvider",
    "LocalIdentityProvider",
    "Principal",
    "resolve",
]


def resolve(backend: str, **kwargs: Any) -> IdentityProvider:
    if backend == "local":
        return LocalIdentityProvider(**kwargs)
    if backend == "oidc":
        try:
            from .oidc import OidcProvider
        except ImportError as exc:
            raise AdapterUnavailableError(
                "The OIDC identity adapter is not installed. "
                "Install it with: pip install 'massingplan[oidc]'"
            ) from exc
        return OidcProvider(**kwargs)
    raise AdapterUnavailableError(
        f"unknown identity backend {backend!r}; expected 'local' or 'oidc'"
    )
