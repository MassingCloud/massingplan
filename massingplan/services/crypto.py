"""Encryption at rest for the few fields that need it.

Not the whole database. Encrypting every column costs every query an index and
buys nothing an attacker with database access has not already got — they hold
the key too, because the application needs it. What this protects is the
narrower and realer case: **a leaked backup or a stolen disk**, where the key
lives in the environment and does not travel with the dump.

So exactly two things are encrypted: TOTP secrets and, later, any third-party
credential. A TOTP secret in the clear is a second factor an attacker can
compute; everything else in this schema is a schedule, and a schedule the
attacker can already read is a schedule they can read.

The key is separate from ``SECRET_KEY`` on purpose. Rotating the session key
signs everyone out and should be cheap; rotating the encryption key requires
re-encrypting rows and must not be something anyone does casually.
"""

from __future__ import annotations

import base64
import hashlib
import os
import secrets

#: Fernet's key format: 32 bytes, urlsafe-base64.
KEY_BYTES = 32


class EncryptionUnavailableError(RuntimeError):
    """No key configured, or the `cryptography` package is absent."""


def generate_key() -> str:
    """A new key, in the format the environment variable expects."""
    return base64.urlsafe_b64encode(secrets.token_bytes(KEY_BYTES)).decode()


def _fernet(key: str | None = None):  # type: ignore[no-untyped-def]
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:  # pragma: no cover - the no-extras path
        raise EncryptionUnavailableError(
            "field encryption needs `cryptography`. Install it with: pip install 'massingplan[mfa]'"
        ) from exc

    material = key or os.getenv("MASSINGPLAN_ENCRYPTION_KEY", "")
    if not material:
        raise EncryptionUnavailableError(
            "MASSINGPLAN_ENCRYPTION_KEY is not set. Generate one with "
            "`massingplan gen-key`. It is deliberately separate from "
            "MASSINGPLAN_SECRET_KEY: rotating the session key signs everyone "
            "out and is cheap; rotating this one requires re-encrypting rows."
        )
    try:
        return Fernet(material.encode())
    except (ValueError, TypeError) as exc:
        raise EncryptionUnavailableError(
            "MASSINGPLAN_ENCRYPTION_KEY is not a valid Fernet key "
            "(32 urlsafe-base64 bytes). Generate one with `massingplan gen-key`."
        ) from exc


def encrypt(plaintext: str, *, key: str | None = None) -> str:
    """Fernet: AES-128-CBC with an HMAC and a timestamp, authenticated.

    Authenticated, which matters more than the cipher choice: an unauthenticated
    ciphertext can be tampered with, and for a TOTP secret that means an
    attacker who can write to the database can substitute a secret they know.
    """
    return _fernet(key).encrypt(plaintext.encode()).decode()


def decrypt(ciphertext: str, *, key: str | None = None) -> str:
    from cryptography.fernet import InvalidToken

    try:
        return _fernet(key).decrypt(ciphertext.encode()).decode()
    except InvalidToken as exc:
        # Named specifically: the usual cause is the key having changed, and
        # "invalid token" alone sends people looking at the data instead.
        raise EncryptionUnavailableError(
            "a stored value could not be decrypted. The usual cause is "
            "MASSINGPLAN_ENCRYPTION_KEY differing from the one that wrote it."
        ) from exc


def is_available(key: str | None = None) -> bool:
    """Whether encryption is usable, without raising. For `massingplan check`."""
    try:
        _fernet(key)
    except EncryptionUnavailableError:
        return False
    return True


# -- recovery codes --------------------------------------------------------


def generate_recovery_codes(count: int = 10) -> list[str]:
    """Human-transcribable one-time codes.

    Grouped and lowercase because these get written on paper and typed back at a
    stressful moment; `w7k2-9fqx` survives that and a 32-character hex string
    does not. The alphabet omits `l`, `1`, `o` and `0` for the same reason.
    """
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    return [
        "-".join("".join(secrets.choice(alphabet) for _ in range(4)) for _ in range(2))
        for _ in range(count)
    ]


def hash_recovery_code(code: str) -> str:
    """SHA-256 of the normalised code.

    A plain hash rather than a KDF: these are machine-generated with ~40 bits of
    entropy each and there is no dictionary to attack, so a slow hash would only
    slow the legitimate holder down.
    """
    return hashlib.sha256(normalise_recovery_code(code).encode()).hexdigest()


def normalise_recovery_code(code: str) -> str:
    """Fold the ways a person types a code they read off paper."""
    return code.strip().lower().replace(" ", "").replace("-", "")
