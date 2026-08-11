"""Who the caller is.

``OidcProvider`` is deliberately *generic*: massing.cloud is one config block,
not a special case. The moment the seam is shaped around one identity provider,
the second one needs a second seam.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field


class IdentityError(RuntimeError):
    """A sign-in that failed a check at the provider.

    Declared on the *base* rather than in an implementation so a caller can
    catch it without importing the implementation -- which is the whole point
    of the seam, and which import-linter enforces: `blueprints` reaching
    straight into `identity.oidc` would mean a deployment missing that adapter
    got an `ImportError` at request time instead of the actionable message
    `resolve()` raises.
    """


@dataclass(frozen=True)
class Principal:
    """An authenticated caller. Not a user record -- a claim about one."""

    subject: str
    email: str | None = None
    display_name: str = ""
    organization_id: str | None = None
    roles: frozenset[str] = field(default_factory=frozenset)

    def has_role(self, role: str) -> bool:
        return role in self.roles


class IdentityProvider(ABC):
    name: str = "base"

    @abstractmethod
    def authenticate(self, credentials: dict[str, str]) -> Principal | None:
        """The principal, or ``None``. Never a partially-authenticated one."""

    @abstractmethod
    def describe(self) -> dict[str, object]:
        """Enough for an operator to see which provider is live, and no secrets."""
