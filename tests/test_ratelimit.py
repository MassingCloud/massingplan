"""Rate limiting, and an honest account of its one weakness.

The tests that matter most are the two that pin the *limitation*: the store is
per-process, and the code says so rather than letting an operator believe a
number four times larger than the one they configured.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from massingplan import database
from massingplan.app import create_app
from massingplan.config import Settings
from massingplan.services import accounts
from massingplan.services import repository as repo
from massingplan.services.ratelimit import (
    LIMITS,
    Limit,
    MemoryStore,
    RateLimiter,
    warn_if_multi_worker,
)


@pytest.fixture
def app(tmp_path):  # type: ignore[no-untyped-def]
    application = create_app(
        Settings(
            env="testing",
            secret_key="test-key",
            database_url=f"sqlite:///{tmp_path / 'rl.db'}",
        )
    )
    application.config["TESTING"] = True
    application.config["WTF_CSRF_ENABLED"] = False
    database.create_all()
    with database.session_scope() as session:
        repo.ensure_default_organization(session)
        accounts.register(
            session,
            email="rl@example.com",
            password="a-long-enough-passphrase",
            organization_id=repo.DEFAULT_ORG_ID,
        )
    return application


# -- the limiter itself ----------------------------------------------------


def test_it_allows_up_to_the_limit_and_then_refuses() -> None:
    limiter = RateLimiter()
    LIMITS["test.endpoint"] = Limit(3, 60)
    try:
        decisions = [limiter.check("test.endpoint", "someone", now=100.0) for _ in range(4)]
    finally:
        del LIMITS["test.endpoint"]
    assert [d.allowed for d in decisions] == [True, True, True, False]  # type: ignore[union-attr]
    assert decisions[2].remaining == 0  # type: ignore[union-attr]
    assert decisions[3].retry_after_seconds > 0  # type: ignore[union-attr]


def test_the_window_resets() -> None:
    limiter = RateLimiter()
    LIMITS["test.endpoint"] = Limit(1, 60)
    try:
        assert limiter.check("test.endpoint", "s", now=100.0).allowed  # type: ignore[union-attr]
        assert not limiter.check("test.endpoint", "s", now=100.0).allowed  # type: ignore[union-attr]
        assert limiter.check("test.endpoint", "s", now=161.0).allowed  # type: ignore[union-attr]
    finally:
        del LIMITS["test.endpoint"]


def test_two_callers_have_separate_budgets() -> None:
    limiter = RateLimiter()
    LIMITS["test.endpoint"] = Limit(1, 60)
    try:
        assert limiter.check("test.endpoint", "a", now=100.0).allowed  # type: ignore[union-attr]
        assert limiter.check("test.endpoint", "b", now=100.0).allowed  # type: ignore[union-attr]
    finally:
        del LIMITS["test.endpoint"]


def test_an_unlisted_endpoint_is_not_limited() -> None:
    """Unlimited by default, deliberately: adding an endpoint must not silently
    inherit a number somebody chose for a different one.
    """
    assert RateLimiter().check("something.new", "someone") is None


def test_the_store_sweeps_so_it_is_not_a_slow_leak() -> None:
    """One entry per distinct client, forever, is a leak that looks like nothing
    until it is a page of RSS.
    """
    store = MemoryStore()
    limit = Limit(1, 1)
    for index in range(10_100):
        store.hit(f"k{index}", limit, 100.0)
    store.hit("later", limit, 100_000.0)
    assert len(store._windows) < 10_100


# -- the limitation, stated ------------------------------------------------


def test_describe_says_the_store_is_per_process() -> None:
    """A limiter that silently multiplies by the worker count is worse than
    none, because it is believed.
    """
    described = RateLimiter().describe()
    assert described["store"] == "memory"
    assert "per process" in str(described["scope"])
    assert "ingress" in str(described["scope"])


def test_it_warns_at_startup_with_more_than_one_worker(caplog) -> None:  # type: ignore[no-untyped-def]
    with caplog.at_level(logging.WARNING):
        warn_if_multi_worker(RateLimiter(), workers=4)
    assert "4 times larger" in caplog.text
    caplog.clear()
    with caplog.at_level(logging.WARNING):
        warn_if_multi_worker(RateLimiter(), workers=1)
    assert caplog.text == ""


def test_the_security_doc_no_longer_claims_there_is_none() -> None:
    """SECURITY.md listed "no rate limiting" under what is missing. Now that it
    exists the doc has to stop saying otherwise -- a stale limitation list is as
    misleading as an absent one, and more likely to be trusted.
    """
    text = (
        Path(__file__).resolve().parent.parent.joinpath("SECURITY.md").read_text(encoding="utf-8")
    )
    assert "No rate limiting on the HTTP surface" not in text
    assert "per-process" in text


# -- through the app -------------------------------------------------------


def test_repeated_sign_in_attempts_are_throttled(app) -> None:  # type: ignore[no-untyped-def]
    """Account lock-out protects one account. This protects the endpoint, which
    is what a spray across many accounts attacks.
    """
    client = app.test_client()
    limit = LIMITS["auth.sign_in"].count
    statuses = [
        client.post(
            "/auth/sign-in",
            data={"email": f"u{i}@example.com", "password": "wrong-wrong-wrong"},
        ).status_code
        for i in range(limit + 2)
    ]
    assert 429 in statuses
    assert statuses[0] != 429


def test_a_throttled_response_says_when_to_come_back(app) -> None:  # type: ignore[no-untyped-def]
    client = app.test_client()
    response = None
    for _ in range(LIMITS["auth.sign_in"].count + 1):
        response = client.post(
            "/auth/sign-in", data={"email": "x@example.com", "password": "wrong-wrong-wrong"}
        )
    assert response is not None
    assert response.status_code == 429
    assert int(response.headers["Retry-After"]) > 0


def test_the_json_api_gets_a_json_429(app) -> None:  # type: ignore[no-untyped-def]
    with database.session_scope() as session:
        plaintext, _record = accounts.issue_api_key(
            session, organization_id=repo.DEFAULT_ORG_ID, name="CI"
        )
    client = app.test_client()
    body = {
        "data_date": "2026-06-01",
        "iterations": 1,
        "activities": [{"id": "A", "duration_days": 1}],
    }
    headers = {"Authorization": f"Bearer {plaintext}"}
    response = None
    for _ in range(LIMITS["schedule_api.risk"].count + 1):
        response = client.post("/api/massingplan/v1/risk", json=body, headers=headers)
    assert response is not None
    assert response.status_code == 429
    payload = response.get_json()
    assert payload["error"]["code"] == "rate_limited"
    assert payload["error"]["retry_after_seconds"] > 0


def test_an_ordinary_page_is_not_throttled(app) -> None:  # type: ignore[no-untyped-def]
    """Reading a page is cheap and legitimate; limiting it turns a busy planner
    into a support ticket.
    """
    client = app.test_client()
    assert all(client.get("/demo").status_code == 200 for _ in range(40))


def test_the_limiter_can_be_switched_off_for_a_harness(tmp_path) -> None:  # type: ignore[no-untyped-def]
    application = create_app(
        Settings(
            env="testing",
            secret_key="k",
            rate_limit_enabled=False,
            database_url=f"sqlite:///{tmp_path / 'off.db'}",
        )
    )
    application.config["WTF_CSRF_ENABLED"] = False
    database.create_all()
    client = application.test_client()
    statuses = {
        client.post(
            "/auth/sign-in", data={"email": "a@b.c", "password": "wrong-wrong-wrong"}
        ).status_code
        for _ in range(LIMITS["auth.sign_in"].count + 3)
    }
    assert 429 not in statuses
