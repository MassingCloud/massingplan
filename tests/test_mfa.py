"""The second factor, and the properties that make it worth having.

The tests that matter most here are the negative ones. A second factor that
verifies a good code is easy; the ones that earn their place are: a replayed
code is refused, a recovery code is consumed, and nothing behind
`login_required` is reachable between the two factors.
"""

from __future__ import annotations

import re

import pytest

from massingplan import database
from massingplan.app import create_app
from massingplan.config import Settings
from massingplan.services import accounts, crypto, mfa
from massingplan.services import repository as repo
from massingplan.services.ratelimit import LIMITS

pyotp = pytest.importorskip("pyotp")
pytest.importorskip("segno")
pytest.importorskip("cryptography")

KEY = crypto.generate_key()
PASSWORD = "a-long-enough-passphrase"


@pytest.fixture(autouse=True)
def encryption_key(monkeypatch):  # type: ignore[no-untyped-def]
    monkeypatch.setenv("MASSINGPLAN_ENCRYPTION_KEY", KEY)


@pytest.fixture
def app(tmp_path):  # type: ignore[no-untyped-def]
    application = create_app(
        Settings(
            env="testing",
            secret_key="test-key",
            database_url=f"sqlite:///{tmp_path / 'mfa.db'}",
            rate_limit_enabled=False,
        )
    )
    application.config["TESTING"] = True
    application.config["WTF_CSRF_ENABLED"] = False
    database.create_all()
    with database.session_scope() as session:
        repo.ensure_default_organization(session)
        accounts.register(
            session,
            email="planner@example.com",
            password=PASSWORD,
            organization_id=repo.DEFAULT_ORG_ID,
        )
    return application


def _user(email: str = "planner@example.com"):  # type: ignore[no-untyped-def]
    from sqlalchemy import select

    from massingplan.models import User

    with database.session_scope() as session:
        return session.scalars(select(User).where(User.email == email)).one()


def _sign_in(client, email: str = "planner@example.com", password: str = PASSWORD):  # type: ignore[no-untyped-def]
    return client.post(
        "/auth/sign-in", data={"email": email, "password": password}, follow_redirects=False
    )


def _enrol(client) -> tuple[str, list[str]]:  # type: ignore[no-untyped-def]
    """Walk the real enrolment pages and return (secret, recovery codes)."""
    page = client.get("/account/mfa")
    assert page.status_code == 200
    with client.session_transaction() as session:
        secret = session["mfa_pending_secret"]
        codes = list(session["mfa_pending_recovery"])
    response = client.post("/account/mfa", data={"code": pyotp.TOTP(secret).now()})
    assert response.status_code == 302
    return secret, codes


# -- the crypto underneath -------------------------------------------------


def test_a_secret_round_trips_through_encryption() -> None:
    assert crypto.decrypt(crypto.encrypt("JBSWY3DPEHPK3PXP")) == "JBSWY3DPEHPK3PXP"


def test_ciphertext_is_not_the_plaintext_and_is_not_stable() -> None:
    """Fernet carries a random IV, so two encryptions of one secret differ. If
    they did not, equal ciphertexts would leak that two users share a secret.
    """
    first, second = crypto.encrypt("SAME"), crypto.encrypt("SAME")
    assert first != second
    assert "SAME" not in first


def test_a_tampered_ciphertext_is_refused_rather_than_decrypted() -> None:
    """Fernet is authenticated. Without that, an attacker who can write to the
    database can substitute a TOTP secret they know.
    """
    token = crypto.encrypt("JBSWY3DPEHPK3PXP")
    mangled = token[:-4] + ("aaaa" if not token.endswith("aaaa") else "bbbb")
    with pytest.raises(crypto.EncryptionUnavailableError):
        crypto.decrypt(mangled)


def test_the_wrong_key_says_so_rather_than_blaming_the_data() -> None:
    token = crypto.encrypt("JBSWY3DPEHPK3PXP", key=crypto.generate_key())
    with pytest.raises(crypto.EncryptionUnavailableError, match="ENCRYPTION_KEY"):
        crypto.decrypt(token)


def test_a_missing_key_is_reported_not_crashed(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    monkeypatch.delenv("MASSINGPLAN_ENCRYPTION_KEY", raising=False)
    assert crypto.is_available() is False
    with pytest.raises(crypto.EncryptionUnavailableError, match="gen-key"):
        crypto.encrypt("x")


def test_recovery_codes_avoid_the_characters_people_misread() -> None:
    """These get written on paper and typed back at a stressful moment."""
    codes = crypto.generate_recovery_codes(50)
    assert len(codes) == 50
    assert len(set(codes)) == 50
    assert not set("".join(codes)) & set("l1o0")
    assert all(re.fullmatch(r"[a-z2-9]{4}-[a-z2-9]{4}", c) for c in codes)


def test_recovery_codes_normalise_the_ways_people_type_them() -> None:
    canonical = crypto.hash_recovery_code("w7k2-9fqx")
    for typed in ("W7K2-9FQX", " w7k29fqx ", "w7k2 9fqx", "W7K2 - 9FQX"):
        assert crypto.hash_recovery_code(typed) == canonical


# -- enrolment -------------------------------------------------------------


def test_enrolment_stores_nothing_until_a_code_verifies(app) -> None:  # type: ignore[no-untyped-def]
    """A secret written before the user proves they hold it leaves an account
    demanding a factor nobody can produce, fixable only by an administrator.
    """
    client = app.test_client()
    _sign_in(client)
    client.get("/account/mfa")
    assert mfa.is_enabled(_user()) is False

    client.post("/account/mfa", data={"code": "000000"})
    assert mfa.is_enabled(_user()) is False


def test_a_wrong_code_keeps_the_same_qr(app) -> None:  # type: ignore[no-untyped-def]
    """Reissuing on a typo would invalidate the QR the user just scanned."""
    client = app.test_client()
    _sign_in(client)
    client.get("/account/mfa")
    with client.session_transaction() as session:
        first = session["mfa_pending_secret"]
    response = client.post("/account/mfa", data={"code": "000000"})
    assert response.status_code == 400
    with client.session_transaction() as session:
        assert session["mfa_pending_secret"] == first


def test_enrolment_completes_and_the_secret_is_encrypted_at_rest(app) -> None:  # type: ignore[no-untyped-def]
    client = app.test_client()
    _sign_in(client)
    secret, _codes = _enrol(client)

    user = _user()
    assert mfa.is_enabled(user)
    # The point of the exercise: what is on disk is not the secret.
    assert secret not in (user.mfa_secret_encrypted or "")
    assert crypto.decrypt(user.mfa_secret_encrypted or "") == secret


def test_recovery_codes_are_stored_hashed(app) -> None:  # type: ignore[no-untyped-def]
    client = app.test_client()
    _sign_in(client)
    _secret, codes = _enrol(client)
    user = _user()
    assert len(user.mfa_recovery_hashes) == len(codes)
    assert not set(codes) & set(user.mfa_recovery_hashes)


def test_the_qr_is_inline_and_fetches_nothing(app) -> None:  # type: ignore[no-untyped-def]
    """An <img> pointing at a QR service is a third party you have just shown a
    TOTP secret to, and it would need a hole in `default-src 'self'`.
    """
    client = app.test_client()
    _sign_in(client)
    body = client.get("/account/mfa").get_data(as_text=True)
    assert "<svg" in body
    urls = set(re.findall(r"https?://[^\s\"'<>]+", body))
    assert urls <= {"http://www.w3.org/2000/svg"}, urls


def test_the_secret_is_offered_in_typeable_groups(app) -> None:  # type: ignore[no-untyped-def]
    """A phone that cannot scan -- locked down, or a desktop client -- is common
    enough that the manual path has to be usable rather than merely present.
    """
    client = app.test_client()
    _sign_in(client)
    client.get("/account/mfa")
    with client.session_transaction() as session:
        secret = session["mfa_pending_secret"]
    grouped = mfa.secret_for_display(secret)
    assert grouped.replace(" ", "") == secret
    assert all(len(chunk) <= 4 for chunk in grouped.split())


# -- the challenge ---------------------------------------------------------


def test_a_password_alone_no_longer_signs_you_in(app) -> None:  # type: ignore[no-untyped-def]
    client = app.test_client()
    _sign_in(client)
    _enrol(client)
    client.post("/auth/sign-out")

    response = _sign_in(client)
    assert response.status_code == 302
    assert "/auth/mfa" in response.headers["Location"]


def test_nothing_is_reachable_between_the_two_factors(app) -> None:  # type: ignore[no-untyped-def]
    """The half-authenticated state is the one worth attacking: a session that
    holds a user id but no principal must not be a session that can read data.
    """
    client = app.test_client()
    _sign_in(client)
    _enrol(client)
    client.post("/auth/sign-out")
    _sign_in(client)

    for path in ("/projects", "/account", "/account/mfa"):
        response = client.get(path)
        assert response.status_code in (302, 401), path
        if response.status_code == 302:
            assert "/auth/sign-in" in response.headers["Location"], path


def test_a_good_code_completes_the_sign_in(app) -> None:  # type: ignore[no-untyped-def]
    client = app.test_client()
    _sign_in(client)
    secret, _codes = _enrol(client)
    client.post("/auth/sign-out")
    _sign_in(client)

    response = client.post("/auth/mfa", data={"code": pyotp.TOTP(secret).now()})
    assert response.status_code == 302
    assert client.get("/projects").status_code == 200


def test_a_bad_code_does_not(app) -> None:  # type: ignore[no-untyped-def]
    client = app.test_client()
    _sign_in(client)
    _enrol(client)
    client.post("/auth/sign-out")
    _sign_in(client)

    assert client.post("/auth/mfa", data={"code": "000000"}).status_code == 401
    assert client.get("/projects").status_code in (302, 401)


def test_the_challenge_is_not_reachable_without_a_password_first(app) -> None:  # type: ignore[no-untyped-def]
    """Otherwise the second factor is a first factor, and a stolen phone is
    enough on its own.
    """
    client = app.test_client()
    response = client.get("/auth/mfa")
    assert response.status_code == 302
    assert "/auth/sign-in" in response.headers["Location"]
    assert client.post("/auth/mfa", data={"code": "000000"}).status_code == 302


def test_a_code_cannot_be_replayed_inside_its_window(app) -> None:  # type: ignore[no-untyped-def]
    """TOTP accepts a code for ninety seconds across the drift window. Without
    this, a code captured by a proxy is reusable for exactly as long as an
    attacker needs.
    """
    client = app.test_client()
    _sign_in(client)
    secret, _codes = _enrol(client)
    code = pyotp.TOTP(secret).now()

    client.post("/auth/sign-out")
    _sign_in(client)
    assert client.post("/auth/mfa", data={"code": code}).status_code == 302

    client.post("/auth/sign-out")
    _sign_in(client)
    assert client.post("/auth/mfa", data={"code": code}).status_code == 401


def test_the_next_target_survives_the_second_factor(app) -> None:  # type: ignore[no-untyped-def]
    """Being dumped on the project list after typing a code is a small thing
    that makes people turn the factor off.
    """
    client = app.test_client()
    _sign_in(client)
    secret, _codes = _enrol(client)
    client.post("/auth/sign-out")

    client.post(
        "/auth/sign-in",
        data={"email": "planner@example.com", "password": PASSWORD, "next": "/account"},
    )
    response = client.post("/auth/mfa", data={"code": pyotp.TOTP(secret).now(), "next": "/account"})
    assert response.headers["Location"].endswith("/account")


def test_an_absolute_next_is_still_refused_at_the_second_factor(app) -> None:  # type: ignore[no-untyped-def]
    """The open-redirect guard has to be on both hops. Guarding only sign-in
    leaves the challenge page as the launchpad instead.
    """
    client = app.test_client()
    _sign_in(client)
    secret, _codes = _enrol(client)
    client.post("/auth/sign-out")
    _sign_in(client)

    response = client.post(
        "/auth/mfa",
        data={"code": pyotp.TOTP(secret).now(), "next": "//evil.example.com/"},
    )
    assert "evil.example.com" not in response.headers["Location"]


# -- recovery codes --------------------------------------------------------


def test_a_recovery_code_signs_you_in(app) -> None:  # type: ignore[no-untyped-def]
    client = app.test_client()
    _sign_in(client)
    _secret, codes = _enrol(client)
    client.post("/auth/sign-out")
    _sign_in(client)

    assert client.post("/auth/mfa", data={"code": codes[0]}).status_code == 302
    assert client.get("/projects").status_code == 200


def test_a_used_recovery_code_is_consumed(app) -> None:  # type: ignore[no-untyped-def]
    """A code that still works after use is a password with extra steps."""
    client = app.test_client()
    _sign_in(client)
    _secret, codes = _enrol(client)
    client.post("/auth/sign-out")
    _sign_in(client)
    client.post("/auth/mfa", data={"code": codes[0]})

    assert mfa.remaining_recovery_codes(_user()) == len(codes) - 1

    client.post("/auth/sign-out")
    _sign_in(client)
    assert client.post("/auth/mfa", data={"code": codes[0]}).status_code == 401


def test_recovery_codes_are_accepted_however_they_are_typed(app) -> None:  # type: ignore[no-untyped-def]
    client = app.test_client()
    _sign_in(client)
    _secret, codes = _enrol(client)
    client.post("/auth/sign-out")
    _sign_in(client)

    assert client.post("/auth/mfa", data={"code": codes[0].upper()}).status_code == 302


# -- turning it off --------------------------------------------------------


def test_disabling_needs_the_password(app) -> None:  # type: ignore[no-untyped-def]
    """Otherwise a borrowed session strips the factor that session was supposed
    to be protected by, which is the one moment the factor exists for.
    """
    client = app.test_client()
    _sign_in(client)
    secret, _codes = _enrol(client)
    client.post("/auth/sign-out")
    _sign_in(client)
    client.post("/auth/mfa", data={"code": pyotp.TOTP(secret).now()})

    assert client.post("/account/mfa/disable", data={"password": "wrong"}).status_code == 401
    assert mfa.is_enabled(_user())

    assert client.post("/account/mfa/disable", data={"password": PASSWORD}).status_code == 302
    assert not mfa.is_enabled(_user())


def test_disabling_clears_the_recovery_codes_too(app) -> None:  # type: ignore[no-untyped-def]
    """Leaving them behind means the next enrolment silently inherits a set the
    user believes they replaced.
    """
    client = app.test_client()
    _sign_in(client)
    secret, _codes = _enrol(client)
    client.post("/auth/sign-out")
    _sign_in(client)
    client.post("/auth/mfa", data={"code": pyotp.TOTP(secret).now()})
    client.post("/account/mfa/disable", data={"password": PASSWORD})

    user = _user()
    assert user.mfa_recovery_hashes == []
    assert user.mfa_enabled_at is None


# -- the install without the extras ----------------------------------------


def test_the_page_explains_itself_when_encryption_is_off(app, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """Rather than a traceback, or a checkbox that silently does nothing."""
    client = app.test_client()
    _sign_in(client)
    monkeypatch.delenv("MASSINGPLAN_ENCRYPTION_KEY", raising=False)
    body = client.get("/account/mfa").get_data(as_text=True)
    assert "not available" in body
    assert "MASSINGPLAN_ENCRYPTION_KEY" in body


def test_an_account_without_a_factor_signs_in_as_before(app) -> None:  # type: ignore[no-untyped-def]
    """MFA is opt-in. The unenrolled path must not have acquired a step."""
    client = app.test_client()
    response = _sign_in(client)
    assert response.status_code == 302
    assert "/auth/mfa" not in response.headers["Location"]


# -- the limit -------------------------------------------------------------


def test_the_challenge_is_rate_limited(app) -> None:  # type: ignore[no-untyped-def]
    """A six-digit code with unlimited attempts makes the factor decorative."""
    assert "auth.mfa_challenge" in LIMITS
    assert LIMITS["auth.mfa_challenge"].count < LIMITS["auth.sign_in"].count


def test_the_security_doc_no_longer_says_there_is_no_mfa() -> None:
    """A stale limitation list is as misleading as an absent one, and more
    likely to be trusted -- it reads as though someone checked.
    """
    from pathlib import Path

    text = (
        Path(__file__).resolve().parent.parent.joinpath("SECURITY.md").read_text(encoding="utf-8")
    )
    assert "**No MFA.**" not in text
    # And it must keep saying the part operators get wrong.
    assert "a CI job cannot type a code" in text
