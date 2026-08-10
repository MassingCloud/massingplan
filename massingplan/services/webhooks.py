"""Outbound webhooks: subscribe, queue, deliver, retry.

The primitives in `security.py` have existed since the first commit and nothing
called them. This is what calls them.

**Queued, not inline.** `emit()` writes delivery rows in the caller's
transaction and returns. Nothing is sent during the request. A subscriber whose
endpoint hangs must not make somebody's "set baseline" hang with it, and an
event that is written in the same transaction as the change it describes cannot
survive a rollback of that change -- or go missing when the change commits.

**Draining is explicit.** `drain()` is called by `massingplan webhooks drain`,
which you run from cron or a sidecar. There is no thread quietly started at
import time: a background thread in a `gunicorn` worker gets four copies with
four workers, and each one races the others for the same rows.

**Failures back off and then stop.** Five attempts over roughly two hours, then
the delivery is `FAILED` -- terminal, and visible. A queue where failures stay
`pending` forever is a queue nobody can drain. An endpoint that fails
`AUTO_DISABLE_AFTER` times in a row is disabled with a reason, not deleted; a
subscriber who fixes their server wants their subscription back.

**The transport is injected.** `deliver()` takes a callable, so the tests
exercise the retry ladder, the status handling and the signature without a
socket. The real one is `HttpTransport`, and it does not follow redirects --
see `webhook_url` for why that is not a detail.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models.webhooks import (
    RESPONSE_EXCERPT_CHARS,
    DeliveryStatus,
    Webhook,
    WebhookDelivery,
    WebhookEvent,
)
from ..security import SIGNATURE_HEADER, sign_with_timestamp
from . import crypto, webhook_url

logger = logging.getLogger("massingplan.webhooks")

#: Attempt 1 is immediate; the rest wait this long after the previous one.
#: Roughly two hours in total, which covers a deploy or a short outage without
#: holding a row for a day. No jitter: each delivery already backs off from its
#: own creation time, so a burst of events queued together does not retry in
#: lockstep the way a shared schedule would.
BACKOFF_SECONDS = (60, 300, 1_500, 5_400)
MAX_ATTEMPTS = len(BACKOFF_SECONDS) + 1

#: Consecutive failed deliveries before the endpoint itself is switched off.
AUTO_DISABLE_AFTER = 20

#: A subscriber gets this long to answer. Short on purpose: a webhook receiver
#: should acknowledge and do its work afterwards, and one that thinks for a
#: minute is one whose backlog becomes ours.
TIMEOUT_SECONDS = 10

#: Retrying these never helps -- the subscriber has told us the request is
#: wrong, and it will be exactly as wrong in an hour.
TERMINAL_STATUSES = frozenset({400, 401, 403, 404, 405, 406, 411, 413, 414, 415, 422})

#: "This endpoint is gone, stop sending." Honoured, because a subscriber with no
#: way to say stop resorts to dropping the traffic silently.
GONE_STATUS = 410


class WebhookError(RuntimeError):
    """A subscription could not be created or changed."""


@dataclass(frozen=True)
class Response:
    status: int
    body: str = ""


Transport = Callable[[webhook_url.Target, bytes, dict[str, str]], Response]


# -- subscriptions ---------------------------------------------------------


def subscribe(
    session: Session,
    *,
    organization_id: str,
    url: str,
    events: list[str],
    name: str = "",
    created_by_id: str | None = None,
    require_tls: bool = True,
) -> tuple[Webhook, str]:
    """Create an endpoint. Returns it and the signing secret, shown once.

    The secret is generated here rather than accepted from the caller. A secret
    somebody chose is a secret somebody reused, and one typed into a form has
    been in a browser's history.
    """
    unknown = [e for e in events if e not in {m.value for m in WebhookEvent}]
    if unknown:
        raise WebhookError(
            f"unknown event(s): {', '.join(sorted(unknown))}. Known events are "
            f"{', '.join(sorted(m.value for m in WebhookEvent))}"
        )
    if not events:
        raise WebhookError("subscribe to at least one event, or the endpoint never fires")

    try:
        target = webhook_url.vet(url, require_tls=require_tls)
    except webhook_url.UrlRejectedError as exc:
        raise WebhookError(str(exc)) from exc

    secret = crypto.generate_key()
    # Encrypted where a key is configured, in the clear where it is not, with
    # the choice recorded on the row. Refusing to work without an encryption key
    # would make webhooks depend on MFA's optional extra; storing plaintext
    # while claiming otherwise would be worse than either.
    encrypted = crypto.is_available()
    hook = Webhook(
        organization_id=organization_id,
        created_by_id=created_by_id,
        name=name or target.host,
        url=target.url,
        secret_stored=crypto.encrypt(secret) if encrypted else secret,
        secret_encrypted=encrypted,
        events=sorted(set(events)),
    )
    session.add(hook)
    session.flush()
    return hook, secret


def secret_of(hook: Webhook) -> str:
    """The signing secret, whichever way it was stored."""
    return crypto.decrypt(hook.secret_stored) if hook.secret_encrypted else hook.secret_stored


def deactivate(session: Session, hook: Webhook, reason: str = "") -> None:
    hook.is_active = False
    hook.disabled_at = datetime.now(tz=timezone.utc)
    hook.disabled_reason = reason[:300]
    session.flush()


# -- emitting --------------------------------------------------------------


def emit(
    session: Session,
    *,
    organization_id: str,
    event: WebhookEvent | str,
    payload: dict[str, Any],
    now: datetime | None = None,
) -> list[WebhookDelivery]:
    """Queue this event for every endpoint subscribed to it.

    Called inside the caller's transaction, deliberately. An event row that
    commits separately from the change it describes is an event about something
    that did not happen, or a change nobody was told about.
    """
    name = event.value if isinstance(event, WebhookEvent) else event
    hooks = session.scalars(
        select(Webhook)
        .where(Webhook.organization_id == organization_id)
        .where(Webhook.is_active.is_(True))
    ).all()

    body = {"event": name, "organization_id": organization_id, "data": payload}
    moment = now or datetime.now(tz=timezone.utc)
    queued = []
    for hook in hooks:
        if not hook.wants(name):
            continue
        delivery = WebhookDelivery(
            organization_id=organization_id,
            webhook_id=hook.id,
            event=name,
            payload=body,
            status=DeliveryStatus.PENDING,
            # Due immediately. The next drain picks it up; there is no delay
            # anybody would have to explain.
            next_attempt_at=moment,
        )
        session.add(delivery)
        queued.append(delivery)
    if queued:
        session.flush()
    return queued


# -- delivery --------------------------------------------------------------


def _encode(delivery: WebhookDelivery) -> bytes:
    """The exact bytes that are signed and sent.

    `sort_keys` and a fixed separator because the subscriber verifies the HMAC
    over the body they received: if the encoding varies between attempts, a
    retry has a different signature over what the subscriber sees as the same
    event, and their replay guard fires on a legitimate resend.
    """
    return json.dumps(delivery.payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def headers_for(
    delivery: WebhookDelivery, secret: str, *, now: int | None = None
) -> dict[str, str]:
    """Called after `attempts` has been incremented for the attempt in hand."""
    body = _encode(delivery)
    return {
        "Content-Type": "application/json",
        "User-Agent": "massingplan-webhooks/1",
        SIGNATURE_HEADER: sign_with_timestamp(body, secret, now=now),
        # So a subscriber can make delivery idempotent without parsing the body.
        # Retries reuse the id, which is the point: it identifies the delivery,
        # not the attempt.
        "X-Massing-Delivery": delivery.id,
        "X-Massing-Event": delivery.event,
        "X-Massing-Attempt": str(delivery.attempts),
    }


def deliver(
    session: Session,
    delivery: WebhookDelivery,
    *,
    transport: Transport | None = None,
    now: datetime | None = None,
    require_tls: bool = True,
) -> DeliveryStatus:
    """One attempt. Records the outcome and schedules the next, or gives up."""
    moment = now or datetime.now(tz=timezone.utc)
    hook = delivery.webhook
    send = transport or HttpTransport()

    delivery.attempts += 1
    try:
        # Re-vetted on every attempt, not just at subscribe time. DNS changes,
        # and a hostname that pointed at a public address last week can point at
        # 10.0.0.5 today without anybody editing the subscription.
        target = webhook_url.vet(hook.url, require_tls=require_tls)
        response = send(target, _encode(delivery), headers_for(delivery, secret_of(hook)))
    except webhook_url.UrlRejectedError as exc:
        # Terminal, and the endpoint goes off. A URL that now resolves somewhere
        # forbidden will not become allowed by waiting, and continuing to try is
        # the server knocking on an internal door once an hour.
        _fail(session, delivery, f"url rejected: {exc}", terminal=True)
        deactivate(session, hook, f"url rejected: {exc}")
        return DeliveryStatus.FAILED
    except Exception as exc:  # noqa: BLE001 - any transport failure is a retry
        return _retry_or_fail(session, delivery, f"{type(exc).__name__}: {exc}", moment)

    delivery.response_status = response.status
    delivery.response_excerpt = (response.body or "")[:RESPONSE_EXCERPT_CHARS]

    if 200 <= response.status < 300:
        delivery.status = DeliveryStatus.DELIVERED
        delivery.delivered_at = moment
        delivery.next_attempt_at = None
        delivery.error = ""
        hook.consecutive_failures = 0
        hook.last_success_at = moment
        session.flush()
        return DeliveryStatus.DELIVERED

    if response.status == GONE_STATUS:
        _fail(session, delivery, "subscriber returned 410 Gone", terminal=True)
        deactivate(session, hook, "the endpoint returned 410 Gone")
        return DeliveryStatus.FAILED

    if response.status in TERMINAL_STATUSES:
        _fail(session, delivery, f"HTTP {response.status}, not retryable", terminal=True)
        _count_failure(session, hook)
        return DeliveryStatus.FAILED

    return _retry_or_fail(session, delivery, f"HTTP {response.status}", moment)


def _retry_or_fail(
    session: Session, delivery: WebhookDelivery, reason: str, moment: datetime
) -> DeliveryStatus:
    if delivery.attempts >= MAX_ATTEMPTS:
        _fail(session, delivery, f"{reason} (gave up after {delivery.attempts} attempts)")
        _count_failure(session, delivery.webhook)
        return DeliveryStatus.FAILED
    wait = BACKOFF_SECONDS[delivery.attempts - 1]
    delivery.status = DeliveryStatus.PENDING
    delivery.error = reason[:300]
    delivery.next_attempt_at = moment + timedelta(seconds=wait)
    session.flush()
    return DeliveryStatus.PENDING


def _fail(
    session: Session, delivery: WebhookDelivery, reason: str, *, terminal: bool = False
) -> None:
    delivery.status = DeliveryStatus.FAILED
    delivery.error = reason[:300]
    # Cleared so the row cannot be picked up again. A FAILED row with a due time
    # is the kind of contradiction that becomes an infinite retry after one
    # careless change to the drain query.
    delivery.next_attempt_at = None
    session.flush()
    del terminal


def _count_failure(session: Session, hook: Webhook) -> None:
    hook.consecutive_failures += 1
    if hook.consecutive_failures >= AUTO_DISABLE_AFTER:
        deactivate(
            session,
            hook,
            f"{hook.consecutive_failures} deliveries failed in a row",
        )
    session.flush()


def due(
    session: Session, *, now: datetime | None = None, limit: int = 100
) -> list[WebhookDelivery]:
    moment = now or datetime.now(tz=timezone.utc)
    return list(
        session.scalars(
            select(WebhookDelivery)
            .where(WebhookDelivery.status == DeliveryStatus.PENDING)
            .where(WebhookDelivery.next_attempt_at <= moment)
            .order_by(WebhookDelivery.next_attempt_at)
            .limit(limit)
        ).all()
    )


def drain(
    session: Session,
    *,
    transport: Transport | None = None,
    now: datetime | None = None,
    limit: int = 100,
    require_tls: bool = True,
) -> dict[str, int]:
    """Attempt every delivery that is due. Returns a count per outcome."""
    counts = {"attempted": 0, "delivered": 0, "retrying": 0, "failed": 0}
    for delivery in due(session, now=now, limit=limit):
        outcome = deliver(session, delivery, transport=transport, now=now, require_tls=require_tls)
        counts["attempted"] += 1
        counts[
            {
                DeliveryStatus.DELIVERED: "delivered",
                DeliveryStatus.PENDING: "retrying",
                DeliveryStatus.FAILED: "failed",
            }[outcome]
        ] += 1
    if counts["attempted"]:
        logger.info("webhook drain", extra={"context": counts})
    return counts


# -- the real transport ----------------------------------------------------


class HttpTransport:
    """A single POST, to the address that was vetted, with no redirects.

    Connects to `target.address` rather than re-resolving `target.host`, so the
    fetch goes to the address the checks actually ran against. TLS still
    validates against the hostname, which is why the connection carries it.
    """

    def __init__(self, timeout: int = TIMEOUT_SECONDS) -> None:
        self.timeout = timeout

    def __call__(
        self, target: webhook_url.Target, body: bytes, headers: dict[str, str]
    ) -> Response:
        import http.client
        import socket
        import ssl
        from urllib.parse import urlsplit

        path = urlsplit(target.url).path or "/"
        query = urlsplit(target.url).query
        if query:
            path = f"{path}?{query}"

        if target.is_tls:
            connection: http.client.HTTPConnection = http.client.HTTPSConnection(
                target.host, target.port, timeout=self.timeout, context=ssl.create_default_context()
            )
        else:
            connection = http.client.HTTPConnection(target.host, target.port, timeout=self.timeout)

        # Pin the socket to the address that was vetted, while leaving
        # `connection.host` as the name -- which is what SNI and certificate
        # validation use. Without this the stack resolves again here and can get
        # a different, unchecked answer: the DNS-rebinding window.
        #
        # `_create_connection` is an instance attribute `http.client` assigns in
        # `__init__`, not a method, which is what makes this replaceable. It is
        # private, so its absence is treated as a hard failure rather than
        # quietly falling back to an unpinned connect -- a silent fallback here
        # is the check turning itself off on a Python upgrade.
        pinned = target.address
        if not hasattr(connection, "_create_connection"):  # pragma: no cover
            raise RuntimeError(
                "http.client no longer exposes _create_connection, so the "
                "webhook connection cannot be pinned to the vetted address. "
                "Refusing to deliver rather than resolving again unchecked."
            )

        def connect_to_pinned(
            address: tuple[str, int], timeout: Any = None, source: Any = None
        ) -> socket.socket:
            return socket.create_connection((pinned, address[1]), timeout, source)

        setattr(connection, "_create_connection", connect_to_pinned)  # noqa: B010

        try:
            connection.request("POST", path, body=body, headers=headers)
            raw = connection.getresponse()
            # Read a bounded amount. A subscriber returning a gigabyte is a
            # denial of service against the drain, not a useful error message.
            excerpt = raw.read(RESPONSE_EXCERPT_CHARS * 2).decode("utf-8", errors="replace")
            return Response(status=raw.status, body=excerpt)
        finally:
            connection.close()
