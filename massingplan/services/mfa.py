"""Time-based one-time passwords, and the recovery path when the phone is gone.

TOTP because it needs no SMS gateway, no vendor and no network at verification
time — the three things that make a second factor either expensive or a new
dependency to be down.

Two decisions worth naming:

**The QR is an inline SVG, not an image URL.** Every "generate a QR" service is
a third party you have just shown a TOTP secret to, and pointing at one would
also force a hole in `default-src 'self'`. `segno` renders SVG markup inline.

**A used recovery code is consumed, not merely checked.** A code that still
works after use is a password with extra steps.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy.orm import Session

from ..models.identity import User
from . import crypto

#: How many 30-second steps either side of now are accepted. One step covers
#: ordinary clock drift and a user who starts typing at second 29. Widening it
#: to please a badly-set phone widens the replay window for everyone.
DRIFT_WINDOWS = 1

ISSUER = "massingplan"


class MfaError(RuntimeError):
    """Enrolment or verification could not proceed."""


@dataclass(frozen=True)
class Enrolment:
    secret: str
    uri: str
    qr_svg: str
    recovery_codes: list[str]


def _totp(secret: str):  # type: ignore[no-untyped-def]
    try:
        import pyotp
    except ImportError as exc:  # pragma: no cover - the no-extras path
        raise MfaError(
            "TOTP needs `pyotp`. Install it with: pip install 'massingplan[mfa]'"
        ) from exc
    return pyotp.TOTP(secret)


def is_available() -> bool:
    """Whether MFA can be offered at all. Reported by `massingplan check`."""
    try:
        import pyotp  # noqa: F401
        import segno  # noqa: F401
    except ImportError:
        return False
    return crypto.is_available()


def begin_enrolment(user: User, *, issuer: str = ISSUER) -> Enrolment:
    """A secret, a provisioning URI, an inline QR, and recovery codes.

    Nothing is stored yet. The secret is only persisted once the user has proved
    they can produce a code from it — otherwise a failed enrolment leaves an
    account with a second factor nobody can satisfy, and the only fix is an
    administrator.
    """
    try:
        import pyotp
        import segno
    except ImportError as exc:  # pragma: no cover
        raise MfaError(
            "TOTP needs `pyotp` and `segno`. Install with: pip install 'massingplan[mfa]'"
        ) from exc

    secret = pyotp.random_base32()
    uri = pyotp.TOTP(secret).provisioning_uri(name=user.email, issuer_name=issuer)

    # segno writes bytes even for SVG, so this is a BytesIO decoded afterwards
    # rather than a StringIO -- the latter raises `string argument expected,
    # got 'bytes'` at enrolment time and nowhere earlier.
    import io

    buffer = io.BytesIO()
    segno.make(uri, error="m").save(buffer, kind="svg", xmldecl=False, svgns=True, scale=4)
    return Enrolment(
        secret=secret,
        uri=uri,
        qr_svg=buffer.getvalue().decode("utf-8"),
        recovery_codes=crypto.generate_recovery_codes(),
    )


def confirm_enrolment(
    session: Session, user: User, *, secret: str, code: str, recovery_codes: list[str]
) -> None:
    """Store the secret, encrypted, once a code from it has verified.

    The recovery codes are stored as hashes. Keeping them readable would make
    the recovery list a second copy of the second factor.
    """
    if not verify_code(secret, code):
        raise MfaError("that code did not match. Check the time on your phone and try again.")
    user.mfa_secret_encrypted = crypto.encrypt(secret)
    user.mfa_recovery_hashes = [crypto.hash_recovery_code(c) for c in recovery_codes]
    user.mfa_enabled_at = datetime.now(tz=timezone.utc)
    session.flush()


def disable(session: Session, user: User) -> None:
    user.mfa_secret_encrypted = None
    user.mfa_recovery_hashes = []
    user.mfa_enabled_at = None
    session.flush()


def is_enabled(user: User) -> bool:
    return bool(user.mfa_secret_encrypted)


def verify_code(secret: str, code: str) -> bool:
    cleaned = (code or "").strip().replace(" ", "")
    if not cleaned.isdigit():
        return False
    return bool(_totp(secret).verify(cleaned, valid_window=DRIFT_WINDOWS))


def verify_for_user(session: Session, user: User, code: str) -> bool:
    """A TOTP code, or a recovery code -- which is consumed on use.

    Also refuses to accept the *same* TOTP code twice inside its window. Without
    that, a code shoulder-surfed or captured from a proxy is replayable for up
    to ninety seconds, which is exactly as long as an attacker needs.
    """
    if not user.mfa_secret_encrypted:
        return False

    cleaned = crypto.normalise_recovery_code(code)
    hashed = crypto.hash_recovery_code(cleaned)
    if hashed in (user.mfa_recovery_hashes or []):
        # Consumed. A recovery code that still works after use is a password
        # with extra steps.
        user.mfa_recovery_hashes = [h for h in user.mfa_recovery_hashes if h != hashed]
        session.flush()
        return True

    secret = crypto.decrypt(user.mfa_secret_encrypted)
    if not verify_code(secret, code):
        return False

    now = datetime.now(tz=timezone.utc)
    digits = (code or "").strip().replace(" ", "")
    if (
        user.mfa_last_code == digits
        and user.mfa_last_code_at is not None
        and now - user.mfa_last_code_at < timedelta(seconds=90)
    ):
        return False
    user.mfa_last_code = digits
    user.mfa_last_code_at = now
    session.flush()
    return True


def remaining_recovery_codes(user: User) -> int:
    return len(user.mfa_recovery_hashes or [])


def secret_for_display(secret: str) -> str:
    """Grouped in fours, for someone typing it in by hand.

    An authenticator app that cannot scan is common enough -- a locked-down
    corporate phone, a desktop client -- that the manual path has to be usable
    rather than technically present.
    """
    padded = secret + "=" * (-len(secret) % 8)
    base64.b32decode(padded, casefold=True)  # raises if the secret is malformed
    return " ".join(secret[i : i + 4] for i in range(0, len(secret), 4))
