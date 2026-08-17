"""Web tools, exercised against mocked HTTP so the suite stays offline."""

from __future__ import annotations

import httpx
import pytest

from jaigent.errors import ToolError
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
