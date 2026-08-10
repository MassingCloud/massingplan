"""The default: identity comes from this deployment, not from anywhere else.

Currently a single principal derived from an API key, because there is no user
model yet. It is written as a real provider rather than a stub so the seam is
exercised by the default path -- an adapter interface only ever used by the
optional implementation is an interface nobody has tested.
"""

from __future__ import annotations

from ...security import verify_api_key
from .base import IdentityProvider, Principal

#: The role a key-authenticated caller gets. One role until there is a user
#: model to hang more off; naming it now keeps the check sites honest.
DEFAULT_ROLE = "scheduler"


class LocalIdentityProvider(IdentityProvider):
    name = "local"

    def __init__(self, key_hashes: dict[str, str] | None = None) -> None:
        #: ``{subject: key_hash}``. Hashes, never keys.
        self._keys = dict(key_hashes or {})

    def authenticate(self, credentials: dict[str, str]) -> Principal | None:
        key = credentials.get("api_key", "")
        if not key:
            return None
        for subject, expected in self._keys.items():
            if verify_api_key(key, expected):
                return Principal(
                    subject=subject,
                    display_name=subject,
                    roles=frozenset({DEFAULT_ROLE}),
                )
        return None

    def describe(self) -> dict[str, object]:
        return {"provider": self.name, "configured_keys": len(self._keys)}
