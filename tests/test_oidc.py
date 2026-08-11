"""OIDC, and mostly the ways an id_token can be a lie.

The happy path is four assertions at the top. Everything after it is the
adversarial half, because that is where this code earns its place: a token
verifier that accepts a valid token is not evidence of anything -- every broken
one does that too.

The fake issuer signs with a real RSA key generated per test run, so the
signature checks are genuine rather than stubbed. What is stubbed is the
network: `Fetcher` is replaced by a callable over a dict, which keeps the
offline guarantee and lets a test serve a hostile discovery document without
standing up a server to be hostile from.
"""

from __future__ import annotations

import base64
import hashlib
import json
import time
from typing import Any
from urllib.parse import parse_qs, urlsplit

import pytest

# The `no-adapters` CI job deletes every optional adapter and re-runs the
# suite, to prove the app stands up without them. These tests are *about* an
# adapter, so they skip there rather than erroring at collection -- and skip
# loudly, by module, so a skip cannot be mistaken for a pass. `cryptography` is
# in the dev extras, so under the normal suite this never skips: a feature
# whose tests always skip is a feature nobody tests, which this repo has
# already shipped once with MFA.
oidc = pytest.importorskip(
    "massingplan.services.identity.oidc",
    reason="the OIDC adapter is absent (the no-adapters job deletes it)",
)
pytest.importorskip("cryptography")

ISSUER = "https://idp.example.com"
CLIENT_ID = "massingplan"
SECRET = "a-client-secret"
REDIRECT = "https://plan.example.com/auth/sso/callback"


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


class FakeIdp:
    """An issuer that signs real JWTs, and can be asked to misbehave."""

    def __init__(self) -> None:
        from cryptography.hazmat.primitives.asymmetric import rsa

        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.kid = "test-key-1"
        self.issuer = ISSUER
        self.responses: dict[str, tuple[int, bytes]] = {}
        self.calls: list[tuple[str, str]] = []
        self._install_defaults()

    # -- what it serves ----------------------------------------------------

    def _jwks(self) -> dict[str, Any]:
        numbers = self.key.public_key().public_numbers()
        return {
            "keys": [
                {
                    "kty": "RSA",
                    "kid": self.kid,
                    "use": "sig",
                    "alg": "RS256",
                    "n": _b64(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
                    "e": _b64(numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")),
                }
            ]
        }

    def _install_defaults(self) -> None:
        self.responses = {
            f"{ISSUER}/.well-known/openid-configuration": (
                200,
                json.dumps(
                    {
                        "issuer": self.issuer,
                        "authorization_endpoint": f"{ISSUER}/authorize",
                        "token_endpoint": f"{ISSUER}/token",
                        "jwks_uri": f"{ISSUER}/jwks",
                    }
                ).encode(),
            ),
            f"{ISSUER}/jwks": (200, json.dumps(self._jwks()).encode()),
        }

    def serve(self, url: str, status: int, payload: object) -> None:
        raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.responses[url] = (status, raw)

    def fetch(
        self,
        url: str,
        *,
        method: str = "GET",
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes]:
        self.calls.append((method, url))
        del body, headers
        if url not in self.responses:
            return 404, b"{}"
        return self.responses[url]

    # -- what it signs -----------------------------------------------------

    def sign(
        self,
        claims: dict[str, Any],
        *,
        algorithm: str = "RS256",
        kid: str | None = "test-key-1",
        signature: bytes | None = None,
    ) -> str:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import padding

        header: dict[str, Any] = {"alg": algorithm, "typ": "JWT"}
        if kid is not None:
            header["kid"] = kid
        head = _b64(json.dumps(header).encode())
        body = _b64(json.dumps(claims).encode())
        signed = f"{head}.{body}".encode("ascii")
        if signature is None:
            signature = self.key.sign(signed, padding.PKCS1v15(), hashes.SHA256())
        return f"{head}.{body}.{_b64(signature)}"

    def id_token(self, **overrides: Any) -> str:
        now = int(time.time())
        claims: dict[str, Any] = {
            "iss": self.issuer,
            "sub": "user-42",
            "aud": CLIENT_ID,
            "exp": now + 300,
            "iat": now,
            "nonce": "the-nonce",
            "email": "planner@example.com",
            "email_verified": True,
            "name": "A Planner",
        }
        signing = {k: overrides.pop(k) for k in ("algorithm", "kid", "signature") if k in overrides}
        claims.update(overrides)
        claims = {k: v for k, v in claims.items() if v is not None}
        return self.sign(claims, **signing)


@pytest.fixture
def idp() -> FakeIdp:
    return FakeIdp()


@pytest.fixture
def provider(idp: FakeIdp) -> oidc.OidcProvider:
    settings = oidc.OidcSettings(
        issuer=ISSUER, client_id=CLIENT_ID, client_secret=SECRET, redirect_uri=REDIRECT
    )
    return oidc.OidcProvider(settings, fetcher=idp.fetch)  # type: ignore[arg-type]


def _request(nonce: str = "the-nonce") -> oidc.AuthorizationRequest:
    return oidc.AuthorizationRequest(
        state="the-state", nonce=nonce, code_verifier="the-verifier", url=""
    )


# -- the happy path --------------------------------------------------------


def test_a_valid_token_authenticates(provider: oidc.OidcProvider, idp: FakeIdp) -> None:
    idp.serve(f"{ISSUER}/token", 200, {"id_token": idp.id_token(), "token_type": "Bearer"})
    principal = provider.authenticate(
        {
            "code": "the-code",
            "state": "the-state",
            "expected_state": "the-state",
            "nonce": "the-nonce",
            "code_verifier": "the-verifier",
        }
    )
    assert principal is not None
    assert principal.subject == f"{ISSUER}#user-42"
    assert principal.email == "planner@example.com"
    assert principal.display_name == "A Planner"


def test_the_subject_is_namespaced_by_issuer(provider: oidc.OidcProvider, idp: FakeIdp) -> None:
    """`sub` is unique within an issuer and nowhere else. Two providers both
    numbering their users from 1 would otherwise be the same person here.
    """
    claims = provider.verify_id_token(idp.id_token(), nonce="the-nonce")
    assert claims["sub"] == "user-42"
    idp.serve(f"{ISSUER}/token", 200, {"id_token": idp.id_token()})
    principal = provider.authenticate(
        {
            "code": "c",
            "state": "s",
            "expected_state": "s",
            "nonce": "the-nonce",
            "code_verifier": "v",
        }
    )
    assert principal is not None
    assert principal.subject.startswith(f"{ISSUER}#")


# -- the authorization request ---------------------------------------------


def test_begin_uses_pkce_with_s256(provider: oidc.OidcProvider) -> None:
    """A confidential client does not strictly need PKCE, and gets it anyway:
    a code that leaks through a referrer, a proxy log or a shared browser is
    redeemable with the client secret alone without it.
    """
    request = provider.begin()
    query = parse_qs(urlsplit(request.url).query)
    assert query["response_type"] == ["code"]
    assert query["code_challenge_method"] == ["S256"]

    expected = (
        base64.urlsafe_b64encode(hashlib.sha256(request.code_verifier.encode()).digest())
        .decode()
        .rstrip("=")
    )
    assert query["code_challenge"] == [expected]


def test_begin_produces_a_fresh_state_and_nonce_every_time(provider: oidc.OidcProvider) -> None:
    first, second = provider.begin(), provider.begin()
    assert first.state != second.state
    assert first.nonce != second.nonce
    assert first.code_verifier != second.code_verifier
    assert len(first.state) >= 32


# -- discovery -------------------------------------------------------------


def test_a_discovery_document_claiming_another_issuer_is_refused(
    provider: oidc.OidcProvider, idp: FakeIdp
) -> None:
    """Every endpoint below comes from this document. If the issuer inside it
    is not the one configured, the operator is trusting a description of
    somewhere else -- from a typo, or a redirect nobody noticed.
    """
    idp.serve(
        f"{ISSUER}/.well-known/openid-configuration",
        200,
        {
            "issuer": "https://attacker.example.com",
            "authorization_endpoint": "https://attacker.example.com/a",
            "token_endpoint": "https://attacker.example.com/t",
            "jwks_uri": "https://attacker.example.com/j",
        },
    )
    with pytest.raises(oidc.OidcError, match="not"):
        provider.discover()


@pytest.mark.parametrize("missing", ["authorization_endpoint", "token_endpoint", "jwks_uri"])
def test_an_incomplete_discovery_document_is_refused(
    provider: oidc.OidcProvider, idp: FakeIdp, missing: str
) -> None:
    document = {
        "issuer": ISSUER,
        "authorization_endpoint": f"{ISSUER}/authorize",
        "token_endpoint": f"{ISSUER}/token",
        "jwks_uri": f"{ISSUER}/jwks",
    }
    del document[missing]
    idp.serve(f"{ISSUER}/.well-known/openid-configuration", 200, document)
    with pytest.raises(oidc.OidcError, match=missing):
        provider.discover()


def test_discovery_is_cached_rather_than_fetched_per_sign_in(
    provider: oidc.OidcProvider, idp: FakeIdp
) -> None:
    provider.discover()
    provider.discover()
    fetches = [url for _m, url in idp.calls if "openid-configuration" in url]
    assert len(fetches) == 1


def test_a_discovery_document_that_is_not_json_is_refused(
    provider: oidc.OidcProvider, idp: FakeIdp
) -> None:
    idp.serve(f"{ISSUER}/.well-known/openid-configuration", 200, b"<html>nope</html>")
    with pytest.raises(oidc.OidcError, match="not JSON"):
        provider.discover()


# -- the attacks on the token ----------------------------------------------


def test_alg_none_is_refused(provider: oidc.OidcProvider, idp: FakeIdp) -> None:
    """The oldest bypass there is. Refused because the header's algorithm never
    decides *whether* to verify -- only which verifier, from a fixed set.
    """
    token = idp.id_token(algorithm="none", signature=b"")
    with pytest.raises(oidc.OidcError, match="unsupported or unsafe"):
        provider.verify_id_token(token, nonce="the-nonce")


@pytest.mark.parametrize("algorithm", ["HS256", "HS384", "HS512"])
def test_hmac_algorithms_are_refused(
    provider: oidc.OidcProvider, idp: FakeIdp, algorithm: str
) -> None:
    """The RSA-public-key-as-HMAC-secret confusion. There is no symmetric code
    path to confuse: the algorithm is simply not in the supported set.
    """
    import hmac

    from cryptography.hazmat.primitives import serialization

    header = _b64(json.dumps({"alg": algorithm, "kid": idp.kid}).encode())
    claims = _b64(json.dumps({"iss": ISSUER, "sub": "x", "aud": CLIENT_ID}).encode())
    # The attack in full: the issuer's *public* key, which anybody can fetch
    # from JWKS, used as the HMAC secret. A verifier that reads `alg` from the
    # header and dispatches on it accepts this.
    public_pem = idp.key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )
    forged = hmac.new(public_pem, f"{header}.{claims}".encode(), hashlib.sha256).digest()
    with pytest.raises(oidc.OidcError, match="unsupported or unsafe"):
        provider.verify_id_token(f"{header}.{claims}.{_b64(forged)}", nonce="the-nonce")


def test_a_tampered_payload_fails_the_signature(provider: oidc.OidcProvider, idp: FakeIdp) -> None:
    """The property the whole file rests on: change one claim and it stops
    verifying. Asserted directly rather than trusted.
    """
    token = idp.id_token()
    head, payload, signature = token.split(".")
    claims = json.loads(base64.urlsafe_b64decode(payload + "=" * (-len(payload) % 4)))
    claims["sub"] = "somebody-else"
    swapped = _b64(json.dumps(claims).encode())
    with pytest.raises(oidc.OidcError, match="signature"):
        provider.verify_id_token(f"{head}.{swapped}.{signature}", nonce="the-nonce")


def test_a_token_signed_by_a_different_key_is_refused(
    provider: oidc.OidcProvider, idp: FakeIdp
) -> None:
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import padding, rsa

    rogue = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = int(time.time())
    claims = {
        "iss": ISSUER,
        "sub": "user-42",
        "aud": CLIENT_ID,
        "exp": now + 300,
        "nonce": "the-nonce",
    }
    head = _b64(json.dumps({"alg": "RS256", "kid": idp.kid}).encode())
    body = _b64(json.dumps(claims).encode())
    forged = rogue.sign(f"{head}.{body}".encode(), padding.PKCS1v15(), hashes.SHA256())
    with pytest.raises(oidc.OidcError, match="signature"):
        provider.verify_id_token(f"{head}.{body}.{_b64(forged)}", nonce="the-nonce")


def test_an_unknown_kid_is_refused_rather_than_falling_back(
    provider: oidc.OidcProvider, idp: FakeIdp
) -> None:
    """ "Try the only key" is the tempting fallback and it is wrong: it turns an
    unknown key id into a check against a key the issuer never used.
    """
    token = idp.id_token(kid="a-key-nobody-published")
    with pytest.raises(oidc.OidcError, match="no key with kid"):
        provider.verify_id_token(token, nonce="the-nonce")


def test_a_token_from_another_issuer_is_refused(provider: oidc.OidcProvider, idp: FakeIdp) -> None:
    token = idp.id_token(iss="https://attacker.example.com")
    with pytest.raises(oidc.OidcError, match="issued by"):
        provider.verify_id_token(token, nonce="the-nonce")


def test_a_token_for_another_client_is_refused(provider: oidc.OidcProvider, idp: FakeIdp) -> None:
    """The same IdP serves many applications. A token minted for one of them is
    a valid, correctly-signed token that must not sign anybody in here.
    """
    token = idp.id_token(aud="some-other-app")
    with pytest.raises(oidc.OidcError, match="different client"):
        provider.verify_id_token(token, nonce="the-nonce")


def test_multiple_audiences_need_an_azp_naming_us(
    provider: oidc.OidcProvider, idp: FakeIdp
) -> None:
    """A token listing us among several audiences without an `azp` was minted
    for somebody else and merely mentions us.
    """
    token = idp.id_token(aud=[CLIENT_ID, "another-app"])
    with pytest.raises(oidc.OidcError, match="several audiences"):
        provider.verify_id_token(token, nonce="the-nonce")

    allowed = idp.id_token(aud=[CLIENT_ID, "another-app"], azp=CLIENT_ID)
    assert provider.verify_id_token(allowed, nonce="the-nonce")["sub"] == "user-42"


def test_an_expired_token_is_refused(provider: oidc.OidcProvider, idp: FakeIdp) -> None:
    token = idp.id_token(exp=int(time.time()) - 3600)
    with pytest.raises(oidc.OidcError, match="expired"):
        provider.verify_id_token(token, nonce="the-nonce")


def test_the_leeway_is_small_and_one_sided(provider: oidc.OidcProvider, idp: FakeIdp) -> None:
    """Just inside the leeway is accepted, just outside is not. Without both,
    "we allow some skew" quietly becomes "we do not check `exp`".
    """
    now = int(time.time())
    assert provider.verify_id_token(
        idp.id_token(exp=now - oidc.LEEWAY_SECONDS + 5), nonce="the-nonce"
    )
    with pytest.raises(oidc.OidcError, match="expired"):
        provider.verify_id_token(idp.id_token(exp=now - oidc.LEEWAY_SECONDS - 5), nonce="the-nonce")


def test_a_token_issued_in_the_future_is_refused(provider: oidc.OidcProvider, idp: FakeIdp) -> None:
    token = idp.id_token(iat=int(time.time()) + 3600)
    with pytest.raises(oidc.OidcError, match="future"):
        provider.verify_id_token(token, nonce="the-nonce")


def test_a_replayed_token_from_another_sign_in_is_refused(
    provider: oidc.OidcProvider, idp: FakeIdp
) -> None:
    """The nonce is what makes a captured id_token useless anywhere but the
    session that asked for it. Everything else about this token is valid.
    """
    token = idp.id_token(nonce="a-nonce-from-somewhere-else")
    with pytest.raises(oidc.OidcError, match="does not answer this sign-in"):
        provider.verify_id_token(token, nonce="the-nonce")


def test_a_token_with_no_nonce_is_refused(provider: oidc.OidcProvider, idp: FakeIdp) -> None:
    token = idp.id_token(nonce=None)
    with pytest.raises(oidc.OidcError, match="does not answer this sign-in"):
        provider.verify_id_token(token, nonce="the-nonce")


def test_an_empty_expected_nonce_cannot_match_anything(
    provider: oidc.OidcProvider, idp: FakeIdp
) -> None:
    """A missing server-side nonce must fail closed. If "" matched "", losing
    the session would turn the replay check off rather than on.
    """
    token = idp.id_token(nonce="")
    with pytest.raises(oidc.OidcError, match="does not answer this sign-in"):
        provider.verify_id_token(token, nonce="")


def test_a_token_with_no_subject_is_refused(provider: oidc.OidcProvider, idp: FakeIdp) -> None:
    token = idp.id_token(sub="")
    with pytest.raises(oidc.OidcError, match="no subject"):
        provider.verify_id_token(token, nonce="the-nonce")


@pytest.mark.parametrize("malformed", ["", "a", "a.b", "a.b.c.d", "not-base64.$$$.zz"])
def test_a_malformed_token_is_refused_rather_than_crashing(
    provider: oidc.OidcProvider, malformed: str
) -> None:
    with pytest.raises(oidc.OidcError):
        provider.verify_id_token(malformed, nonce="the-nonce")


# -- the callback ----------------------------------------------------------


def test_a_mismatched_state_is_refused_before_anything_is_redeemed(
    provider: oidc.OidcProvider, idp: FakeIdp
) -> None:
    """Login CSRF: an attacker who can make the browser hit the callback with
    *their* code signs the victim into the attacker's account. The state check
    is the only thing that stops it, and it has to run before the exchange.
    """
    idp.serve(f"{ISSUER}/token", 200, {"id_token": idp.id_token()})
    with pytest.raises(oidc.OidcError, match="state"):
        provider.authenticate(
            {
                "code": "the-code",
                "state": "forged",
                "expected_state": "the-state",
                "nonce": "the-nonce",
                "code_verifier": "v",
            }
        )
    assert not [url for _m, url in idp.calls if url.endswith("/token")]


def test_a_missing_expected_state_fails_closed(provider: oidc.OidcProvider) -> None:
    with pytest.raises(oidc.OidcError, match="state"):
        provider.authenticate({"code": "c", "state": "", "expected_state": ""})


def test_no_code_is_a_fall_through_not_a_failure(provider: oidc.OidcProvider) -> None:
    """ "No OIDC attempt here" and "an OIDC attempt that failed" are different
    outcomes: one falls through to the next provider, the other is something an
    operator has to see.
    """
    assert provider.authenticate({}) is None


def test_a_token_endpoint_error_does_not_echo_its_body(
    provider: oidc.OidcProvider, idp: FakeIdp
) -> None:
    """A token endpoint's error can carry the code or the client id, and this
    message reaches a rendered page.
    """
    idp.serve(f"{ISSUER}/token", 400, {"error": "invalid_grant", "code": "the-secret-code"})
    with pytest.raises(oidc.OidcError) as caught:
        provider.exchange("the-code", _request())
    assert "the-secret-code" not in str(caught.value)
    assert "400" in str(caught.value)


def test_a_token_response_without_an_id_token_is_refused(
    provider: oidc.OidcProvider, idp: FakeIdp
) -> None:
    idp.serve(f"{ISSUER}/token", 200, {"access_token": "an-access-token"})
    with pytest.raises(oidc.OidcError, match="no id_token"):
        provider.exchange("the-code", _request())


def test_the_exchange_sends_the_verifier_and_authenticates_as_the_client(
    provider: oidc.OidcProvider, idp: FakeIdp
) -> None:
    sent: dict[str, Any] = {}

    def capture(url: str, *, method: str = "GET", body: bytes | None = None, headers=None):  # type: ignore[no-untyped-def]
        if url.endswith("/token"):
            sent["body"] = (body or b"").decode()
            sent["headers"] = headers or {}
            return 200, json.dumps({"id_token": idp.id_token()}).encode()
        return idp.fetch(url, method=method, body=body, headers=headers)

    provider.fetch = capture  # type: ignore[assignment]
    provider.exchange("the-code", _request())

    assert "code_verifier=the-verifier" in sent["body"]
    assert "grant_type=authorization_code" in sent["body"]
    expected = base64.b64encode(f"{CLIENT_ID}:{SECRET}".encode()).decode()
    assert sent["headers"]["Authorization"] == f"Basic {expected}"


# -- what it will not claim ------------------------------------------------


def test_an_unverified_email_is_dropped_rather_than_trusted(
    provider: oidc.OidcProvider, idp: FakeIdp
) -> None:
    """At a provider that does not verify addresses, `email` is a string the
    user typed. Carrying it as identity lets anyone claim anyone's address.
    """
    idp.serve(f"{ISSUER}/token", 200, {"id_token": idp.id_token(email_verified=False)})
    principal = provider.authenticate(
        {
            "code": "c",
            "state": "s",
            "expected_state": "s",
            "nonce": "the-nonce",
            "code_verifier": "v",
        }
    )
    assert principal is not None
    assert principal.email is None
    assert principal.subject == f"{ISSUER}#user-42"


def test_no_roles_are_granted_from_the_token(provider: oidc.OidcProvider, idp: FakeIdp) -> None:
    """A claim called `roles` is the IdP's opinion about a different system.
    Mapping it silently is the mass-assignment bug this repo already fixed once
    on the registration form.
    """
    idp.serve(
        f"{ISSUER}/token",
        200,
        {"id_token": idp.id_token(roles=["OWNER"], groups=["admins"])},
    )
    principal = provider.authenticate(
        {
            "code": "c",
            "state": "s",
            "expected_state": "s",
            "nonce": "the-nonce",
            "code_verifier": "v",
        }
    )
    assert principal is not None
    assert principal.roles == frozenset()
    assert principal.organization_id is None


def test_describe_never_returns_the_client_secret(provider: oidc.OidcProvider) -> None:
    described = json.dumps(provider.describe())
    assert SECRET not in described
    assert "massingplan" in described


# -- configuration ---------------------------------------------------------


@pytest.mark.parametrize("missing", ["issuer", "client_id", "client_secret", "redirect_uri"])
def test_incomplete_settings_are_refused(missing: str) -> None:
    fields = {
        "issuer": ISSUER,
        "client_id": CLIENT_ID,
        "client_secret": SECRET,
        "redirect_uri": REDIRECT,
    }
    fields[missing] = ""
    with pytest.raises(oidc.OidcError, match=missing):
        oidc.OidcSettings(**fields)


def test_the_openid_scope_is_required() -> None:
    with pytest.raises(oidc.OidcError, match="openid"):
        oidc.OidcSettings(
            issuer=ISSUER,
            client_id=CLIENT_ID,
            client_secret=SECRET,
            redirect_uri=REDIRECT,
            scopes=("email",),
        )


def test_the_adapter_resolves_through_the_seam() -> None:
    """The seam has existed since P0 with nothing behind it. This is the test
    that it now resolves rather than raising `AdapterUnavailableError`.
    """
    from massingplan.services import identity

    provider = identity.resolve(
        "oidc",
        issuer=ISSUER,
        client_id=CLIENT_ID,
        client_secret=SECRET,
        redirect_uri=REDIRECT,
    )
    assert provider.name == "oidc"
    assert provider.describe()["issuer"] == ISSUER


def test_an_unknown_backend_is_still_refused() -> None:
    from massingplan.services import identity

    with pytest.raises(identity.AdapterUnavailableError):
        identity.resolve("saml")
