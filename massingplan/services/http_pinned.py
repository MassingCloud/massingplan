"""One HTTP request, to the address that was vetted, with no redirects.

Extracted from `webhooks.HttpTransport` when the OIDC adapter needed the same
thing. That extraction is the whole point of the module existing: this is the
code that closes the DNS-rebinding window, and a second copy of it is a second
place for the pin to be forgotten -- which would not fail any test, because an
unpinned connection reaches exactly the same server on every non-adversarial
network.

Two callers, two directions:

* **webhooks** POST to a URL a *tenant* supplied, where the risk is reaching
  the metadata service or something on the internal network;
* **OIDC** GET and POST to URLs an *identity provider's discovery document*
  supplied, which is the same risk with a longer chain -- the operator vets the
  issuer, and the issuer then names the token and JWKS endpoints.

Both are "a URL that arrived from outside", and both get the same treatment.
"""

from __future__ import annotations

import http.client
import socket
import ssl
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from . import webhook_url

#: Bodies are read up to this many bytes. An endpoint returning a gigabyte is a
#: denial of service against the caller, not a useful error message.
MAX_RESPONSE_BYTES = 512 * 1024


@dataclass(frozen=True)
class PinnedResponse:
    status: int
    body: bytes

    def text(self, limit: int | None = None) -> str:
        raw = self.body if limit is None else self.body[:limit]
        return raw.decode("utf-8", errors="replace")


def request(
    target: webhook_url.Target,
    *,
    method: str = "GET",
    body: bytes | None = None,
    headers: dict[str, str] | None = None,
    timeout: int = 10,
    max_bytes: int = MAX_RESPONSE_BYTES,
) -> PinnedResponse:
    """Connect to `target.address`, ask for `target.url`'s path, read a bounded body.

    Redirects are not followed. A redirect is a fresh URL that nothing has
    vetted, and following it is how every one of the checks above gets skipped
    in a single hop.
    """
    parts = urlsplit(target.url)
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"

    if target.is_tls:
        connection: http.client.HTTPConnection = http.client.HTTPSConnection(
            target.host, target.port, timeout=timeout, context=ssl.create_default_context()
        )
    else:
        connection = http.client.HTTPConnection(target.host, target.port, timeout=timeout)

    # Pin the socket to the address that was vetted, while leaving
    # `connection.host` as the name -- which is what SNI and certificate
    # validation use. Without this the stack resolves again here and can get a
    # different, unchecked answer: the DNS-rebinding window.
    #
    # `_create_connection` is an instance attribute `http.client` assigns in
    # `__init__`, not a method, which is what makes this replaceable. It is
    # private, so its absence is treated as a hard failure rather than quietly
    # falling back to an unpinned connect -- a silent fallback here is the check
    # turning itself off on a Python upgrade.
    pinned = target.address
    if not hasattr(connection, "_create_connection"):  # pragma: no cover
        raise RuntimeError(
            "http.client no longer exposes _create_connection, so the request "
            "cannot be pinned to the vetted address. Refusing to connect rather "
            "than resolving again unchecked."
        )

    def connect_to_pinned(
        address: tuple[str, int], timeout_: Any = None, source: Any = None
    ) -> socket.socket:
        return socket.create_connection((pinned, address[1]), timeout_, source)

    setattr(connection, "_create_connection", connect_to_pinned)  # noqa: B010

    try:
        connection.request(method, path, body=body, headers=headers or {})
        raw = connection.getresponse()
        return PinnedResponse(status=raw.status, body=raw.read(max_bytes))
    finally:
        connection.close()
