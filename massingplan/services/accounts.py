"""Sign-up, sign-in, membership and API keys.

The security decisions live here rather than in a route, so there is one place
to read them and one place they can be got wrong.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from enum import Enum

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Organization
from ..models.identity import ApiKey, AuditEvent, Membership, Permission, Role, User
from ..security import generate_api_key, hash_api_key

#: Attempts before an account locks. Low enough to stop credential stuffing,
#: high enough that a person mistyping twice is not locked out of their own job.
MAX_FAILED_SIGN_INS = 8
LOCKOUT = timedelta(minutes=15)


class SignInOutcome(str, Enum):
    OK = "ok"
    BAD_CREDENTIALS = "bad_credentials"
    #: Named separately from BAD_CREDENTIALS on purpose. A locked-out user told
    #: "wrong password" keeps trying, stays locked, and raises a support ticket
    #: about a bug that does not exist.
    LOCKED = "locked"
    INACTIVE = "inactive"


@dataclass(frozen=True)
class SignInResult:
    outcome: SignInOutcome
    user: User | None = None
    retry_after_seconds: int = 0

    @property
    def ok(self) -> bool:
        return self.outcome is SignInOutcome.OK


class AccountError(RuntimeError):
    """A sign-up or membership change that cannot be applied."""


# -- password hashing ------------------------------------------------------
#
# argon2id, via argon2-cffi. Imported lazily and behind a clear failure, because
# `core` and the engine tests must keep working on an install that has no
# password hashing at all.


# Parameters, not defaults-by-accident. ~64MB and three passes is the
# OWASP-recommended floor at the time of writing and costs roughly 50ms per
# sign-in, which is the point: it has to be slow.
TIME_COST = 3
MEMORY_COST_KIB = 65536
PARALLELISM = 2

#: What this module *ships* with, captured at import so a harness that lowers
#: the cost cannot also lower the definition of the floor. `test_auth.py`
#: asserts this tuple, so weakening production hashing fails a test even while
#: the suite itself runs cheaply.
SHIPPED_PARAMETERS = (TIME_COST, MEMORY_COST_KIB, PARALLELISM)


def _hasher():  # type: ignore[no-untyped-def]
    """The password hasher.

    The three constants above are read at call time rather than baked in, so a
    test suite can turn the cost down. That is not a nicety: at 64MiB per hash
    and several hundred registrations, the suite allocates tens of gigabytes
    over its run and fails outright on a machine under memory pressure with
    `argon2.exceptions.HashingError: Memory allocation error` -- which looks
    like a code defect and is not one.
    """
    try:
        from argon2 import PasswordHasher
    except ImportError as exc:  # pragma: no cover - exercised by the no-extras job
        raise AccountError(
            "password hashing needs argon2-cffi. Install it with: pip install 'massingplan[auth]'"
        ) from exc
    return PasswordHasher(time_cost=TIME_COST, memory_cost=MEMORY_COST_KIB, parallelism=PARALLELISM)


#: How many password hashes may be in flight at once, per process.
#:
#: The rate limiter bounds hashes **per fifteen minutes**; this bounds them
#: **at the same instant**, and the two are not the same guard. Twenty sign-in
#: attempts arriving together all pass a limit of twenty per window and then all
#: enter argon2 simultaneously -- twenty times 64MiB is 1.3GB of allocation, at
#: once, from one client. Distinct source addresses lift even that bound.
#:
#: The cost is deliberate and must not be lowered; what was missing is that a
#: *deliberately expensive* operation with no concurrency bound is a memory
#: amplifier pointed at the server. Queueing is the right trade: a sign-in that
#: waits is a slow sign-in, and a sign-in that cannot allocate is a 500 -- which
#: this repo has already seen, as `HashingError: Memory allocation error`, and
#: mistook for a code defect because on a loaded machine it looks like one.
#:
#: Four, so the ceiling is ~256MiB of hashing whatever arrives. Overridable for
#: an operator who has sized their box and wants more throughput.
MAX_CONCURRENT_HASHES = int(os.getenv("MASSINGPLAN_MAX_CONCURRENT_HASHES", "4"))

#: Module-level and deliberately not per-app: the bound protects the *process*
#: address space, which is shared by every app instance a worker happens to
#: hold. A per-app semaphore would be a bound per app object, which is not the
#: resource being protected.
_hash_slots = threading.BoundedSemaphore(max(1, MAX_CONCURRENT_HASHES))


def hash_password(password: str) -> str:
    if len(password) < 12:
        # Length, not composition rules. Forced symbols produce `P@ssw0rd!` and
        # a sticky note; length is the only requirement that reliably helps.
        raise AccountError("a password must be at least 12 characters")
    with _hash_slots:
        return str(_hasher().hash(password))


def verify_password(password: str, password_hash: str) -> bool:
    from argon2.exceptions import VerificationError, VerifyMismatchError

    with _hash_slots:
        try:
            return bool(_hasher().verify(password_hash, password))
        except (VerifyMismatchError, VerificationError):
            return False


def needs_rehash(password_hash: str) -> bool:
    """True when the stored hash used weaker parameters than we now use."""
    return bool(_hasher().check_needs_rehash(password_hash))


# -- sign-up and sign-in ---------------------------------------------------


def register(
    session: Session,
    *,
    email: str,
    password: str,
    display_name: str = "",
    organization_id: str,
    role: Role = Role.OWNER,
) -> User:
    """Create a user and give them a role in one organisation."""
    email = email.strip().lower()
    if "@" not in email or len(email) < 3:
        raise AccountError(f"{email!r} is not an email address")

    existing = session.scalars(
        select(Membership).where(
            Membership.organization_id == organization_id, Membership.email == email
        )
    ).first()
    if existing is not None:
        raise AccountError("an account with that email already exists in this organisation")

    user = User(
        email=email, display_name=display_name or email, password_hash=hash_password(password)
    )
    session.add(user)
    session.flush()
    session.add(
        Membership(user_id=user.id, organization_id=organization_id, email=email, role=role)
    )
    session.flush()
    return user


def find_by_sso_subject(session: Session, subject: str) -> User | None:
    """The user this external identity belongs to, or `None`.

    By subject, never by email: see `User.sso_subject` for why matching on the
    address hands a reallocated mailbox somebody else's projects.
    """
    if not subject:
        return None
    return session.scalars(select(User).where(User.sso_subject == subject)).first()


def provision_sso_user(
    session: Session,
    *,
    subject: str,
    email: str | None,
    display_name: str,
    organization_name: str,
) -> tuple[User, str]:
    """Create an account for a verified external identity, in a **new** tenant.

    Never joins an existing organisation. A verified assertion that somebody
    controls an email address at an identity provider is not a statement about
    who they work for -- and this repo has already shipped the version of that
    mistake where self-service registration made strangers owners of the
    default organisation. Joining an existing tenant is by invitation, which is
    a different route with a different check.

    The password hash is a fresh random value nobody holds. Not empty, and not
    a fixed sentinel: `attempt_sign_in` must be unable to match it, and it must
    not be the *same* unmatched value across every SSO account.
    """
    if not subject:
        raise AccountError("an SSO account needs a subject")
    if find_by_sso_subject(session, subject) is not None:
        raise AccountError("that external identity already has an account")

    label = (email or display_name or subject).strip()
    slug_source = (organization_name or label.split("@")[0] or "sso").lower()
    slug = "".join(ch if ch.isalnum() or ch == "-" else "-" for ch in slug_source)[:80].strip("-")
    slug = slug or "sso"

    existing = session.scalars(select(Organization).where(Organization.slug == slug)).first()
    if existing is not None:
        # Suffixed rather than joined. Colliding on a name must not be a way to
        # get into somebody else's tenant.
        for n in range(2, 1000):
            candidate = f"{slug}-{n}"[:80]
            if (
                session.scalars(select(Organization).where(Organization.slug == candidate)).first()
                is None
            ):
                slug = candidate
                break
        else:
            raise AccountError("cannot find a free organisation name")

    organization = Organization(name=organization_name or label, slug=slug)
    session.add(organization)
    session.flush()

    user = User(
        email=(email or f"{subject}@sso.invalid").strip().lower()[:320],
        display_name=display_name or label,
        password_hash=hash_password(secrets.token_urlsafe(32)),
        sso_subject=subject,
    )
    session.add(user)
    session.flush()
    session.add(
        Membership(
            user_id=user.id,
            organization_id=organization.id,
            email=user.email,
            role=Role.OWNER,
        )
    )
    session.flush()
    return user, organization.id


def attempt_sign_in(session: Session, *, email: str, password: str) -> SignInResult:
    """Verify a credential, with lock-out and a constant-ish cost on failure."""
    email = email.strip().lower()
    membership = session.scalars(select(Membership).where(Membership.email == email)).first()
    user = session.get(User, membership.user_id) if membership else None

    if user is None:
        # Hash anyway. Returning immediately for an unknown address makes the
        # response measurably faster than for a known one, which turns sign-in
        # into an account-enumeration oracle.
        with contextlib.suppress(AccountError):
            _hasher().hash("not-a-real-password-just-burning-time")
        return SignInResult(SignInOutcome.BAD_CREDENTIALS)

    now = datetime.now(tz=timezone.utc)
    if user.locked_until and user.locked_until > now:
        return SignInResult(
            SignInOutcome.LOCKED,
            retry_after_seconds=int((user.locked_until - now).total_seconds()),
        )
    if not user.is_active:
        return SignInResult(SignInOutcome.INACTIVE)

    if not verify_password(password, user.password_hash):
        user.failed_sign_ins += 1
        if user.failed_sign_ins >= MAX_FAILED_SIGN_INS:
            user.locked_until = now + LOCKOUT
            user.failed_sign_ins = 0
        session.flush()
        return SignInResult(SignInOutcome.BAD_CREDENTIALS)

    if needs_rehash(user.password_hash):
        # Upgrade on the one occasion the plaintext is legitimately in hand.
        user.password_hash = hash_password(password)
    user.failed_sign_ins = 0
    user.locked_until = None
    user.last_seen_at = now
    session.flush()
    return SignInResult(SignInOutcome.OK, user=user)


def organizations_for(session: Session, user: User) -> list[Organization]:
    ids = [m.organization_id for m in user.memberships]
    if not ids:
        return []
    return list(session.scalars(select(Organization).where(Organization.id.in_(ids))).all())


def permissions_for(user: User, organization_id: str) -> frozenset[Permission]:
    """What this user may do here, right now.

    Resolved per request rather than cached on the session: a role change has to
    take effect on the next request, not at the next sign-in.
    """
    membership = user.membership_in(organization_id)
    return membership.permissions if membership else frozenset()


# -- API keys --------------------------------------------------------------


def issue_api_key(
    session: Session,
    *,
    organization_id: str,
    name: str,
    role: Role = Role.PLANNER,
    created_by: User | None = None,
) -> tuple[str, ApiKey]:
    """Returns ``(plaintext, record)``. The plaintext is shown once and not stored."""
    plaintext, key_hash = generate_api_key()
    record = ApiKey(
        organization_id=organization_id,
        created_by_id=created_by.id if created_by else None,
        name=name,
        key_hash=key_hash,
        prefix=plaintext[:12],
        role=role,
    )
    session.add(record)
    session.flush()
    return plaintext, record


def authenticate_api_key(session: Session, presented: str) -> ApiKey | None:
    """Look a key up by hash. Constant-time by construction: it is an index hit.

    Comparing hashes row by row would be both slow and timing-leaky; hashing the
    presented key and looking up the digest is neither.
    """
    if not presented:
        return None
    record = session.scalars(
        select(ApiKey).where(ApiKey.key_hash == hash_api_key(presented))
    ).first()
    if record is None or not record.is_active:
        return None
    record.last_used_at = datetime.now(tz=timezone.utc)
    return record


def revoke_api_key(session: Session, record: ApiKey) -> None:
    record.revoked_at = datetime.now(tz=timezone.utc)
    session.flush()


# -- audit -----------------------------------------------------------------


def audit(
    session: Session,
    *,
    organization_id: str,
    action: str,
    actor_id: str | None = None,
    actor_label: str = "",
    subject_type: str = "",
    subject_id: str = "",
    summary: str = "",
    detail: dict | None = None,
) -> AuditEvent:
    """Record something that happened. Append-only; there is no update path."""
    event = AuditEvent(
        organization_id=organization_id,
        at=datetime.now(tz=timezone.utc),
        actor_id=actor_id,
        actor_label=actor_label,
        action=action,
        subject_type=subject_type,
        subject_id=subject_id,
        summary=summary,
        detail=detail or {},
    )
    session.add(event)
    session.flush()
    return event


def bootstrap_demo_account(session: Session, *, organization_id: str) -> tuple[User, str] | None:
    """A signed-in demo user, for a fresh local install.

    Returns ``None`` if one already exists. The password is random and printed
    once by the CLI rather than being a well-known default -- a documented
    default password is a documented vulnerability the moment somebody exposes
    the port.
    """
    existing = session.scalars(
        select(Membership).where(Membership.organization_id == organization_id)
    ).first()
    if existing is not None:
        return None
    password = secrets.token_urlsafe(18)
    user = register(
        session,
        email="demo@massingplan.local",
        password=password,
        display_name="Demo planner",
        organization_id=organization_id,
        role=Role.OWNER,
    )
    return user, password
