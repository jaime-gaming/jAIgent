"""Web tools: search the internet and fetch pages as readable text.

Two search backends ship in the box:

``duckduckgo``
    Scrapes the DuckDuckGo HTML endpoint. No API key, no account, rate limited
    by DuckDuckGo. This is the default so that ``jaigent`` works with nothing
    but an LLM key.
``tavily``
    Uses the Tavily search API (``TAVILY_API_KEY``). Better quality, needs a key.
"""

from __future__ import annotations

import html
import re
from dataclasses import dataclass
from urllib.parse import parse_qs, unquote, urlparse

import httpx

from jaigent.errors import ToolError
from jaigent.tools.base import Tool

USER_AGENT = "Mozilla/5.0 (compatible; jaigent/0.1; +https://github.com/jaime-gaming/jaigent)"
DDG_ENDPOINT = "https://html.duckduckgo.com/html/"
TAVILY_ENDPOINT = "https://api.tavily.com/search"
MAX_PAGE_CHARS = 20_000

_TAG_RE = re.compile(r"<[^>]+>")
_SCRIPT_RE = re.compile(r"<(script|style|noscript|svg|iframe)[^>]*>.*?</\1>", re.S | re.I)
_WS_RE = re.compile(r"[ \t\r\f\v]+")
_BLANK_RE = re.compile(r"\n{3,}")
_RESULT_RE = re.compile(
    r'<a[^>]+class="[^"]*result__a[^"]*"[^>]+href="(?P<url>[^"]+)"[^>]*>(?P<title>.*?)</a>'
    r".*?(?:<a[^>]+class=\"[^\"]*result__snippet[^\"]*\"[^>]*>(?P<snippet>.*?)</a>)?",
    re.S | re.I,
)


@dataclass(slots=True, frozen=True)
class SearchResult:
    """One search hit."""

    title: str
    url: str
    snippet: str

    def render(self, index: int) -> str:
        snippet = f"\n   {self.snippet}" if self.snippet else ""
        return f"{index}. {self.title}\n   {self.url}{snippet}"


def strip_html(raw: str) -> str:
    """Turn an HTML document into plain-ish text."""
    text = _SCRIPT_RE.sub(" ", raw)
    text = re.sub(r"<br\s*/?>|</p>|</div>|</li>|</h[1-6]>", "\n", text, flags=re.I)
    text = _TAG_RE.sub(" ", text)
    text = html.unescape(text)
    text = _WS_RE.sub(" ", text)
    text = "\n".join(line.strip() for line in text.splitlines())
    return _BLANK_RE.sub("\n\n", text).strip()


def _clean_ddg_url(href: str) -> str:
    """DuckDuckGo wraps results in a redirect (``/l/?uddg=...``); unwrap it."""
    if "duckduckgo.com/l/" in href or href.startswith("//duckduckgo.com/l/"):
        query = parse_qs(urlparse(f"https:{href}" if href.startswith("//") else href).query)
        if "uddg" in query:
            return unquote(query["uddg"][0])
    return f"https:{href}" if href.startswith("//") else href


def _new_client(timeout: float) -> httpx.Client:
    return httpx.Client(
        timeout=timeout,
        follow_redirects=True,
        headers={"User-Agent": USER_AGENT, "Accept-Language": "en-US,en;q=0.9"},
    )


# ----------------------------------------------------------------------
# Backends
# ----------------------------------------------------------------------
def search_duckduckgo(
    query: str, max_results: int = 5, timeout: float = 30.0
) -> list[SearchResult]:
    try:
        with _new_client(timeout) as client:
            response = client.post(DDG_ENDPOINT, data={"q": query, "kl": "wt-wt"})
            response.raise_for_status()
    except httpx.HTTPError as exc:
        raise ToolError(f"DuckDuckGo search failed: {exc}") from exc

    results: list[SearchResult] = []
    for match in _RESULT_RE.finditer(response.text):
        url = _clean_ddg_url(html.unescape(match.group("url")))
        title = strip_html(match.group("title"))
        snippet = strip_html(match.group("snippet") or "")
        if not url.startswith("http"):
            continue
        results.append(SearchResult(title=title, url=url, snippet=snippet[:400]))
        if len(results) >= max_results:
            break
    return results


def search_tavily(
    query: str, api_key: str, max_results: int = 5, timeout: float = 30.0
) -> list[SearchResult]:
    payload = {
        "api_key": api_key,
        "query": query,
        "max_results": max_results,
        "search_depth": "basic",
    }
    try:
        with _new_client(timeout) as client:
            response = client.post(TAVILY_ENDPOINT, json=payload)
            response.raise_for_status()
            data = response.json()
    except httpx.HTTPStatusError as exc:
        raise ToolError(
            f"Tavily returned HTTP {exc.response.status_code}. Check TAVILY_API_KEY."
        ) from exc
    except httpx.HTTPError as exc:
        raise ToolError(f"Tavily search failed: {exc}") from exc

    return [
        SearchResult(
            title=item.get("title", "(untitled)"),
            url=item.get("url", ""),
            snippet=(item.get("content") or "")[:400],
        )
        for item in data.get("results", [])[:max_results]
    ]


# ----------------------------------------------------------------------
# Tool implementations
# ----------------------------------------------------------------------
def web_search(
    query: str,
    max_results: int = 5,
    *,
    backend: str = "duckduckgo",
    api_key: str | None = None,
    timeout: float = 30.0,
) -> str:
    query = (query or "").strip()
    if not query:
        raise ToolError("query must not be empty")
    max_results = max(1, min(int(max_results), 10))

    if backend == "tavily":
        if not api_key:
            raise ToolError(
                "The tavily backend needs TAVILY_API_KEY. Export it, or switch to "
                "JAIGENT_SEARCH_BACKEND=duckduckgo which needs no key."
            )
        results = search_tavily(query, api_key, max_results, timeout)
    elif backend == "duckduckgo":
        results = search_duckduckgo(query, max_results, timeout)
    else:
        raise ToolError(f"Unknown search backend {backend!r}; use 'duckduckgo' or 'tavily'")

    if not results:
        return (
            f"No results for {query!r}. Try different keywords, or fetch a known URL "
            "directly with fetch_page."
        )
    header = f"Search results for {query!r} (via {backend}):\n"
    return header + "\n\n".join(item.render(i) for i, item in enumerate(results, start=1))


def fetch_page(url: str, max_chars: int = MAX_PAGE_CHARS, timeout: float = 30.0) -> str:
    url = (url or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        raise ToolError(f"Only http(s) URLs can be fetched, got {url!r}")

    try:
        with _new_client(timeout) as client:
            response = client.get(url)
            response.raise_for_status()
    except httpx.HTTPStatusError as exc:
        raise ToolError(f"{url} returned HTTP {exc.response.status_code}") from exc
    except httpx.HTTPError as exc:
        raise ToolError(f"Could not fetch {url}: {exc}") from exc

    content_type = response.headers.get("content-type", "")
    if "html" in content_type:
        body = strip_html(response.text)
    elif content_type.startswith("text/") or "json" in content_type or "xml" in content_type:
        body = response.text
    else:
        raise ToolError(
            f"{url} is {content_type or 'an unknown type'}, not text. jaigent cannot read binary "
            "content; look for an HTML or text version."
        )

    max_chars = max(500, min(int(max_chars), 100_000))
    truncated = len(body) > max_chars
    if truncated:
        body = body[:max_chars] + f"\n\n... [truncated, {len(body) - max_chars} more characters]"
    return f"Content of {url}:\n\n{body}"


# ----------------------------------------------------------------------
# Tool descriptors
# ----------------------------------------------------------------------
def build_web_tools(
    *, backend: str = "duckduckgo", api_key: str | None = None, timeout: float = 30.0
) -> list[Tool]:
    """Create the web tools for the configured search ``backend``."""
    return [
        Tool(
            name="web_search",
            description=(
                "Search the web and get back titles, URLs and snippets. Use it for anything "
                "you are unsure about, anything recent, or anything outside your training "
                "data. Follow up with fetch_page to read a promising result in full."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "Search query. Keywords work better than full sentences.",
                    },
                    "max_results": {
                        "type": "integer",
                        "description": "How many results to return, 1-10. Defaults to 5.",
                    },
                },
                "required": ["query"],
            },
            func=lambda query, max_results=5: web_search(
                query, max_results, backend=backend, api_key=api_key, timeout=timeout
            ),
        ),
        Tool(
            name="fetch_page",
            description=(
                "Download a web page or text document and return its readable content as "
                "plain text. HTML is stripped of markup. Use it after web_search, or "
                "directly when the user gives you a URL."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Absolute http(s) URL."},
                    "max_chars": {
                        "type": "integer",
                        "description": (
                            "Truncate the page to this many characters. Defaults to 20000."
                        ),
                    },
                },
                "required": ["url"],
            },
            func=lambda url, max_chars=MAX_PAGE_CHARS: fetch_page(url, max_chars, timeout),
        ),
    ]
