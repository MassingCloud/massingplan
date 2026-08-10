"""Signed tokens, API keys and webhook signatures.

Standard library only, deliberately: everything here is HMAC over a shared
secret, and pulling in a crypto dependency for that would be a supply-chain cost
with no security benefit.

What is **not** here, and why: password hashing. There are no passwords yet
because there is no user model yet. When one lands it uses argon2id via
``argon2-cffi``, added as a dependency at the same time. An empty
``hash_password`` returning something plausible would be far worse than its
absence.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import time

#: Prefix on every issued key, so one found in a log is identifiable at a glance
#: and can be grepped for across a codebase. Mirrors massingbill's `mbil_`.
API_KEY_PREFIX = "mpln_"

#: The header massing.cloud signs webhooks with (SPEC.md 3.2). Byte-identical
#: across products so one subscriber implementation verifies both.
SIGNATURE_HEADER = "X-Massing-Signature"

#: How long a signed payload stays valid. Five minutes is enough for clock skew
#: and a retry, and short enough that a captured request is not a standing key.
DEFAULT_MAX_AGE_SECONDS = 300


def generate_api_key() -> tuple[str, str]:
    """A new key and its hash. ``(key, key_hash)``.

    The plaintext is returned once, to be shown once. Only the hash is stored --
    a database of usable credentials is a breach waiting for a backup to leak.
    """
    key = API_KEY_PREFIX + secrets.token_urlsafe(32)
    return key, hash_api_key(key)


def hash_api_key(key: str) -> str:
    """SHA-256 of the key.

    A plain hash rather than a password KDF, on purpose: an API key is 32 bytes
    of machine-generated entropy, so there is no dictionary to attack and the
    KDF would only slow down every request.
    """
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def verify_api_key(key: str, expected_hash: str) -> bool:
    """Constant-time comparison.

    ``==`` on a hash leaks its prefix through timing, one byte at a time.
    """
    return hmac.compare_digest(hash_api_key(key), expected_hash)


def sign(payload: bytes, secret: str) -> str:
    """Hex HMAC-SHA256, the format massing.cloud's subscribers already verify."""
    return hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).hexdigest()


def verify_signature(payload: bytes, signature: str, secret: str) -> bool:
    return hmac.compare_digest(sign(payload, secret), signature)


def sign_with_timestamp(payload: bytes, secret: str, *, now: int | None = None) -> str:
    """``t=<unix>,v1=<hex>`` -- the signature covers the timestamp too.

    Without binding the timestamp into the signed material, an attacker replays
    a captured body forever: the signature stays valid because the body did not
    change. Signing ``t.payload`` makes the age tamper-evident.
    """
    stamp = int(time.time()) if now is None else now
    signed = f"{stamp}.".encode() + payload
    return f"t={stamp},v1={sign(signed, secret)}"


def verify_timestamped(
    payload: bytes,
    header: str,
    secret: str,
    *,
    max_age_seconds: int = DEFAULT_MAX_AGE_SECONDS,
    now: int | None = None,
) -> bool:
    """Verify a ``t=...,v1=...`` header, rejecting anything too old."""
    parts = dict(piece.split("=", 1) for piece in header.split(",") if "=" in piece)
    stamp_raw, provided = parts.get("t"), parts.get("v1")
    if not stamp_raw or not provided:
        return False
    try:
        stamp = int(stamp_raw)
    except ValueError:
        return False

    current = int(time.time()) if now is None else now
    # Both directions. A timestamp far in the future is as much a sign of
    # tampering as one far in the past, and only checking one side lets a
    # forged future stamp keep a replay valid indefinitely.
    if abs(current - stamp) > max_age_seconds:
        return False

    signed = f"{stamp}.".encode() + payload
    return hmac.compare_digest(sign(signed, secret), provided)
