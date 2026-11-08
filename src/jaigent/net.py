"""Pinning a fetch to the address we validated.

The SSRF guard in :mod:`jaigent.tools.web` resolves a hostname, checks every
address it maps to, and then hands the *hostname* to ``httpx`` — which resolves
it again, later, to open the connection. That gap is the whole of DNS
rebinding: an attacker-controlled domain answers with a public address for the
check and ``127.0.0.1`` (or ``169.254.169.254``) a moment later, and the fetch
lands on the machine itself. Low time-to-live records make it reliable, and the
agent then reads a local service or cloud credentials.

Resolving is therefore not enough; the connection has to go to a specific
address. This transport does that:

* :func:`resolve_public_address` resolves once and returns the first address
  that passes the guard, so validation and connection use the same answer.
* :class:`PinnedTransport` builds the connection pool for that literal address
  while keeping urllib3-style semantics for the parts that must still be right:
  the ``Host`` header, the TLS SNI name, certificate verification against the
  real hostname, and the bytes on the wire.

It is a thin subclass of ``httpx.HTTPTransport``, so proxies, timeouts, limits
and streaming behave exactly as they did; only the address is decided here.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

import httpcore
import httpx

from jaigent.errors import ToolError


@dataclass(slots=True)
class ResolvedTarget:
    """A validated address and everything needed to talk to it by name."""

    url: httpx.URL
    address: str
    host: str
    port: int
    #: The value for the ``Host`` header, port included only when non-default.
    host_header: str
    sni: str = ""
    #: Extra addresses the name resolved to, for the error message.
    checked: list[str] = field(default_factory=list)


def _default_port(scheme: str) -> int:
    return 443 if scheme == "https" else 80


def resolve_public_address(url: httpx.URL, is_blocked: Any) -> ResolvedTarget:
    """Resolve ``url``'s host once and return a validated literal address.

    Args:
        url: The request target. Its scheme, port and path are preserved.
        is_blocked: ``callable(ip) -> str`` returning the reason an address is
            not fetchable, or ``""`` when it is. Injected so this module and
            the guard in ``web.py`` cannot drift apart.

    Raises:
        ToolError: if nothing fetchable comes back, or if the name resolves to
            both a public and a blocked address (a deliberate split-horizon
            record; refusing is the only safe reading).
    """
    host = url.host
    if not host:
        raise ToolError(f"{str(url)!r} has no host to fetch.")
    port = url.port or _default_port(url.scheme)

    # A literal needs no lookup, but still goes through the same guard.
    try:
        literal = ipaddress.ip_address(host)
    except ValueError:
        literal = None

    addresses: list[str] = []
    if literal is not None:
        addresses = [str(literal)]
    else:
        try:
            infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise ToolError(f"Could not resolve {host}: {exc}") from exc
        for info in infos:
            address = str(info[4][0])
            if address not in addresses:
                addresses.append(address)

    if not addresses:
        raise ToolError(f"Could not resolve {host} to any address.")

    blocked: list[tuple[str, str]] = []
    usable: list[str] = []
    for address in addresses:
        try:
            candidate = ipaddress.ip_address(address)
        except ValueError:  # pragma: no cover - getaddrinfo returned nonsense
            continue
        reason = is_blocked(candidate)
        if reason:
            blocked.append((address, reason))
        else:
            usable.append(address)

    if blocked and not usable:
        address, reason = blocked[0]
        raise ToolError(
            f"Refusing to fetch {url}: {host} resolves to {address}, which is {reason}."
        )
    if blocked and usable:
        # Same name, public and private answers: that is the shape of a
        # rebinding record, not a mistake we should work around.
        address, reason = blocked[0]
        raise ToolError(
            f"Refusing to fetch {url}: {host} resolves to both {usable[0]} and "
            f"{address} ({reason}). A name that points at two different networks "
            "cannot be fetched safely."
        )

    address = usable[0]
    return ResolvedTarget(
        url=url,
        address=address,
        host=host,
        port=port,
        host_header=host if port == _default_port(url.scheme) else f"{host}:{port}",
        sni=host if literal is None else "",
        checked=addresses,
    )


class PinnedTransport(httpx.HTTPTransport):
    """Connect to :attr:`ResolvedTarget.address`, not to a fresh DNS answer.

    ``handle_request`` builds the ``httpcore`` request against the validated
    literal address and restores the real name in the ``Host`` header and the
    TLS SNI. That is what keeps the certificate meaningful: the handshake still
    say ``example.com`` while the socket goes to the address that was actually
    inspected.
    """

    def __init__(self, target: ResolvedTarget, **kwargs: Any) -> None:
        self._target = target
        super().__init__(**kwargs)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        target = self._target
        raw_headers = [
            (name, value) for name, value in request.headers.raw if name.lower() != b"host"
        ]
        raw_headers.insert(0, (b"host", target.host_header.encode()))

        extensions = dict(request.extensions)
        if target.sni:
            # Without this the pool takes the SNI from the pinned literal and
            # every certificate fails to match.
            extensions["sni_hostname"] = target.sni.encode()

        pinned = httpcore.Request(
            method=request.method,
            url=httpcore.URL(
                scheme=request.url.raw_scheme,
                host=target.address.encode(),
                port=target.port,
                target=request.url.raw_path,
            ),
            headers=raw_headers,
            content=request.stream,
            extensions=extensions,
        )
        with httpx._transports.default.map_httpcore_exceptions():
            response = self._pool.handle_request(pinned)
        assert isinstance(response.stream, Iterable)
        return httpx.Response(
            status_code=response.status,
            headers=response.headers,
            stream=httpx._transports.default.ResponseStream(response.stream),
            extensions=response.extensions,
        )


def pinned_client(target: ResolvedTarget, **kwargs: Any) -> httpx.Client:
    """An ``httpx.Client`` whose connections can only go to ``target``."""
    return httpx.Client(transport=PinnedTransport(target, verify=True), **kwargs)
