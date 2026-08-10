"""Deciding whether the server is allowed to fetch a URL a user typed.

This is the whole security question in outbound webhooks, and it is worth its
own module. A webhook URL is a request **the server makes**, from inside your
network, with whatever the network trusts. `http://169.254.169.254/latest/
meta-data/iam/security-credentials/` is a cloud provider's credential endpoint;
`http://localhost:9200/_search` is your Elasticsearch. Both are one careless
subscribe away if nothing checks the address.

Four rules, each earning its place:

**Scheme allow-list.** `http` and `https` only. `file:///etc/passwd` and
`gopher://` are requests too, and a blocklist misses the scheme nobody thought
of.

**The address is checked, not the hostname.** Blocking the string "localhost"
stops nothing: `127.0.0.1`, `0x7f.1`, `[::1]`, `2130706433` and a hostname whose
A record is `10.0.0.1` all reach the same place. So the name is resolved and
every returned address is judged.

**Every resolved address must pass, not just the first.** A name resolving to
both a public address and `10.0.0.5` would otherwise depend on resolver
ordering, which is not a security boundary.

**Redirects are not followed.** A vetted public URL that 302s to
`169.254.169.254` defeats every check above, because the check ran on the URL
the user typed and the fetch went somewhere else.

**The residual risk, stated:** between the check and the connection, the name
can be re-resolved to a different address — DNS rebinding. `deliver()` narrows
that by connecting to the address that was vetted rather than re-resolving, and
the window that remains is documented in SECURITY.md rather than papered over.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit

#: `(host, port) -> [(family, address), ...]`. Injectable so tests exercise the
#: judgement without depending on what DNS happens to answer today.
Resolver = Callable[[str, int], "list[tuple[int, str]]"]

ALLOWED_SCHEMES = frozenset({"http", "https"})

#: Ports the server will connect to. Not a security boundary on its own -- an
#: attacker who controls a public host can listen anywhere -- but it stops the
#: obvious internal-service sweep (5432, 6379, 9200, 11211) with one rule.
ALLOWED_PORTS = frozenset({80, 443, 8000, 8080, 8443})

MAX_URL_LENGTH = 2000

#: Ranges named here rather than left to `ipaddress.is_private`, because what
#: that property covers has *changed between Python versions* -- 100.64.0.0/10
#: moved in and out of it. A security check that means different things on 3.11
#: and 3.13 is not a check. These are stated so they hold on both.
EXTRA_FORBIDDEN = (
    ipaddress.ip_network("100.64.0.0/10"),  # carrier-grade NAT / shared space
    ipaddress.ip_network("192.0.0.0/24"),  # IETF protocol assignments
    ipaddress.ip_network("198.18.0.0/15"),  # benchmarking
    ipaddress.ip_network("240.0.0.0/4"),  # reserved for future use
    ipaddress.ip_network("64:ff9b::/96"),  # NAT64, which can carry v4 loopback
    ipaddress.ip_network("2002::/16"),  # 6to4
    ipaddress.ip_network("100::/64"),  # discard-only
)


class UrlRejectedError(ValueError):
    """The URL is not one the server will fetch. The message says why."""


@dataclass(frozen=True)
class Target:
    """A vetted URL, with the address it was vetted against."""

    url: str
    scheme: str
    host: str
    port: int
    address: str
    family: int

    @property
    def is_tls(self) -> bool:
        return self.scheme == "https"


def _address_is_forbidden(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str:
    """Empty string when the address is fine, otherwise the reason it is not."""
    if ip.is_loopback:
        return "loopback addresses are the server itself"
    if ip.is_link_local:
        # 169.254.169.254 is the cloud metadata service on AWS, GCP, Azure and
        # DigitalOcean alike. This single rule is most of the point of the
        # module.
        return "link-local addresses reach cloud metadata services"
    if ip.is_private:
        return "private addresses are inside your network"
    if ip.is_reserved or ip.is_multicast or ip.is_unspecified:
        return "reserved, multicast and unspecified addresses are not endpoints"
    for network in EXTRA_FORBIDDEN:
        if ip.version == network.version and ip in network:
            return f"{network} is not routable to a subscriber"
    if isinstance(ip, ipaddress.IPv6Address):
        # ::ffff:127.0.0.1 and 64:ff9b::7f00:1 are loopback wearing a hat.
        mapped = ip.ipv4_mapped or (
            ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF) if ip.sixtofour else None
        )
        if mapped is not None:
            return _address_is_forbidden(mapped)
    return ""


def resolve(host: str, port: int) -> list[tuple[int, str]]:
    """Every address the name resolves to. Separated out so tests can replace it."""
    try:
        infos = socket.getaddrinfo(host, port, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise UrlRejectedError(f"{host} does not resolve ({exc.strerror or exc})") from exc
    return [(int(info[0]), str(info[4][0])) for info in infos]


def vet(
    raw: str,
    *,
    require_tls: bool = True,
    resolver: Resolver | None = None,
) -> Target:
    """Judge a URL, returning the address the caller should connect to.

    `require_tls` is on by default and relaxed only for a development install:
    an unencrypted webhook publishes every event, and its signature, to anyone
    on the path.
    """
    if not raw or len(raw) > MAX_URL_LENGTH:
        raise UrlRejectedError(f"the URL must be 1 to {MAX_URL_LENGTH} characters")

    parts = urlsplit(raw.strip())
    if parts.scheme not in ALLOWED_SCHEMES:
        raise UrlRejectedError(
            f"{parts.scheme or 'that'} is not a scheme this server will fetch; use http or https"
        )
    if require_tls and parts.scheme != "https":
        raise UrlRejectedError(
            "https is required. An http webhook publishes every event, and its "
            "signature, to anyone on the path."
        )
    if parts.username or parts.password:
        # `https://evil@internal/` is read as host `internal` by some parsers
        # and `evil` by others. Refusing is cheaper than picking a side.
        raise UrlRejectedError("credentials in the URL are not accepted")
    if parts.fragment:
        raise UrlRejectedError(
            "a fragment is not sent to the server, so it cannot mean anything here"
        )

    host = parts.hostname
    if not host:
        raise UrlRejectedError("the URL has no host")

    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as exc:
        raise UrlRejectedError("that is not a valid port") from exc
    if port not in ALLOWED_PORTS:
        raise UrlRejectedError(
            f"port {port} is not one this server will connect to "
            f"({', '.join(str(p) for p in sorted(ALLOWED_PORTS))})"
        )

    addresses = (resolver or resolve)(host, port)
    if not addresses:
        raise UrlRejectedError(f"{host} does not resolve to any address")

    # Every address, not just the one we would use. A name resolving to both a
    # public address and 10.0.0.5 would otherwise depend on resolver ordering.
    for family, address in addresses:
        reason = _address_is_forbidden(ipaddress.ip_address(address))
        if reason:
            raise UrlRejectedError(f"{host} resolves to {address}, and {reason}")
        del family

    family, address = addresses[0]
    return Target(
        url=raw.strip(), scheme=parts.scheme, host=host, port=port, address=address, family=family
    )
