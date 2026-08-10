"""Who is calling, in which organisation, and what they may do.

Framework-agnostic on purpose: it takes a ``Principal`` and answers questions.
The Flask layer builds the principal from a session cookie or a bearer key and
passes it in; massing would build one from its own auth and pass it in too.

The one rule that matters more than the rest: **an unauthorised read is a 404,
not a 403.** "This exists but is not yours" tells one contractor that another
contractor's project id is real, and a sequential scan then maps their portfolio.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from ..models.identity import Permission, Role


@dataclass(frozen=True)
class Principal:
    """An authenticated caller, resolved for one request.

    Not a user record: a *claim* about one, valid for this request only. Roles
    are re-read per request so a revoked membership takes effect immediately
    rather than at the next sign-in.
    """

    subject_id: str
    label: str
    organization_id: str | None
    role: Role | None
    permissions: frozenset[Permission] = field(default_factory=frozenset)
    #: How the caller proved who they are. Recorded because an API key and a
    #: browser session warrant different treatment -- CSRF applies to one and
    #: not the other, and the audit trail should say which was used.
    via: str = "session"

    @property
    def is_authenticated(self) -> bool:
        return bool(self.subject_id)

    def can(self, permission: Permission) -> bool:
        return permission in self.permissions

    def to_dict(self) -> dict[str, object]:
        return {
            "subject_id": self.subject_id,
            "label": self.label,
            "organization_id": self.organization_id,
            "role": self.role.value if self.role else None,
            "via": self.via,
        }


#: The caller who has not signed in. A real object rather than `None`, so every
#: call site goes through the same `can()` and none of them has to remember a
#: null check that fails open.
ANONYMOUS = Principal(subject_id="", label="anonymous", organization_id=None, role=None)


class NotAuthenticated(Exception):  # noqa: N818 - the API's vocabulary; see api/errors.py
    """No credential was presented. The caller should sign in."""


class NotPermitted(Exception):  # noqa: N818
    """A credential was presented and does not carry the permission.

    Distinct from :class:`NotAuthenticated` because the remedies differ: one is
    "sign in", the other is "ask an admin". Both are surfaced as 404 on a
    *resource* read; this is for actions where the caller's identity is already
    known and hiding the endpoint's existence gains nothing.
    """

    def __init__(self, permission: Permission) -> None:
        super().__init__(f"this account does not have {permission.value}")
        self.permission = permission


def require(principal: Principal, permission: Permission) -> None:
    if not principal.is_authenticated:
        raise NotAuthenticated
    if not principal.can(permission):
        raise NotPermitted(permission)


def principal_for_user(user: object, organization_id: str) -> Principal:
    from .accounts import permissions_for

    membership = user.membership_in(organization_id)  # type: ignore[attr-defined]
    return Principal(
        subject_id=user.id,  # type: ignore[attr-defined]
        label=user.display_name or user.email,  # type: ignore[attr-defined]
        organization_id=organization_id,
        role=membership.role if membership else None,
        permissions=permissions_for(user, organization_id),  # type: ignore[arg-type]
        via="session",
    )


def principal_for_api_key(record: object) -> Principal:
    from ..models.identity import ROLE_PERMISSIONS

    return Principal(
        subject_id=record.id,  # type: ignore[attr-defined]
        label=f"key {record.prefix}...",  # type: ignore[attr-defined]
        organization_id=record.organization_id,  # type: ignore[attr-defined]
        role=record.role,  # type: ignore[attr-defined]
        permissions=ROLE_PERMISSIONS[record.role],  # type: ignore[attr-defined]
        via="api_key",
    )
