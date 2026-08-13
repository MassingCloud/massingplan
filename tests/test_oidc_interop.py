"""The interop half of OIDC, as far as it can be reached without an IdP.

There is no test here against Entra, Okta, Auth0 or Google, and there cannot
be: no credentials, and the suite blocks non-local sockets on purpose. What a
run against a real provider would mostly catch, though, is not cryptography --
it is the places where providers legitimately differ from each other and from
the one shape a fake issuer happens to have. Those are reachable offline, by
building the fake issuer into the *other* shapes the specification allows.

Three of them, each a real deployment failure rather than a hypothetical:

* **Client authentication.** `client_secret_basic` is the default and not the
  only one. A provider advertising only `client_secret_post` answers 401 to
  every exchange, and nothing on our side explains why.
* **ES256.** The elliptic-curve path was implemented and never once executed.
  Signature verification that has never run is not verification.
* **A JWKS with more than one key.** Real documents carry both sides of a
  rotation and, at several providers, encryption keys as well.
"""

from __future__ import annotations

import json
from typing import Any
from urllib.parse import parse_qsl

import pytest

oidc = pytest.importorskip(
    "massingplan.services.identity.oidc",
    reason="the OIDC adapter is absent (the no-adapters job deletes it)",
)
pytest.importorskip("cryptography")

from .test_oidc import CLIENT_ID, ISSUER, REDIRECT, SECRET, FakeIdp, _b64  # noqa: E402


def _provider(idp: FakeIdp) -> Any:
    return oidc.OidcProvider(
        oidc.OidcSettings(
            issuer=ISSUER, client_id=CLIENT_ID, client_secret=SECRET, redirect_uri=REDIRECT
        ),
        fetcher=idp.fetch,
    )


def _request() -> Any:
    return oidc.AuthorizationRequest(
        state="s", nonce="the-nonce", code_verifier="the-verifier", url=""
    )


def _discovery(**extra: Any) -> dict[str, Any]:
    document = {
        "issuer": ISSUER,
        "authorization_endpoint": f"{ISSUER}/authorize",
        "token_endpoint": f"{ISSUER}/token",
        "jwks_uri": f"{ISSUER}/jwks",
    }
    document.update(extra)
    return document


class Capture:
    """A fetcher that records the token request and answers it."""

    def __init__(self, idp: FakeIdp) -> None:
        self.idp = idp
        self.headers: dict[str, str] = {}
        self.fields: dict[str, str] = {}

    def __call__(
        self,
        url: str,
        *,
        method: str = "GET",
        body: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> tuple[int, bytes]:
        if url.endswith("/token"):
            self.headers = dict(headers or {})
            self.fields = dict(parse_qsl((body or b"").decode()))
            return 200, json.dumps({"id_token": self.idp.id_token()}).encode()
        return self.idp.fetch(url, method=method, body=body, headers=headers)


# -- how the client authenticates ------------------------------------------


def test_basic_is_the_default_when_the_document_says_nothing() -> None:
    """OpenID Connect Discovery names `client_secret_basic` as the default, so
    an issuer that omits the field gets it.
    """
    idp = FakeIdp()
    capture = Capture(idp)
    provider = _provider(idp)
    provider.fetch = capture
    provider.exchange("the-code", _request())

    assert capture.headers["Authorization"].startswith("Basic ")
    assert "client_secret" not in capture.fields


def test_a_provider_that_only_accepts_post_gets_the_secret_in_the_body() -> None:
    """The deployment failure this closes.

    Sending Basic to an issuer that advertises only `client_secret_post` earns
    a 401 on every sign-in, and the previous implementation had no way to do
    anything else -- it sent Basic under a comment asserting every provider
    supports it.
    """
    idp = FakeIdp()
    idp.serve(
        f"{ISSUER}/.well-known/openid-configuration",
        200,
        _discovery(token_endpoint_auth_methods_supported=["client_secret_post"]),
    )
    capture = Capture(idp)
    provider = _provider(idp)
    provider.fetch = capture
    provider.exchange("the-code", _request())

    assert "Authorization" not in capture.headers
    assert capture.fields["client_id"] == CLIENT_ID
    assert capture.fields["client_secret"] == SECRET
    assert capture.fields["code_verifier"] == "the-verifier"


def test_basic_is_preferred_when_the_provider_accepts_both() -> None:
    """Keeping the secret out of the body and the logs where there is a choice."""
    idp = FakeIdp()
    idp.serve(
        f"{ISSUER}/.well-known/openid-configuration",
        200,
        _discovery(
            token_endpoint_auth_methods_supported=["client_secret_post", "client_secret_basic"]
        ),
    )
    capture = Capture(idp)
    provider = _provider(idp)
    provider.fetch = capture
    provider.exchange("the-code", _request())

    assert capture.headers["Authorization"].startswith("Basic ")
    assert "client_secret" not in capture.fields


def test_a_provider_accepting_only_methods_we_do_not_implement_says_so() -> None:
    """`private_key_jwt` is a different key-management story and is absent
    rather than half-built. An operator whose IdP requires it needs to be told
    that, not handed a 401 from the token endpoint.
    """
    idp = FakeIdp()
    idp.serve(
        f"{ISSUER}/.well-known/openid-configuration",
        200,
        _discovery(token_endpoint_auth_methods_supported=["private_key_jwt", "tls_client_auth"]),
    )
    provider = _provider(idp)
    with pytest.raises(oidc.OidcError) as caught:
        provider.exchange("the-code", _request())
    assert "private_key_jwt" in str(caught.value)
    assert "client_secret_basic" in str(caught.value)


# -- ES256, which had never been executed ----------------------------------


class EcIdp(FakeIdp):
    """The same issuer signing with P-256 instead of RSA."""

    def __init__(self) -> None:
        super().__init__()
        from cryptography.hazmat.primitives.asymmetric import ec

        self.ec_key = ec.generate_private_key(ec.SECP256R1())
        self.kid = "ec-key-1"
        self._install_defaults()
        self.serve(f"{ISSUER}/jwks", 200, self._ec_jwks())

    def _ec_jwks(self) -> dict[str, Any]:
        numbers = self.ec_key.public_key().public_numbers()
        return {
            "keys": [
                {
                    "kty": "EC",
                    "crv": "P-256",
                    "kid": self.kid,
                    "use": "sig",
                    "alg": "ES256",
                    "x": _b64(numbers.x.to_bytes(32, "big")),
                    "y": _b64(numbers.y.to_bytes(32, "big")),
                }
            ]
        }

    def sign(self, claims: dict[str, Any], **kw: Any) -> str:  # type: ignore[override]
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.asymmetric import ec
        from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature

        header = {"alg": "ES256", "typ": "JWT", "kid": kw.get("kid", self.kid)}
        head = _b64(json.dumps(header).encode())
        body = _b64(json.dumps(claims).encode())
        der = self.ec_key.sign(f"{head}.{body}".encode("ascii"), ec.ECDSA(hashes.SHA256()))
        # JWS carries raw r||s, not DER -- the conversion the verifier undoes.
        r, s = decode_dss_signature(der)
        raw = r.to_bytes(32, "big") + s.to_bytes(32, "big")
        return f"{head}.{body}.{_b64(raw)}"


def test_an_es256_token_verifies() -> None:
    """The path existed and had never run once. A signature check that has
    never executed is not a signature check.
    """
    idp = EcIdp()
    claims = _provider(idp).verify_id_token(idp.id_token(), nonce="the-nonce")
    assert claims["sub"] == "user-42"


def test_a_tampered_es256_token_does_not_verify() -> None:
    """The half that matters. Without it, an ECDSA path that accepted anything
    would pass the test above."""
    idp = EcIdp()
    token = idp.id_token()
    head, _payload, signature = token.split(".")
    forged = _b64(json.dumps({"iss": ISSUER, "sub": "somebody-else"}).encode())
    with pytest.raises(oidc.OidcError, match="signature"):
        _provider(idp).verify_id_token(f"{head}.{forged}.{signature}", nonce="the-nonce")


def test_an_es256_header_against_an_rsa_key_is_refused() -> None:
    """Algorithm and key type must agree. A provider rotating from RSA to EC
    publishes both for a while, and the header is what says which was used.
    """
    idp = FakeIdp()  # publishes an RSA key only
    token = idp.sign(
        {"iss": ISSUER, "sub": "x", "aud": CLIENT_ID, "nonce": "the-nonce"},
        algorithm="ES256",
        signature=b"\x00" * 64,
    )
    with pytest.raises(oidc.OidcError):
        _provider(idp).verify_id_token(token, nonce="the-nonce")


# -- a JWKS shaped like a real one -----------------------------------------


def test_a_rotation_publishes_two_keys_and_the_kid_picks_one() -> None:
    """Every provider publishes both sides of a rotation for a window. Picking
    by `kid` is the only thing that makes that window survivable.
    """
    from cryptography.hazmat.primitives.asymmetric import rsa

    idp = FakeIdp()
    outgoing = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    numbers = outgoing.public_key().public_numbers()
    published = idp._jwks()
    published["keys"].insert(
        0,
        {
            "kty": "RSA",
            "kid": "the-old-key",
            "use": "sig",
            "alg": "RS256",
            "n": _b64(numbers.n.to_bytes((numbers.n.bit_length() + 7) // 8, "big")),
            "e": _b64(numbers.e.to_bytes((numbers.e.bit_length() + 7) // 8, "big")),
        },
    )
    idp.serve(f"{ISSUER}/jwks", 200, published)

    claims = _provider(idp).verify_id_token(idp.id_token(), nonce="the-nonce")
    assert claims["sub"] == "user-42"


def test_an_encryption_key_is_never_used_to_check_a_signature() -> None:
    """Several providers publish `use: "enc"` keys in the same document.

    With no `kid` in the header, "the only key" has to mean the only *signing*
    key -- counting an encryption key towards that total makes the choice
    ambiguous and the verifier picks by position.
    """
    idp = FakeIdp()
    published = idp._jwks()
    signing = dict(published["keys"][0])
    signing.pop("kid")
    published["keys"] = [
        {**signing, "use": "enc", "kid": "an-encryption-key"},
        signing,
    ]
    idp.serve(f"{ISSUER}/jwks", 200, published)

    token = idp.id_token(kid=None)
    claims = _provider(idp).verify_id_token(token, nonce="the-nonce")
    assert claims["sub"] == "user-42", "the signing key should have been unambiguous"


def test_two_signing_keys_and_no_kid_is_refused_rather_than_guessed() -> None:
    """Ambiguous is not the same as fine. Choosing the first of several keys is
    picking one at random and calling the result verified.
    """
    idp = FakeIdp()
    published = idp._jwks()
    first = dict(published["keys"][0])
    first.pop("kid")
    published["keys"] = [first, dict(first)]
    idp.serve(f"{ISSUER}/jwks", 200, published)

    with pytest.raises(oidc.OidcError, match="no key with kid"):
        _provider(idp).verify_id_token(idp.id_token(kid=None), nonce="the-nonce")
