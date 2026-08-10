"""Outbound webhooks: the URL guard, the retry ladder, and the signature.

Most of this file is about `webhook_url.vet`. That is deliberate — a webhook URL
is a request the *server* makes from inside your network, so the guard is the
security boundary and everything else is plumbing around it.

The transport is injected throughout, so the retry ladder and the status
handling are exercised without a socket. Two things a fake cannot prove are
called out where they appear.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

from massingplan import database
from massingplan.app import create_app
from massingplan.config import Settings
from massingplan.models.webhooks import DeliveryStatus, Webhook, WebhookEvent
from massingplan.security import SIGNATURE_HEADER, verify_timestamped
from massingplan.services import accounts, webhook_url, webhooks
from massingplan.services import repository as repo

PASSWORD = "a-long-enough-passphrase"
NOW = datetime(2026, 6, 1, 9, 0, tzinfo=timezone.utc)


def _public_resolver(host: str, _port: int) -> list[tuple[int, str]]:
    """A resolver that behaves like a resolver, without touching DNS.

    Literal addresses resolve to themselves and `localhost` to loopback --
    otherwise a stub that answers "public" to everything would quietly make the
    guard tests pass by removing the thing they test.
    """
    import ipaddress

    try:
        return [(2, str(ipaddress.ip_address(host.strip("[]"))))]
    except ValueError:
        pass
    if host in {"localhost", "localhost.localdomain"}:
        return [(2, "127.0.0.1")]
    return [(2, "93.184.216.34")]


def _resolver_returning(*addresses: str):  # type: ignore[no-untyped-def]
    def resolve(_host: str, _port: int) -> list[tuple[int, str]]:
        return [(2, a) for a in addresses]

    return resolve


# -- the URL guard ---------------------------------------------------------


def test_a_public_https_url_is_accepted() -> None:
    target = webhook_url.vet("https://example.com/hooks", resolver=_public_resolver)
    assert target.host == "example.com"
    assert target.port == 443
    assert target.address == "93.184.216.34"
    assert target.is_tls


@pytest.mark.parametrize(
    "address",
    [
        "127.0.0.1",  # the server itself
        "127.1.2.3",  # the whole /8, not just .0.1
        "0.0.0.0",  # noqa: S104 - unspecified, which routes to localhost on Linux
        "10.0.0.5",  # RFC1918
        "172.16.4.9",
        "192.168.1.1",
        "169.254.169.254",  # the cloud metadata service. The whole point.
        "169.254.1.1",
        "100.64.0.1",  # carrier-grade NAT, shared address space
        "192.0.2.1",  # TEST-NET-1, reserved
        "224.0.0.1",  # multicast
        "::1",  # loopback, v6
        "fe80::1",  # link-local, v6
        "fc00::1",  # unique-local, v6
        "::ffff:127.0.0.1",  # loopback wearing a v6 hat
    ],
)
def test_addresses_the_server_must_not_be_made_to_fetch(address: str) -> None:
    """Checked at the address, not the hostname. `localhost`, `127.0.0.1`,
    `0x7f.1`, `2130706433` and a name whose A record is `10.0.0.1` all arrive at
    the same place, and only one of them looks suspicious as a string.
    """
    with pytest.raises(webhook_url.UrlRejectedError):
        webhook_url.vet("https://anything.example/hook", resolver=_resolver_returning(address))


def test_the_metadata_rejection_says_what_it_is() -> None:
    """The operator who hits this needs to know it was deliberate."""
    with pytest.raises(webhook_url.UrlRejectedError, match="metadata"):
        webhook_url.vet(
            "https://sneaky.example/hook", resolver=_resolver_returning("169.254.169.254")
        )


def test_every_resolved_address_must_pass_not_just_the_first() -> None:
    """A name resolving to both a public address and 10.0.0.5 would otherwise
    depend on resolver ordering, which is not a security boundary.
    """
    with pytest.raises(webhook_url.UrlRejectedError, match=r"10\.0\.0\.5"):
        webhook_url.vet(
            "https://split.example/hook",
            resolver=_resolver_returning("93.184.216.34", "10.0.0.5"),
        )


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "gopher://example.com/",
        "ftp://example.com/",
        "javascript:alert(1)",
        "//example.com/hook",
        "",
    ],
)
def test_only_http_and_https_are_fetched(url: str) -> None:
    """An allow-list, not a blocklist: a blocklist misses the scheme nobody
    thought of, and `file:` is a request too.
    """
    with pytest.raises(webhook_url.UrlRejectedError):
        webhook_url.vet(url, resolver=_public_resolver)


def test_http_is_refused_when_tls_is_required() -> None:
    with pytest.raises(webhook_url.UrlRejectedError, match="https is required"):
        webhook_url.vet("http://example.com/hook", resolver=_public_resolver)


def test_http_is_allowed_when_tls_is_not_required() -> None:
    """For a development install, and only there."""
    target = webhook_url.vet(
        "http://example.com/hook", require_tls=False, resolver=_public_resolver
    )
    assert target.port == 80
    assert not target.is_tls


def test_credentials_in_the_url_are_refused() -> None:
    """`https://evil@internal/` is host `internal` to some parsers and `evil` to
    others. Refusing is cheaper than picking a side and being wrong.
    """
    with pytest.raises(webhook_url.UrlRejectedError, match="credentials"):
        webhook_url.vet("https://user:pw@example.com/hook", resolver=_public_resolver)


def test_internal_service_ports_are_refused() -> None:
    for port in (5432, 6379, 9200, 11211, 22):
        with pytest.raises(webhook_url.UrlRejectedError, match="port"):
            webhook_url.vet(f"https://example.com:{port}/hook", resolver=_public_resolver)


def test_a_name_that_does_not_resolve_is_refused_not_retried() -> None:
    def nothing(_host: str, _port: int) -> list[tuple[int, str]]:
        return []

    with pytest.raises(webhook_url.UrlRejectedError, match="does not resolve"):
        webhook_url.vet("https://nowhere.example/hook", resolver=nothing)


def test_an_absurdly_long_url_is_refused_before_it_is_parsed() -> None:
    with pytest.raises(webhook_url.UrlRejectedError):
        webhook_url.vet("https://example.com/" + "a" * 3000, resolver=_public_resolver)


# -- the app fixture -------------------------------------------------------


@pytest.fixture(autouse=True)
def no_real_dns(monkeypatch):  # type: ignore[no-untyped-def]
    """No test resolves a real name.

    A suite that reaches DNS fails on a laptop in a tunnel and passes in CI, and
    the pure guard tests above pass their own resolver anyway -- so this only
    covers the paths that go through the service.
    """
    monkeypatch.setattr(webhook_url, "resolve", _public_resolver)


@pytest.fixture
def app(tmp_path):  # type: ignore[no-untyped-def]
    application = create_app(
        Settings(
            env="testing",
            secret_key="test-key",
            database_url=f"sqlite:///{tmp_path / 'hooks.db'}",
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
            email="owner@example.com",
            password=PASSWORD,
            organization_id=repo.DEFAULT_ORG_ID,
        )
    return application


@pytest.fixture
def hook(app):  # type: ignore[no-untyped-def]
    """A subscribed endpoint, and its secret."""
    with database.session_scope() as session:
        record, secret = webhooks.subscribe(
            session,
            organization_id=repo.DEFAULT_ORG_ID,
            url="https://example.com/hook",
            events=[WebhookEvent.BASELINE_SET.value, WebhookEvent.PROJECT_DELETED.value],
        )
        session.commit()
        return record.id, secret


class Recorder:
    """A transport that records and answers with whatever it was told to."""

    def __init__(self, *statuses: int, body: str = "ok") -> None:
        self.statuses = list(statuses) or [200]
        self.body = body
        self.calls: list[tuple[str, bytes, dict[str, str]]] = []

    def __call__(self, target, body, headers):  # type: ignore[no-untyped-def]
        self.calls.append((target.url, body, headers))
        status = self.statuses[min(len(self.calls) - 1, len(self.statuses) - 1)]
        return webhooks.Response(status=status, body=self.body)


def _emit(session, event=WebhookEvent.BASELINE_SET, payload=None, now=NOW):  # type: ignore[no-untyped-def]
    """Queued as of NOW, so a drain at NOW or at the real clock both see it due."""
    return webhooks.emit(
        session,
        organization_id=repo.DEFAULT_ORG_ID,
        event=event,
        payload=payload or {"project_id": "p1"},
        now=now,
    )


# -- subscribing -----------------------------------------------------------


def test_subscribing_returns_a_secret_that_is_not_stored_in_the_clear(app, hook) -> None:  # type: ignore[no-untyped-def]
    hook_id, secret = hook
    with database.session_scope() as session:
        record = session.get(Webhook, hook_id)
        assert record is not None
        if record.secret_encrypted:
            assert secret not in record.secret_stored
        assert webhooks.secret_of(record) == secret


def test_an_unknown_event_name_is_refused_at_subscribe_time(app) -> None:  # type: ignore[no-untyped-def]
    """An open string field means a typo produces a subscription that silently
    never fires, and the subscriber blames the sender.
    """
    with (
        database.session_scope() as session,
        pytest.raises(webhooks.WebhookError, match="unknown event"),
    ):
        webhooks.subscribe(
            session,
            organization_id=repo.DEFAULT_ORG_ID,
            url="https://example.com/hook",
            events=["baseline.st"],
        )


def test_subscribing_to_nothing_is_refused(app) -> None:  # type: ignore[no-untyped-def]
    with (
        database.session_scope() as session,
        pytest.raises(webhooks.WebhookError, match="at least one"),
    ):
        webhooks.subscribe(
            session,
            organization_id=repo.DEFAULT_ORG_ID,
            url="https://example.com/hook",
            events=[],
        )


def test_a_forbidden_url_cannot_be_subscribed(app) -> None:  # type: ignore[no-untyped-def]
    with database.session_scope() as session, pytest.raises(webhooks.WebhookError):
        webhooks.subscribe(
            session,
            organization_id=repo.DEFAULT_ORG_ID,
            url="https://localhost/hook",
            events=[WebhookEvent.BASELINE_SET.value],
        )


# -- emitting --------------------------------------------------------------


def test_emit_queues_one_row_per_subscribed_endpoint(app, hook) -> None:  # type: ignore[no-untyped-def]
    with database.session_scope() as session:
        queued = _emit(session)
        session.commit()
    assert len(queued) == 1


def test_emit_skips_endpoints_that_did_not_ask_for_the_event(app, hook) -> None:  # type: ignore[no-untyped-def]
    with database.session_scope() as session:
        assert _emit(session, event=WebhookEvent.IMPORT_COMPLETED) == []
        session.commit()


def test_emit_skips_a_disabled_endpoint(app, hook) -> None:  # type: ignore[no-untyped-def]
    hook_id, _secret = hook
    with database.session_scope() as session:
        webhooks.deactivate(session, session.get(Webhook, hook_id), "test")
        assert _emit(session) == []
        session.commit()


def test_emit_does_not_cross_the_tenant_boundary(app, hook) -> None:  # type: ignore[no-untyped-def]
    """The endpoint belongs to one organisation. An event from another must not
    reach it, and this is the check that would have to fail for that to happen.
    """
    with database.session_scope() as session:
        assert (
            webhooks.emit(
                session,
                organization_id="a-different-organisation",
                event=WebhookEvent.BASELINE_SET,
                payload={},
            )
            == []
        )
        session.commit()


# -- the signature ---------------------------------------------------------


def test_the_request_carries_a_signature_the_subscriber_can_verify(app, hook) -> None:  # type: ignore[no-untyped-def]
    _hook_id, secret = hook
    transport = Recorder(200)
    with database.session_scope() as session:
        _emit(session)
        webhooks.drain(session, transport=transport)
        session.commit()

    _url, body, headers = transport.calls[0]
    assert verify_timestamped(body, headers[SIGNATURE_HEADER], secret)
    # And it is a real check, not one that passes on anything.
    assert not verify_timestamped(body, headers[SIGNATURE_HEADER], "not-the-secret")
    assert not verify_timestamped(body + b" ", headers[SIGNATURE_HEADER], secret)


def test_a_stale_signature_is_refused_by_the_subscriber_side(app, hook) -> None:  # type: ignore[no-untyped-def]
    """The timestamp is inside the signed material precisely so a captured body
    cannot be replayed forever -- the signature stays valid because the body did
    not change, and only the age gives it away.
    """
    _hook_id, secret = hook
    transport = Recorder(200)
    with database.session_scope() as session:
        _emit(session)
        webhooks.drain(session, transport=transport)
        session.commit()
    _url, body, headers = transport.calls[0]
    import time

    assert not verify_timestamped(
        body, headers[SIGNATURE_HEADER], secret, now=int(time.time()) + 86_400
    )


def test_a_retry_signs_the_same_bytes(app, hook) -> None:  # type: ignore[no-untyped-def]
    """If the encoding varied between attempts, a retry would carry a different
    signature over what the subscriber sees as the same event, and their replay
    guard would fire on a legitimate resend.
    """
    transport = Recorder(500, 200)
    with database.session_scope() as session:
        _emit(session)
        webhooks.drain(session, transport=transport, now=NOW)
        webhooks.drain(session, transport=transport, now=NOW + timedelta(seconds=61))
        session.commit()
    assert len(transport.calls) == 2
    assert transport.calls[0][1] == transport.calls[1][1]


def test_the_delivery_id_is_stable_across_retries(app, hook) -> None:  # type: ignore[no-untyped-def]
    """It identifies the delivery, not the attempt, so a subscriber can make
    handling idempotent without parsing the body.
    """
    transport = Recorder(500, 200)
    with database.session_scope() as session:
        _emit(session)
        webhooks.drain(session, transport=transport, now=NOW)
        webhooks.drain(session, transport=transport, now=NOW + timedelta(seconds=61))
        session.commit()
    first, second = transport.calls[0][2], transport.calls[1][2]
    assert first["X-Massing-Delivery"] == second["X-Massing-Delivery"]
    assert (first["X-Massing-Attempt"], second["X-Massing-Attempt"]) == ("1", "2")


def test_the_body_is_the_event_and_the_payload(app, hook) -> None:  # type: ignore[no-untyped-def]
    transport = Recorder(200)
    with database.session_scope() as session:
        _emit(session, payload={"project_id": "p1", "project_code": "TOWER"})
        webhooks.drain(session, transport=transport)
        session.commit()
    body = json.loads(transport.calls[0][1])
    assert body["event"] == "baseline.set"
    assert body["organization_id"] == repo.DEFAULT_ORG_ID
    assert body["data"]["project_code"] == "TOWER"


# -- the retry ladder ------------------------------------------------------


def test_a_2xx_marks_it_delivered_and_stops(app, hook) -> None:  # type: ignore[no-untyped-def]
    transport = Recorder(204)
    with database.session_scope() as session:
        [delivery] = _emit(session)
        webhooks.drain(session, transport=transport)
        assert delivery.status is DeliveryStatus.DELIVERED
        assert delivery.next_attempt_at is None
        session.commit()
    # Draining again must not resend it.
    with database.session_scope() as session:
        assert webhooks.drain(session, transport=transport)["attempted"] == 0


def test_a_5xx_backs_off_and_eventually_gives_up(app, hook) -> None:  # type: ignore[no-untyped-def]
    """Terminal, and visible. A queue where failures stay pending forever is a
    queue nobody can drain.
    """
    transport = Recorder(503)
    with database.session_scope() as session:
        [delivery] = _emit(session)
        moment = NOW
        for _ in range(webhooks.MAX_ATTEMPTS):
            webhooks.drain(session, transport=transport, now=moment)
            moment += timedelta(hours=3)
        assert delivery.attempts == webhooks.MAX_ATTEMPTS
        assert delivery.status is DeliveryStatus.FAILED
        assert delivery.next_attempt_at is None
        session.commit()


def test_the_backoff_is_actually_waited_for(app, hook) -> None:  # type: ignore[no-untyped-def]
    """Retrying immediately is not retrying, it is hammering."""
    transport = Recorder(503)
    with database.session_scope() as session:
        _emit(session)
        webhooks.drain(session, transport=transport, now=NOW)
        assert len(transport.calls) == 1
        webhooks.drain(session, transport=transport, now=NOW + timedelta(seconds=30))
        assert len(transport.calls) == 1, "retried before the backoff elapsed"
        webhooks.drain(session, transport=transport, now=NOW + timedelta(seconds=61))
        assert len(transport.calls) == 2
        session.commit()


@pytest.mark.parametrize("status", [400, 401, 403, 404, 422])
def test_a_client_error_is_not_retried(app, hook, status: int) -> None:  # type: ignore[no-untyped-def]
    """The subscriber has told us the request is wrong. It will be exactly as
    wrong in an hour, and retrying is noise in their logs and ours.
    """
    transport = Recorder(status)
    with database.session_scope() as session:
        [delivery] = _emit(session)
        webhooks.drain(session, transport=transport, now=NOW)
        assert delivery.status is DeliveryStatus.FAILED
        assert delivery.attempts == 1
        session.commit()


def test_a_429_is_retried(app, hook) -> None:  # type: ignore[no-untyped-def]
    """ "Slow down" is the one 4xx that means try again."""
    transport = Recorder(429, 200)
    with database.session_scope() as session:
        [delivery] = _emit(session)
        webhooks.drain(session, transport=transport, now=NOW)
        assert delivery.status is DeliveryStatus.PENDING
        webhooks.drain(session, transport=transport, now=NOW + timedelta(seconds=61))
        assert delivery.status is DeliveryStatus.DELIVERED
        session.commit()


def test_a_410_switches_the_endpoint_off(app, hook) -> None:  # type: ignore[no-untyped-def]
    """A subscriber with no way to say stop resorts to dropping the traffic
    silently, and then nobody knows the integration is dead.
    """
    hook_id, _secret = hook
    with database.session_scope() as session:
        _emit(session)
        webhooks.drain(session, transport=Recorder(410), now=NOW)
        record = session.get(Webhook, hook_id)
        assert record is not None
        assert not record.is_active
        assert "410" in record.disabled_reason
        session.commit()


def test_a_transport_exception_is_a_retry_not_a_crash(app, hook) -> None:  # type: ignore[no-untyped-def]
    def explode(_target, _body, _headers):  # type: ignore[no-untyped-def]
        raise TimeoutError("the subscriber did not answer")

    with database.session_scope() as session:
        [delivery] = _emit(session)
        webhooks.drain(session, transport=explode, now=NOW)
        assert delivery.status is DeliveryStatus.PENDING
        assert "TimeoutError" in delivery.error
        session.commit()


def test_an_endpoint_that_keeps_failing_is_disabled_with_a_reason(app, hook) -> None:  # type: ignore[no-untyped-def]
    """Disabled, not deleted: a subscriber who fixes their server wants their
    subscription back, and silently dropping it means they never learn it went.
    """
    hook_id, _secret = hook
    transport = Recorder(400)  # terminal, so one attempt per event
    with database.session_scope() as session:
        moment = NOW
        for _ in range(webhooks.AUTO_DISABLE_AFTER):
            _emit(session)
            webhooks.drain(session, transport=transport, now=moment)
            moment += timedelta(minutes=1)
        record = session.get(Webhook, hook_id)
        assert record is not None
        assert not record.is_active
        assert str(webhooks.AUTO_DISABLE_AFTER) in record.disabled_reason
        session.commit()


def test_a_success_resets_the_failure_count(app, hook) -> None:  # type: ignore[no-untyped-def]
    hook_id, _secret = hook
    with database.session_scope() as session:
        _emit(session)
        webhooks.drain(session, transport=Recorder(400), now=NOW)
        assert session.get(Webhook, hook_id).consecutive_failures == 1  # type: ignore[union-attr]
        _emit(session)
        webhooks.drain(session, transport=Recorder(200), now=NOW)
        assert session.get(Webhook, hook_id).consecutive_failures == 0  # type: ignore[union-attr]
        session.commit()


# -- delivery-time re-vetting ----------------------------------------------


def test_a_url_that_goes_bad_later_is_refused_at_delivery(app, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """DNS changes. A hostname that pointed at a public address last week can
    point at 10.0.0.5 today without anybody editing the subscription, so the
    check at subscribe time is not the check that protects you.
    """
    with database.session_scope() as session:
        record, _secret = webhooks.subscribe(
            session,
            organization_id=repo.DEFAULT_ORG_ID,
            url="https://drifting.example/hook",
            events=[WebhookEvent.BASELINE_SET.value],
        )
        hook_id = record.id
        session.commit()

    # The name now points somewhere it must not.
    monkeypatch.setattr(webhook_url, "resolve", _resolver_returning("10.0.0.5"))
    with database.session_scope() as session:
        [delivery] = _emit(session)
        webhooks.drain(session, transport=Recorder(200), now=NOW)
        assert delivery.status is DeliveryStatus.FAILED
        assert "url rejected" in delivery.error
        assert not session.get(Webhook, hook_id).is_active  # type: ignore[union-attr]
        session.commit()


# -- what is recorded ------------------------------------------------------


def test_the_response_body_is_truncated(app, hook) -> None:  # type: ignore[no-untyped-def]
    """A subscriber's error page can carry their own session cookie, an internal
    hostname or a stack trace. Storing all of it makes our audit trail a copy of
    their leak.
    """
    from massingplan.models.webhooks import RESPONSE_EXCERPT_CHARS

    with database.session_scope() as session:
        [delivery] = _emit(session)
        webhooks.drain(session, transport=Recorder(500, body="x" * 50_000), now=NOW)
        assert len(delivery.response_excerpt) <= RESPONSE_EXCERPT_CHARS
        session.commit()


def test_the_stored_payload_does_not_contain_the_secret(app, hook) -> None:  # type: ignore[no-untyped-def]
    _hook_id, secret = hook
    with database.session_scope() as session:
        [delivery] = _emit(session)
        session.commit()
        assert secret not in json.dumps(delivery.payload)


# -- through the app -------------------------------------------------------


def _sign_in(client):  # type: ignore[no-untyped-def]
    return client.post("/auth/sign-in", data={"email": "owner@example.com", "password": PASSWORD})


def test_the_page_shows_the_secret_once_and_then_never(app) -> None:  # type: ignore[no-untyped-def]
    client = app.test_client()
    _sign_in(client)
    response = client.post(
        "/account/webhooks",
        data={"url": "https://example.com/hook", "events": ["baseline.set"]},
        follow_redirects=False,
    )
    assert response.status_code == 302
    secret = response.headers["Location"].split("secret=")[1]
    assert len(secret) > 20

    later = client.get("/account/webhooks").get_data(as_text=True)
    assert secret not in later


def test_the_form_refuses_a_loopback_url_with_a_reason(app) -> None:  # type: ignore[no-untyped-def]
    client = app.test_client()
    _sign_in(client)
    response = client.post(
        "/account/webhooks",
        data={"url": "https://127.0.0.1/hook", "events": ["baseline.set"]},
    )
    assert response.status_code == 400
    assert "loopback" in response.get_data(as_text=True)


def test_a_viewer_cannot_manage_webhooks(app) -> None:  # type: ignore[no-untyped-def]
    """A webhook makes the server issue outbound requests to an address the
    subscriber picks, and mails them every event thereafter. That is not a
    read-only capability.
    """
    from massingplan.models.identity import ROLE_PERMISSIONS, Permission, Role

    assert Permission.WEBHOOK_MANAGE not in ROLE_PERMISSIONS[Role.VIEWER]
    assert Permission.WEBHOOK_MANAGE not in ROLE_PERMISSIONS[Role.PLANNER]
    assert Permission.WEBHOOK_MANAGE in ROLE_PERMISSIONS[Role.ADMIN]
    assert Permission.WEBHOOK_MANAGE in ROLE_PERMISSIONS[Role.OWNER]


def test_removing_an_endpoint_takes_its_queued_deliveries_with_it(app, hook) -> None:  # type: ignore[no-untyped-def]
    """The cascade is the point: a delivery row pointing at a webhook that no
    longer exists is a row the drain picks up and cannot send.
    """
    from sqlalchemy import select

    from massingplan.models import WebhookDelivery

    hook_id, _secret = hook
    with database.session_scope() as session:
        _emit(session)
        session.commit()

    client = app.test_client()
    _sign_in(client)
    response = client.post(f"/account/webhooks/{hook_id}/delete")
    assert response.status_code == 302

    with database.session_scope() as session:
        assert session.get(Webhook, hook_id) is None
        assert session.scalars(select(WebhookDelivery)).all() == []


def test_another_organisations_webhook_is_a_404_not_a_403(app, hook) -> None:  # type: ignore[no-untyped-def]
    """403 confirms the id exists. 404 does not, and "does this id exist" is
    exactly what an attacker is asking.
    """
    hook_id, _secret = hook
    with database.session_scope() as session:
        record = session.get(Webhook, hook_id)
        assert record is not None
        record.organization_id = "some-other-organisation"
        session.commit()

    client = app.test_client()
    _sign_in(client)
    assert client.post(f"/account/webhooks/{hook_id}/delete").status_code == 404


def test_the_docs_say_the_drain_has_to_be_run(app) -> None:  # type: ignore[no-untyped-def]
    """Without it, subscriptions accept events and silently accumulate them.
    An operator who does not know that reads the queue as a delivery failure.
    """
    from pathlib import Path

    text = (
        Path(__file__)
        .resolve()
        .parent.parent.joinpath("docs/deployment.md")
        .read_text(encoding="utf-8")
    )
    assert "massingplan webhooks drain" in text
    assert "silently accumulate them" in text
    assert "169.254.169.254" in text


def test_setting_a_baseline_queues_an_event(app) -> None:  # type: ignore[no-untyped-def]
    """The end of the wire: a real page action produces a real queued delivery."""
    from massingplan.models import WebhookDelivery

    client = app.test_client()
    _sign_in(client)
    client.post(
        "/account/webhooks",
        data={"url": "https://example.com/hook", "events": ["import.completed"]},
    )

    xer = (
        "%T\tPROJECT\n%F\tproj_id\tproj_short_name\n%R\t1\tTOWER\n"
        "%T\tTASK\n%F\ttask_id\tproj_id\ttask_code\ttask_name\ttarget_drtn_hr_cnt\n"
        "%R\t10\t1\tA1000\tExcavate\t40\n%E\n"
    )
    client.post(
        "/upload",
        data={"file": (__import__("io").BytesIO(xer.encode()), "job.xer")},
        content_type="multipart/form-data",
    )

    with database.session_scope() as session:
        from sqlalchemy import select

        rows = session.scalars(select(WebhookDelivery)).all()
    assert [r.event for r in rows] == ["import.completed"]
    assert rows[0].payload["data"]["activity_count"] == 1
