"""OpenID Connect: authorization code flow with PKCE, and strict token checks.

The seam has existed since P0 and this is the implementation behind it. It is
written against the standard library plus `cryptography` -- which the MFA extra
already brings -- rather than an OIDC library, for two reasons that are about
this codebase rather than about taste:

* the SSRF machinery that vets and pins outbound requests is already here, and
  a library that fetches its own discovery document and JWKS bypasses all of
  it. The issuer names the token and JWKS endpoints; those URLs arrive from
  outside and get the same treatment a tenant-supplied webhook URL does.
* an optional dependency that is not installed means tests that skip, and a
  feature whose tests always skip is a feature nobody tests. This repo has
  already shipped that once with MFA.

**What makes hand-written token validation defensible is that it refuses by
construction rather than by check.** The classic attacks on this code are all
"persuade the verifier to use a different algorithm than the issuer signed
with":

* `{"alg": "none"}` -- refused because the algorithm is never read from the
  token to decide *whether* to verify;
* HS256 with the RSA public key as the HMAC secret -- refused because no
  symmetric algorithm is implemented at all, so there is nothing to confuse it
  with;
* an unknown `kid`, or a key the issuer does not publish -- refused because the
  key comes from JWKS by `kid` and a miss is an error, not a fallback to the
  first key.

Everything else is the ordinary list, and every item is a test: exact `iss`,
`aud` containing the client id, `exp` and `iat` inside a small leeway, and
`nonce` equal to the one this session sent. The nonce is what makes a stolen
id_token useless somewhere else.

What this deliberately does not do
----------------------------------
* **No implicit joining of an existing organisation.** A verified assertion
  that somebody controls an email address is not a statement about who they
  work for. Provisioning is explicit and defaults to a new organisation, for
  the same reason self-service registration was changed to: the first version
  of that made strangers owners of the default tenant.
* **No refresh tokens, no userinfo call, no back-channel logout.** Each is a
  real feature with real failure modes; absent is honest, half-built is not.
* **No dynamic client registration.** The client id and secret are operator
  configuration.
"""

from __future__ import annotations

import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlencode, urlsplit

from .. import webhook_url
from .base import IdentityError, IdentityProvider, Principal


class OidcError(IdentityError):
    """The exchange or the token failed a check. Never partially authenticated."""


#: Asymmetric only, and named explicitly. The absence of every HMAC algorithm
#: is the defence against the RSA-public-key-as-HMAC-secret confusion attack:
#: there is no code path that could be tricked into taking it.
SUPPORTED_ALGORITHMS = frozenset({"RS256", "RS384", "RS512", "ES256", "ES384", "ES512"})

#: Clock skew allowed on `exp` and `iat`. Sixty seconds is the usual figure and
#: is small enough that an expired token is not usable for meaningfully longer.
LEEWAY_SECONDS = 60

#: How long a discovery document is trusted before it is fetched again. Not
#: forever: an issuer rotating its JWKS URI should be picked up without a
#: restart. Not never: a fetch per sign-in makes the IdP a hard dependency of
#: every request.
DISCOVERY_TTL_SECONDS = 300


def _b64url_decode(segment: str) -> bytes:
    """Base64url with the padding the JWS spec strips.

    A wrong-length segment is a malformed token, not something to pad and hope
    about, so the padding is computed rather than tried.
    """
    padding = "=" * (-len(segment) % 4)
    try:
        return base64.urlsafe_b64decode(segment + padding)
    except (ValueError, TypeError) as exc:
        raise OidcError("the token is not valid base64url") from exc


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


@dataclass(frozen=True)
class OidcSettings:
    """Operator configuration. No secrets are ever returned by `describe()`."""

    issuer: str
    client_id: str
    client_secret: str
    redirect_uri: str
    scopes: tuple[str, ...] = ("openid", "email", "profile")
    #: Relaxed only for a development install against a local IdP. The default
    #: refuses plain HTTP, because an id_token over http is an id_token anybody
    #: on the path can read and replay.
    require_tls: bool = True

    def __post_init__(self) -> None:
        for name in ("issuer", "client_id", "client_secret", "redirect_uri"):
            if not str(getattr(self, name) or "").strip():
                raise OidcError(f"OIDC needs a {name}")
        if "openid" not in self.scopes:
            raise OidcError("the `openid` scope is what makes this OpenID Connect")


@dataclass
class AuthorizationRequest:
    """The three secrets a callback has to be checked against.

    All three are held server-side in the session and none travels anywhere
    else. `state` catches a forged callback, `nonce` catches a replayed
    id_token, and `code_verifier` catches an intercepted code.
    """

    state: str
    nonce: str
    code_verifier: str
    url: str


class Fetcher:
    """Vetted, pinned, redirect-free HTTP. Replaceable so tests need no socket.

    Every URL passed here came from outside: the discovery document from the
    operator's configured issuer, and the token and JWKS endpoints from the
    discovery document itself. The second hop is the one worth stating -- an
    issuer that is compromised, or simply hostile, names the URLs this code
    then fetches, which is the same shape as a tenant-supplied webhook.
    """

    def __init__(self, *, require_tls: bool = True, timeout: int = 10) -> None:
        self.require_tls = require_tls
        self.timeout = timeout

    def __call__(
        self,
        url: str,
        *,
        method: str = "GET",
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes]:
        from .. import http_pinned

        target = webhook_url.vet(url, require_tls=self.require_tls)
        answer = http_pinned.request(
            target, method=method, body=body, headers=headers, timeout=self.timeout
        )
        return answer.status, answer.body


@dataclass
class _CachedDiscovery:
    document: dict[str, Any]
    fetched_at: float


class OidcProvider(IdentityProvider):
    """The adapter. `authenticate()` takes a finished exchange, not a password.

    `IdentityProvider.authenticate` is shaped for credentials, and an OIDC
    sign-in is a three-legged flow rather than a credential presentation. The
    honest mapping is that `credentials` carries the *result* of the flow --
    the code and the request that started it -- and this class owns the legs.
    """

    name = "oidc"

    def __init__(
        self,
        settings: OidcSettings | None = None,
        *,
        fetcher: Fetcher | None = None,
        clock: Any = time.time,
        **kwargs: Any,
    ) -> None:
        if settings is None:
            settings = OidcSettings(**kwargs)
        # Checked here rather than at the first signature verification. Without
        # it, `resolve("oidc")` succeeds on an install with no `cryptography`,
        # the operator sees a working sign-in button, and the failure arrives
        # as an ImportError three legs into somebody's first real sign-in.
        try:
            import cryptography  # noqa: F401
        except ImportError as exc:  # pragma: no cover - exercised by the no-extras job
            raise OidcError(
                "the OIDC adapter needs `cryptography` to verify id_tokens. "
                "Install it with: pip install 'massingplan[oidc]'"
            ) from exc
        self.settings = settings
        self.fetch = fetcher or Fetcher(require_tls=settings.require_tls)
        self.clock = clock
        self._discovery: _CachedDiscovery | None = None
        self._jwks: dict[str, Any] = {}

    # -- discovery ---------------------------------------------------------

    def discover(self, *, force: bool = False) -> dict[str, Any]:
        """The issuer's own description of itself, with the issuer verified.

        `issuer` inside the document must equal the configured one exactly.
        Without that check, an operator who mistypes the issuer -- or a
        redirect nobody noticed -- ends up trusting a document from somewhere
        else entirely, and every endpoint below comes from that document.
        """
        now = self.clock()
        cached = self._discovery
        if cached and not force and now - cached.fetched_at < DISCOVERY_TTL_SECONDS:
            return cached.document

        base = self.settings.issuer.rstrip("/")
        status, raw = self.fetch(f"{base}/.well-known/openid-configuration")
        if status != 200:
            raise OidcError(f"the issuer's discovery document answered {status}")
        try:
            document = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise OidcError("the discovery document is not JSON") from exc
        if not isinstance(document, dict):
            raise OidcError("the discovery document is not an object")

        if str(document.get("issuer", "")).rstrip("/") != base:
            raise OidcError(
                f"the discovery document claims issuer {document.get('issuer')!r}, "
                f"which is not {self.settings.issuer!r}"
            )
        for required in ("authorization_endpoint", "token_endpoint", "jwks_uri"):
            if not document.get(required):
                raise OidcError(f"the discovery document has no {required}")

        self._discovery = _CachedDiscovery(document=document, fetched_at=now)
        self._jwks = {}
        return document

    # -- leg one: send them to the issuer ----------------------------------

    def begin(self) -> AuthorizationRequest:
        """Build the authorization URL, and the three secrets to check it with.

        PKCE with S256 even though this is a confidential client. The code
        challenge costs nothing and removes a whole class of failure where the
        code leaks -- a referrer header, a proxy log, a shared browser -- and
        the secret alone is enough to redeem it.
        """
        document = self.discover()
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        verifier = secrets.token_urlsafe(64)
        challenge = _b64url_encode(hashlib.sha256(verifier.encode("ascii")).digest())

        query = urlencode(
            {
                "response_type": "code",
                "client_id": self.settings.client_id,
                "redirect_uri": self.settings.redirect_uri,
                "scope": " ".join(self.settings.scopes),
                "state": state,
                "nonce": nonce,
                "code_challenge": challenge,
                "code_challenge_method": "S256",
            }
        )
        endpoint = str(document["authorization_endpoint"])
        joiner = "&" if urlsplit(endpoint).query else "?"
        return AuthorizationRequest(
            state=state, nonce=nonce, code_verifier=verifier, url=f"{endpoint}{joiner}{query}"
        )

    # -- leg two: redeem the code ------------------------------------------

    def exchange(self, code: str, request: AuthorizationRequest) -> dict[str, Any]:
        """Swap the code for tokens at the token endpoint.

        `client_secret_basic`, which every provider supports, and the verifier
        so the issuer can check the challenge it was given at the start.
        """
        document = self.discover()
        credentials = f"{self.settings.client_id}:{self.settings.client_secret}"
        basic = base64.b64encode(credentials.encode("utf-8")).decode("ascii")
        body = urlencode(
            {
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": self.settings.redirect_uri,
                "code_verifier": request.code_verifier,
            }
        ).encode("ascii")

        status, raw = self.fetch(
            str(document["token_endpoint"]),
            method="POST",
            body=body,
            headers={
                "Authorization": f"Basic {basic}",
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        )
        if status != 200:
            # The body is not echoed. A token endpoint's error can carry the
            # code or the client id, and this string reaches a rendered page.
            raise OidcError(f"the token endpoint answered {status}")
        try:
            tokens = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise OidcError("the token endpoint did not return JSON") from exc
        if not isinstance(tokens, dict) or not tokens.get("id_token"):
            raise OidcError("the token response carried no id_token")
        return tokens

    # -- leg three: believe the token, or do not ---------------------------

    def _key_for(self, kid: str | None, algorithm: str) -> Any:
        """The issuer's public key with this `kid`, from JWKS.

        A miss refetches once -- keys rotate -- and then fails. It never falls
        back to "the only key" or "the first key": that turns an unknown `kid`
        into a signature check against a key the issuer did not use.
        """
        from cryptography.hazmat.primitives.asymmetric import ec, rsa

        def find(keys: list[dict[str, Any]]) -> dict[str, Any] | None:
            for key in keys:
                if kid is not None and key.get("kid") != kid:
                    continue
                if kid is None and len(keys) != 1:
                    return None
                return key
            return None

        if not self._jwks:
            self._load_jwks()
        chosen = find(self._jwks.get("keys", []))
        if chosen is None:
            self._load_jwks(force=True)
            chosen = find(self._jwks.get("keys", []))
        if chosen is None:
            raise OidcError(f"the issuer publishes no key with kid {kid!r}")

        kty = chosen.get("kty")
        if algorithm.startswith("RS") and kty != "RSA":
            raise OidcError(f"the token says {algorithm} but the key is {kty}")
        if algorithm.startswith("ES") and kty != "EC":
            raise OidcError(f"the token says {algorithm} but the key is {kty}")

        if kty == "RSA":
            numbers = rsa.RSAPublicNumbers(
                e=int.from_bytes(_b64url_decode(str(chosen["e"])), "big"),
                n=int.from_bytes(_b64url_decode(str(chosen["n"])), "big"),
            )
            return numbers.public_key()
        if kty == "EC":
            curves = {"P-256": ec.SECP256R1(), "P-384": ec.SECP384R1(), "P-521": ec.SECP521R1()}
            curve = curves.get(str(chosen.get("crv")))
            if curve is None:
                raise OidcError(f"unsupported EC curve {chosen.get('crv')!r}")
            return ec.EllipticCurvePublicNumbers(
                x=int.from_bytes(_b64url_decode(str(chosen["x"])), "big"),
                y=int.from_bytes(_b64url_decode(str(chosen["y"])), "big"),
                curve=curve,
            ).public_key()
        raise OidcError(f"unsupported key type {kty!r}")

    def _load_jwks(self, *, force: bool = False) -> None:
        document = self.discover(force=force)
        status, raw = self.fetch(str(document["jwks_uri"]))
        if status != 200:
            raise OidcError(f"the issuer's JWKS answered {status}")
        try:
            jwks = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError) as exc:
            raise OidcError("the JWKS is not JSON") from exc
        if not isinstance(jwks, dict) or not isinstance(jwks.get("keys"), list):
            raise OidcError("the JWKS has no key list")
        self._jwks = jwks

    def verify_id_token(self, token: str, *, nonce: str) -> dict[str, Any]:
        """Every check, in an order where none of them can be skipped.

        The algorithm is taken from the header only to *select a verifier from
        a fixed set*. It never decides whether to verify, which is the
        difference between this and every `alg: none` bypass ever written.
        """
        from cryptography.exceptions import InvalidSignature
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec, padding

        parts = token.split(".")
        if len(parts) != 3:
            raise OidcError("an id_token has three segments")
        header_raw, payload_raw, signature_raw = parts

        try:
            header = json.loads(_b64url_decode(header_raw))
            claims = json.loads(_b64url_decode(payload_raw))
        except ValueError as exc:
            raise OidcError("the token header or payload is not JSON") from exc
        if not isinstance(header, dict) or not isinstance(claims, dict):
            raise OidcError("the token header or payload is not an object")

        algorithm = str(header.get("alg", ""))
        if algorithm not in SUPPORTED_ALGORITHMS:
            # Covers `none`, every HMAC variant, and anything invented later.
            raise OidcError(f"unsupported or unsafe signing algorithm {algorithm!r}")

        key = self._key_for(header.get("kid"), algorithm)
        signed = f"{header_raw}.{payload_raw}".encode("ascii")
        signature = _b64url_decode(signature_raw)
        digest = {
            "256": hashes.SHA256(),
            "384": hashes.SHA384(),
            "512": hashes.SHA512(),
        }[algorithm[-3:]]

        try:
            if algorithm.startswith("RS"):
                key.verify(signature, signed, padding.PKCS1v15(), digest)
            else:
                # JWS ES* signatures are raw r||s; `cryptography` wants DER.
                from cryptography.hazmat.primitives.asymmetric.utils import (
                    encode_dss_signature,
                )

                half = len(signature) // 2
                der = encode_dss_signature(
                    int.from_bytes(signature[:half], "big"),
                    int.from_bytes(signature[half:], "big"),
                )
                key.verify(der, signed, ec.ECDSA(digest))
        except InvalidSignature as exc:
            raise OidcError("the id_token signature does not verify") from exc

        issuer = str(claims.get("iss", "")).rstrip("/")
        if issuer != self.settings.issuer.rstrip("/"):
            raise OidcError(f"the id_token was issued by {issuer!r}")

        audience = claims.get("aud")
        audiences = [audience] if isinstance(audience, str) else list(audience or [])
        if self.settings.client_id not in audiences:
            raise OidcError("the id_token is for a different client")
        if len(audiences) > 1 and claims.get("azp") != self.settings.client_id:
            # Multiple audiences without an `azp` naming us means the token was
            # minted for somebody else and merely mentions us.
            raise OidcError("the id_token has several audiences and is not authorised to us")

        now = self.clock()
        expires = claims.get("exp")
        if not isinstance(expires, int | float) or now > expires + LEEWAY_SECONDS:
            raise OidcError("the id_token has expired")
        issued = claims.get("iat")
        if isinstance(issued, int | float) and issued > now + LEEWAY_SECONDS:
            raise OidcError("the id_token was issued in the future")

        # Last, and the one that makes a stolen token useless elsewhere: it has
        # to be the nonce *this* session sent.
        if not nonce or not secrets.compare_digest(str(claims.get("nonce", "")), nonce):
            raise OidcError("the id_token does not answer this sign-in")

        if not str(claims.get("sub", "")).strip():
            raise OidcError("the id_token has no subject")
        return claims

    # -- the seam ----------------------------------------------------------

    def authenticate(self, credentials: dict[str, str]) -> Principal | None:
        """Finish a flow already begun. Returns `None` only for "not this one".

        A failed *check* raises rather than returning `None`, because the two
        outcomes are different: "no OIDC attempt here" is a fall-through to the
        next provider, and "an OIDC attempt that failed verification" is
        something an operator needs to see.
        """
        code = credentials.get("code", "")
        if not code:
            return None
        state = credentials.get("state", "")
        expected_state = credentials.get("expected_state", "")
        if not expected_state or not secrets.compare_digest(state, expected_state):
            raise OidcError("the callback state does not match this sign-in")

        request = AuthorizationRequest(
            state=expected_state,
            nonce=credentials.get("nonce", ""),
            code_verifier=credentials.get("code_verifier", ""),
            url="",
        )
        tokens = self.exchange(code, request)
        claims = self.verify_id_token(str(tokens["id_token"]), nonce=request.nonce)

        email = claims.get("email")
        # An unverified email is a string the user typed at the IdP. Treating it
        # as an identity lets anyone claim anyone's address at a provider that
        # does not check.
        if email is not None and claims.get("email_verified") is False:
            email = None

        return Principal(
            subject=f"{self.settings.issuer.rstrip('/')}#{claims['sub']}",
            email=str(email) if email else None,
            display_name=str(claims.get("name") or claims.get("preferred_username") or ""),
            roles=frozenset(),
        )

    def describe(self) -> dict[str, object]:
        """Enough to see which provider is live. Never the client secret."""
        return {
            "provider": self.name,
            "issuer": self.settings.issuer,
            "client_id": self.settings.client_id,
            "scopes": list(self.settings.scopes),
            "require_tls": self.settings.require_tls,
            "discovery_cached": self._discovery is not None,
        }


#: Kept out of `__init__` so importing the package does not need `cryptography`.
__all__ = [
    "SUPPORTED_ALGORITHMS",
    "AuthorizationRequest",
    "Fetcher",
    "OidcError",
    "OidcProvider",
    "OidcSettings",
]
