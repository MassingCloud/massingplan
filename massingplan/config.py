"""Settings. Every default works offline.

Read from the environment with a `MASSINGPLAN_` prefix. Production refuses to
boot without a secret key rather than generating one per worker -- four workers
with four different keys invalidate each other's sessions, and the symptom is
"users get logged out at random", which is a long way from the cause.
"""

from __future__ import annotations

import os
import secrets
from dataclasses import dataclass, field


@dataclass
class Settings:
    env: str = field(default_factory=lambda: os.getenv("MASSINGPLAN_ENV", "development"))
    secret_key: str = field(default_factory=lambda: os.getenv("MASSINGPLAN_SECRET_KEY", ""))
    database_url: str = field(
        default_factory=lambda: os.getenv(
            "MASSINGPLAN_DATABASE_URL", "sqlite:///instance/massingplan.db"
        )
    )
    log_level: str = field(default_factory=lambda: os.getenv("MASSINGPLAN_LOG_LEVEL", "INFO"))
    max_upload_bytes: int = field(
        default_factory=lambda: int(os.getenv("MASSINGPLAN_MAX_UPLOAD_BYTES", 16 * 1024 * 1024))
    )

    rate_limit_enabled: bool = field(
        default_factory=lambda: os.getenv("MASSINGPLAN_RATE_LIMIT", "1") != "0"
    )
    #: `memory` or `database`. Memory is the default because it needs nothing,
    #: and it is correct for exactly one worker. `database` shares one counter
    #: across every worker and replica pointed at the same database, which is
    #: what makes the configured limit the real one.
    rate_limit_store: str = field(
        default_factory=lambda: os.getenv("MASSINGPLAN_RATE_LIMIT_STORE", "memory").strip().lower()
    )
    #: Read only to warn about it. The limiter's store is per-process, so N
    #: workers means N times the configured limit -- and a limiter that
    #: multiplies silently is worse than none, because it is believed.
    web_concurrency: int = field(default_factory=lambda: int(os.getenv("WEB_CONCURRENCY", "1")))
    session_lifetime_seconds: int = field(
        default_factory=lambda: int(os.getenv("MASSINGPLAN_SESSION_LIFETIME", 12 * 3600))
    )

    # -- SSO. Off unless an issuer is set; there is no half-configured state. --
    oidc_issuer: str = field(default_factory=lambda: os.getenv("MASSINGPLAN_OIDC_ISSUER", ""))
    oidc_client_id: str = field(default_factory=lambda: os.getenv("MASSINGPLAN_OIDC_CLIENT_ID", ""))
    oidc_client_secret: str = field(
        default_factory=lambda: os.getenv("MASSINGPLAN_OIDC_CLIENT_SECRET", "")
    )
    oidc_redirect_uri: str = field(
        default_factory=lambda: os.getenv("MASSINGPLAN_OIDC_REDIRECT_URI", "")
    )
    #: Relaxed only for a development IdP on localhost. An id_token over plain
    #: HTTP is one anybody on the path can read and replay.
    oidc_require_tls: bool = field(
        default_factory=lambda: os.getenv("MASSINGPLAN_OIDC_REQUIRE_TLS", "1") != "0"
    )

    @property
    def is_production(self) -> bool:
        return self.env == "production"

    @property
    def sso_enabled(self) -> bool:
        """All four, or none.

        A partially configured SSO is the dangerous state: a sign-in button
        that leads somewhere broken, or worse, an exchange that skips a check
        because the value it needed was empty. `oidc_settings()` refuses each
        missing field by name, and this decides whether to offer it at all.
        """
        return bool(
            self.oidc_issuer
            and self.oidc_client_id
            and self.oidc_client_secret
            and self.oidc_redirect_uri
        )

    def resolve_secret_key(self) -> str:
        if self.secret_key:
            return self.secret_key
        if self.is_production:
            raise RuntimeError(
                "MASSINGPLAN_SECRET_KEY is not set. Generate one with "
                '`python -c "import secrets; print(secrets.token_hex(32))"` and set it '
                "in the environment -- a per-worker random key logs users out at random."
            )
        return secrets.token_hex(32)

    def flask_config(self) -> dict[str, object]:
        return {
            "SECRET_KEY": self.resolve_secret_key(),
            "MAX_CONTENT_LENGTH": self.max_upload_bytes,
            "SESSION_COOKIE_HTTPONLY": True,
            "SESSION_COOKIE_SAMESITE": "Lax",
            "SESSION_COOKIE_SECURE": self.is_production,
            "JSON_SORT_KEYS": False,
            # Sessions expire. A browser cookie that never ages is a credential
            # left on a shared machine indefinitely.
            "PERMANENT_SESSION_LIFETIME": self.session_lifetime_seconds,
        }
