"""Web tools, exercised against mocked HTTP so the suite stays offline."""

from __future__ import annotations

import httpx
import pytest

from jaigent.errors import ToolError
from jaigent.tools import web
from jaigent.tools.web import (
    SearchResult,
    _clean_ddg_url,
    build_web_tools,
    fetch_page,
    search_duckduckgo,
    strip_html,
    web_search,
)

DDG_HTML = """
<div class="result">
  <a class="result__a" href="https://python.org/downloads">Python <b>Downloads</b></a>
  <a class="result__snippet">The official home of Python.</a>
</div>
<div class="result">
  <a class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fdocs.python.org%2F3%2F">Docs</a>
  <a class="result__snippet">Official documentation.</a>
</div>
"""


class TestStripHtml:
    def test_removes_tags_and_unescapes(self) -> None:
        assert strip_html("<p>Hello &amp; <b>world</b></p>") == "Hello & world"

    def test_drops_scripts_and_styles(self) -> None:
        out = strip_html("<style>.a{color:red}</style><script>evil()</script><p>keep</p>")
        assert "evil" not in out
        assert "color" not in out
        assert "keep" in out

    def test_collapses_blank_lines(self) -> None:
        assert "\n\n\n" not in strip_html("<p>a</p><br><br><br><br><p>b</p>")


class TestCleanDdgUrl:
    def test_unwraps_redirect(self) -> None:
        wrapped = "//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Fx"
        assert _clean_ddg_url(wrapped) == "https://example.com/x"

    def test_passes_direct_urls_through(self) -> None:
        assert _clean_ddg_url("https://example.com") == "https://example.com"


def _mock_transport(handler) -> httpx.MockTransport:  # noqa: ANN001
    return httpx.MockTransport(handler)


class TestSearchDuckDuckGo:
    def test_parses_results(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            assert request.url.host == "html.duckduckgo.com"
            return httpx.Response(200, text=DDG_HTML)

        _patch_client(monkeypatch, handler)
        results = search_duckduckgo("python", max_results=5)

        assert len(results) == 2
        assert results[0].title == "Python Downloads"
        assert results[0].url == "https://python.org/downloads"
        assert results[1].url == "https://docs.python.org/3/"

    def test_respects_max_results(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_client(monkeypatch, lambda r: httpx.Response(200, text=DDG_HTML))
        assert len(search_duckduckgo("python", max_results=1)) == 1

    def test_network_error_becomes_tool_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("offline")

        _patch_client(monkeypatch, handler)
        with pytest.raises(ToolError, match="DuckDuckGo search failed"):
            search_duckduckgo("python")


class TestWebSearch:
    def test_renders_numbered_results(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_client(monkeypatch, lambda r: httpx.Response(200, text=DDG_HTML))
        out = web_search("python", 5)
        assert "1. Python Downloads" in out
        assert "https://python.org/downloads" in out

    def test_empty_query_rejected(self) -> None:
        with pytest.raises(ToolError, match="must not be empty"):
            web_search("   ")

    def test_unknown_backend(self) -> None:
        with pytest.raises(ToolError, match="Unknown search backend"):
            web_search("x", backend="bing")

    def test_tavily_requires_key(self) -> None:
        with pytest.raises(ToolError, match="TAVILY_API_KEY"):
            web_search("x", backend="tavily", api_key=None)

    def test_no_results_message(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_client(monkeypatch, lambda r: httpx.Response(200, text="<html></html>"))
        assert "No results" in web_search("obscure")


class TestFetchPage:
    def test_returns_stripped_html(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(
                200,
                text="<html><body><h1>Title</h1><p>Body</p></body></html>",
                headers={"content-type": "text/html"},
            )

        _patch_client(monkeypatch, handler)
        out = fetch_page("https://example.com")
        assert "Title" in out
        assert "<h1>" not in out

    def test_truncates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        long_text = "word " * 5000
        _patch_client(
            monkeypatch,
            lambda r: httpx.Response(200, text=long_text, headers={"content-type": "text/plain"}),
        )
        out = fetch_page("https://example.com", max_chars=500)
        assert "truncated" in out

    def test_rejects_non_http_scheme(self) -> None:
        with pytest.raises(ToolError, match="http"):
            fetch_page("file:///etc/passwd")

    def test_rejects_binary_content(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_client(
            monkeypatch,
            lambda r: httpx.Response(
                200, content=b"\x89PNG", headers={"content-type": "image/png"}
            ),
        )
        with pytest.raises(ToolError, match="not text"):
            fetch_page("https://example.com/a.png")

    def test_http_error_is_explained(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_client(monkeypatch, lambda r: httpx.Response(404, text="nope"))
        with pytest.raises(ToolError, match="HTTP 404"):
            fetch_page("https://example.com/missing")


def test_build_web_tools_names() -> None:
    assert {t.name for t in build_web_tools()} == {"web_search", "fetch_page"}


def test_search_result_render() -> None:
    rendered = SearchResult("T", "https://u", "S").render(1)
    assert rendered.startswith("1. T")
    assert "https://u" in rendered


def _patch_client(monkeypatch: pytest.MonkeyPatch, handler) -> None:  # noqa: ANN001
    """Force jaigent's HTTP client factory to use a mock transport."""
    import jaigent.tools.web as web_module

    def factory(timeout: float) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)

    monkeypatch.setattr(web_module, "_new_client", factory)


class TestSSRFGuard:
    """fetch_page must not reach the local machine or a private network.

    An injected page can tell the model to fetch an internal URL. The cloud
    metadata endpoint is the worst case: it hands out credentials to anything
    that asks it.
    """

    @pytest.mark.parametrize(
        ("url", "why"),
        [
            ("http://169.254.169.254/latest/meta-data/", "cloud metadata"),
            ("http://100.100.100.200/", "alibaba metadata"),
            ("http://localhost:8080/admin", "localhost by name"),
            ("http://127.0.0.1/", "loopback v4"),
            ("http://[::1]/", "loopback v6"),
            ("http://192.168.1.1/", "private class C"),
            ("http://10.0.0.5/", "private class A"),
            ("http://172.16.0.1/", "private class B"),
            ("http://0.0.0.0/", "unspecified"),
            ("http://169.254.1.1/", "link-local"),
        ],
    )
    def test_internal_targets_are_refused(self, url: str, why: str) -> None:
        with pytest.raises(ToolError, match="Refusing to fetch"):
            web.check_public_url(url)

    def test_a_public_url_is_allowed(self) -> None:
        web.check_public_url("https://example.com/page")

    def test_a_hostname_resolving_to_loopback_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """DNS rebinding: the name looks public, the address is not."""
        monkeypatch.setattr(
            web.socket,
            "getaddrinfo",
            lambda *a, **k: [(2, 1, 6, "", ("127.0.0.1", 80))],
        )

        with pytest.raises(ToolError, match="resolves to 127.0.0.1"):
            web.check_public_url("http://sneaky.example.com/")

    def test_a_url_without_a_host_is_refused(self) -> None:
        with pytest.raises(ToolError, match="no host"):
            web.check_public_url("http:///nohost")

    def test_a_trailing_dot_does_not_bypass_the_check(self) -> None:
        with pytest.raises(ToolError, match="Refusing to fetch"):
            web.check_public_url("http://localhost./")

    def test_case_does_not_bypass_the_check(self) -> None:
        with pytest.raises(ToolError, match="Refusing to fetch"):
            web.check_public_url("http://LOCALHOST/")

    def test_an_unresolvable_host_reports_clearly(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def boom(*args: object, **kwargs: object) -> None:
            raise web.socket.gaierror("Name or service not known")

        monkeypatch.setattr(web.socket, "getaddrinfo", boom)

        with pytest.raises(ToolError, match="Could not resolve"):
            web.check_public_url("http://nope.invalid/")

    def test_fetch_page_refuses_an_internal_url_before_any_request(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        called = False

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal called
            called = True
            return httpx.Response(200, text="secret")

        _patch_client(monkeypatch, handler)

        with pytest.raises(ToolError, match="Refusing to fetch"):
            web.fetch_page("http://169.254.169.254/latest/meta-data/")

        assert called is False

    def test_a_redirect_to_an_internal_address_is_refused(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A public URL that 302s inward must not slip through."""

        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.host == "example.com":
                return httpx.Response(
                    302, headers={"location": "http://169.254.169.254/latest/meta-data/"}
                )
            return httpx.Response(200, text="credentials")

        _patch_client(monkeypatch, handler)

        with pytest.raises(ToolError, match="Refusing to fetch"):
            web.fetch_page("https://example.com/redirect")

    def test_a_redirect_to_another_public_page_is_followed(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            if request.url.path == "/old":
                return httpx.Response(301, headers={"location": "https://example.com/new"})
            return httpx.Response(
                200, text="<html><body>arrived</body></html>", headers={"content-type": "text/html"}
            )

        _patch_client(monkeypatch, handler)

        assert "arrived" in web.fetch_page("https://example.com/old")

    def test_a_redirect_loop_terminates(self, monkeypatch: pytest.MonkeyPatch) -> None:
        def handler(request: httpx.Request) -> httpx.Response:
            return httpx.Response(302, headers={"location": "https://example.com/loop"})

        _patch_client(monkeypatch, handler)

        with pytest.raises(ToolError, match="[Tt]oo many redirects"):
            web.fetch_page("https://example.com/loop")
