"""Pinning a fetch to the address that was validated.

The interesting failure is a *time of check to time of use* one: the guard
resolves a name and approves the answer, then the HTTP client resolves it
again to connect. A TTL of one second is enough for the second answer to be
``127.0.0.1``. These tests make the second answer hostile and assert on which
socket was actually opened.
"""

from __future__ import annotations

import http.server
import ipaddress
import socket
import threading

import httpx
import pytest

from jaigent import net
from jaigent.errors import ToolError
from jaigent.tools import web


def blocked(candidate: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str:
    """The same rule the real guard uses, in one line."""
    if candidate.is_loopback or candidate.is_private or candidate.is_link_local:
        return "a private network address"
    return ""


class _Service:
    """A tiny HTTP server that counts the requests it receives."""

    def __init__(self, body: bytes = b"ok", content_type: str = "text/plain") -> None:
        self.hits = 0
        self.body = body
        self.content_type = content_type
        service = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *args: object) -> None:
                pass

            def do_GET(self) -> None:  # noqa: N802
                service.hits += 1
                payload = service.body
                self.send_response(200)
                self.send_header("content-type", service.content_type)
                self.send_header("content-length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self) -> None:
        self.server.shutdown()


@pytest.fixture
def service():
    made = _Service()
    try:
        yield made
    finally:
        made.close()


def real_getaddrinfo(host, port, *args, **kwargs):  # noqa: ANN001, ANN202
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", port or 80))]


class TestResolvePublicAddress:
    def test_a_public_name_is_resolved_to_a_literal(self, monkeypatch) -> None:  # noqa: ANN001
        monkeypatch.setattr(
            net.socket,
            "getaddrinfo",
            lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))],
        )

        target = net.resolve_public_address(httpx.URL("https://example.com/page"), lambda ip: "")

        assert target.address == "93.184.216.34"
        assert target.host == "example.com"
        assert target.host_header == "example.com"
        assert target.sni == "example.com"

    def test_a_private_answer_is_refused(self, monkeypatch) -> None:  # noqa: ANN001
        monkeypatch.setattr(
            net.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("127.0.0.1", 0))]
        )

        with pytest.raises(ToolError, match="loopback|private"):
            net.resolve_public_address(httpx.URL("http://evil.example/"), blocked)

    def test_a_split_answer_is_refused(self, monkeypatch) -> None:  # noqa: ANN001
        """One name, one public and one private address: not a mistake."""
        monkeypatch.setattr(
            net.socket,
            "getaddrinfo",
            lambda *a, **k: [
                (2, 1, 6, "", ("93.184.216.34", 0)),
                (2, 1, 6, "", ("127.0.0.1", 0)),
            ],
        )

        with pytest.raises(ToolError, match="both"):
            net.resolve_public_address(httpx.URL("http://rebind.example/"), blocked)

    def test_a_literal_keeps_no_sni(self) -> None:
        target = net.resolve_public_address(httpx.URL("http://93.184.216.34/"), lambda ip: "")

        assert target.address == "93.184.216.34"
        assert target.sni == ""

    def test_a_non_default_port_is_in_the_host_header(self, monkeypatch) -> None:  # noqa: ANN001
        monkeypatch.setattr(
            net.socket, "getaddrinfo", lambda *a, **k: [(2, 1, 6, "", ("93.184.216.34", 0))]
        )

        target = net.resolve_public_address(httpx.URL("https://example.com:8443/x"), lambda ip: "")

        assert target.host_header == "example.com:8443"
        assert target.port == 8443


class TestPinnedTransport:
    def test_the_connection_goes_to_the_validated_address(self, service, monkeypatch) -> None:  # noqa: ANN001
        """The whole point: DNS may answer anything, the socket is decided already.

        Python resolves even a numeric literal through ``getaddrinfo``, so the
        assertion is about the *name*: if it is consulted again, the hostile
        answer below would win and the secret service would see the request.
        """
        secret = _Service(body=b"AWS_SECRET_ACCESS_KEY=leaked")
        try:
            queries: list[str] = []

            def hostile(host, port=None, *args, **kwargs):  # noqa: ANN001, ANN202
                queries.append(str(host))
                if str(host).startswith("rebind.example.com"):
                    # The second answer a rebinding record would give.
                    return [(2, 1, 6, "", ("127.0.0.1", secret.port))]
                return [(2, 1, 6, "", ("127.0.0.1", port or service.port))]

            monkeypatch.setattr(net.socket, "getaddrinfo", hostile)

            target = net.ResolvedTarget(
                url=httpx.URL(f"http://rebind.example.com:{service.port}/"),
                address="127.0.0.1",
                host="rebind.example.com",
                port=service.port,
                host_header=f"rebind.example.com:{service.port}",
                sni="",
            )
            with net.pinned_client(target, timeout=5) as client:
                response = client.get(str(target.url))

            assert response.status_code == 200
            assert service.hits == 1, "the validated service must answer"
            assert secret.hits == 0, "the rebound address must never be reached"
            assert not [q for q in queries if q.startswith("rebind.example.com")]
        finally:
            secret.close()

    def test_the_host_header_names_the_real_site(self, service) -> None:  # noqa: ANN001
        """Virtual hosts: the wire must still say example.com, not 127.0.0.1."""
        seen: list[str] = []
        service.server.RequestHandlerClass.do_GET = (
            lambda self: _echo_host(self, seen)  # type: ignore[method-assign]
        )
        target = net.ResolvedTarget(
            url=httpx.URL(f"http://example.com:{service.port}/"),
            address="127.0.0.1",
            host="example.com",
            port=service.port,
            host_header=f"example.com:{service.port}",
            sni="",
        )

        with net.pinned_client(target, timeout=5) as client:
            client.get(str(target.url))

        assert seen == [f"example.com:{service.port}"]

    def test_a_redirect_to_a_pinned_hop_is_refetched_with_a_new_pin(self, monkeypatch) -> None:  # noqa: ANN001
        """Each hop must re-resolve and re-pin; reusing a transport skips the check."""
        first = _Service(body=b"", content_type="text/html")
        second = _Service(body=b"second hop", content_type="text/plain")
        try:
            original = first.server.RequestHandlerClass.do_GET

            def redirect(self) -> None:  # noqa: ANN001
                self.send_response(302)
                self.send_header("location", f"http://second.example:{second.port}/")
                self.send_header("content-length", "0")
                self.end_headers()

            first.server.RequestHandlerClass.do_GET = redirect  # type: ignore[method-assign]

            def resolve(url, is_blocked):  # noqa: ANN001, ANN202
                host = url.host
                port = first.port if host.startswith("first") else second.port
                return net.ResolvedTarget(
                    url=url,
                    address="127.0.0.1",
                    host=host,
                    port=port,
                    host_header=f"{host}:{port}",
                    sni="",
                )

            monkeypatch.setattr(web, "resolve_public_address", resolve)
            monkeypatch.setattr(web, "check_public_url", lambda url: None)

            out = web.fetch_page(f"http://first.example:{first.port}/")

            assert "second hop" in out
            assert second.hits == 1
            first.server.RequestHandlerClass.do_GET = original  # type: ignore[method-assign]
        finally:
            first.close()
            second.close()


def _echo_host(handler: http.server.BaseHTTPRequestHandler, seen: list[str]) -> None:
    seen.append(handler.headers.get("host", ""))
    handler.send_response(200)
    handler.send_header("content-length", "0")
    handler.end_headers()


class TestFetchPageUsesIt:
    def test_fetch_page_pins_before_it_connects(self, monkeypatch) -> None:  # noqa: ANN001
        calls: list[str] = []

        def resolve(url, is_blocked):  # noqa: ANN001, ANN202
            calls.append(url.host)
            raise ToolError("stop here: the pin was consulted")

        monkeypatch.setattr(web, "resolve_public_address", resolve)
        monkeypatch.setattr(web, "check_public_url", lambda url: None)

        with pytest.raises(ToolError, match="stop here"):
            web.fetch_page("http://example.com/")

        assert calls == ["example.com"]
