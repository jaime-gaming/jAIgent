"""The local web panel: what it serves, what it refuses, and one real turn.

The panel is a browser front door to the same agent the terminal uses, so the
interesting questions are the ones about the door: who gets in, what they can
see, and whether a turn started here behaves like a turn started anywhere else.
Every test runs against a real socket on a port the operating system chose, so
nothing here depends on how the handler is wired internally.
"""

from __future__ import annotations

import http.client
import json
import threading
import time
from pathlib import Path
from typing import Any

import pytest

from jaigent import session as sessions
from jaigent import webui
from jaigent.agent import AgentResult
from jaigent.config import Settings
from jaigent.errors import ConfigurationError
from jaigent.pricing import Cost


# ---------------------------------------------------------------------------
# Harness
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def isolated_sessions(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Sessions land in a temporary directory, never in the real one."""
    directory = tmp_path / "sessions"
    monkeypatch.setenv("JAIGENT_SESSION_DIR", str(directory))
    return directory


def settings_for(tmp_path: Path, **overrides: Any) -> Settings:
    """A configuration that never reaches a provider."""
    workspace = tmp_path / "workspace"
    workspace.mkdir(exist_ok=True)
    return Settings(
        provider="openai",
        model="gpt-4o-mini",
        api_key="sk-test-not-a-real-key",
        workspace=workspace,
        approval=overrides.pop("approval", "auto"),
        **overrides,
    )


class ScriptedAgent:
    """An agent that does what it is told and reports what it did."""

    def __init__(self, script: list[tuple[str, Any]] | None = None, output: str = "All done."):
        self.script = script or [("tool", "read_file", "first line\nsecond line")]
        self.output = output
        self.history: list[dict[str, Any]] = []
        self.loaded: list[dict[str, Any]] | None = None
        self.prompts: list[str] = []
        self.on_text: Any = None
        self.on_tool_start: Any = None
        self.on_tool_call: Any = None

    def load_history(self, messages: list[dict[str, Any]]) -> None:
        self.loaded = list(messages)
        self.history = list(messages)

    def run(self, prompt: str) -> AgentResult:
        self.prompts.append(prompt)
        for item in self.script:
            if item[0] == "tool":
                _, name, output = item
                if self.on_tool_start:
                    self.on_tool_start(name, {"path": "notes.md"})
                if self.on_tool_call:
                    self.on_tool_call(name, {"path": "notes.md"}, output)
            elif item[0] == "text" and self.on_text:
                self.on_text(item[1])
        if self.on_text:
            for chunk in ("All ", "done."):
                self.on_text(chunk)
        self.history = [
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": self.output},
        ]
        return AgentResult(
            output=self.output,
            usage={"total_tokens": 42},
            cost=Cost(input_tokens=30, output_tokens=12, usd=0.0011),
            elapsed=0.25,
        )


class Panel:
    """A running panel and a client for it."""

    def __init__(self, tmp_path: Path, *, chat: bool = False, agent: Any = None, **config: Any):
        self.settings = settings_for(tmp_path, **config.pop("settings_overrides", {}))
        self.config = webui.WebConfig(port=0, chat=chat, **config)
        self.agent = agent if agent is not None else ScriptedAgent()
        self.server = webui.build_server(
            self.settings, self.config, agent_factory=lambda _settings: self.agent
        )
        self.port = int(self.server.server_address[1])
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)

    # -- requests ------------------------------------------------------
    def request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        token: str | None = "default",
        headers: dict[str, str] | None = None,
    ) -> tuple[int, dict[str, Any], bytes]:
        """One request. ``token=None`` sends no Authorization header at all."""
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        try:
            sent = dict(headers or {})
            if token == "default":
                sent["authorization"] = f"Bearer {self.config.token}"
            elif token:
                sent["authorization"] = f"Bearer {token}"
            raw = json.dumps(body).encode() if body is not None else None
            if raw is not None:
                sent["content-type"] = "application/json"
            connection.request(method, path, body=raw, headers=sent)
            response = connection.getresponse()
            payload = response.read()
            return response.status, dict(response.getheaders()), payload
        finally:
            connection.close()

    def get(self, path: str, **kwargs: Any) -> tuple[int, dict[str, Any], bytes]:
        return self.request("GET", path, **kwargs)

    def json_get(self, path: str, **kwargs: Any) -> dict[str, Any]:
        status, _headers, payload = self.get(path, **kwargs)
        assert status == 200, (status, payload[:200])
        return json.loads(payload)

    def post_chat(self, prompt: str, **kwargs: Any) -> tuple[int, list[tuple[str, Any]]]:
        """POST a turn and read the event stream back as ``(name, data)`` pairs."""
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=15)
        try:
            body = json.dumps({"prompt": prompt, **kwargs}).encode()
            connection.request(
                "POST",
                "/api/chat",
                body=body,
                headers={
                    "authorization": f"Bearer {self.config.token}",
                    "content-type": "application/json",
                    "accept": "text/event-stream",
                },
            )
            response = connection.getresponse()
            if response.status != 200:
                return response.status, [("error", json.loads(response.read() or b"{}"))]
            return response.status, read_events(response)
        finally:
            connection.close()


def read_events(response: http.client.HTTPResponse) -> list[tuple[str, Any]]:
    """Parse an ``text/event-stream`` body the way the panel's JavaScript does."""
    events: list[tuple[str, Any]] = []
    name = "message"
    data: list[str] = []
    for line in response.read().decode("utf-8").split("\n"):
        if line.startswith("event:"):
            name = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
        elif not line and data:
            events.append((name, json.loads("\n".join(data))))
            name, data = "message", []
    if data:
        events.append((name, json.loads("\n".join(data))))
    return events


def _quiet_console():  # noqa: ANN202
    """A console that writes nowhere, so a command's banner is not test output."""
    import io

    from rich.console import Console

    return Console(file=io.StringIO(), force_terminal=False, width=100, no_color=True)


def session_id(events: list[tuple[str, Any]]) -> str:
    """The id of the conversation a turn ran in."""
    return str(dict(events)["session"]["id"])


def strip_comments(source: str) -> str:
    """JavaScript with its comments removed, so a test can read the code.

    The panel's own prose mentions the things it refuses to do; asserting on the
    file as written would fail on a sentence that says "there is no innerHTML
    here".
    """
    import re

    without_blocks = re.sub(r"/\*.*?\*/", "", source, flags=re.DOTALL)
    return "\n".join(
        line for line in without_blocks.splitlines() if not line.strip().startswith("//")
    )


@pytest.fixture
def panel(tmp_path: Path):  # noqa: ANN201
    """A read-only panel on a free port."""
    live = Panel(tmp_path)
    try:
        yield live
    finally:
        live.close()


@pytest.fixture
def chatting(tmp_path: Path):  # noqa: ANN201
    """A panel allowed to run turns."""
    live = Panel(tmp_path, chat=True)
    try:
        yield live
    finally:
        live.close()


# ---------------------------------------------------------------------------
class TestWhereThePanelLives:
    """The files are one folder, and the server finds them however it was installed."""

    def test_the_shipped_panel_is_found(self) -> None:
        root = webui.asset_dir()

        assert root is not None, "web/public did not ship"
        assert (root / "index.html").is_file()
        assert (root / "app.js").is_file()

    def test_the_folder_holds_nothing_but_the_panel(self) -> None:
        root = webui.asset_dir()
        assert root is not None

        names = {path.name for path in root.rglob("*") if path.is_file()}

        # No build output, no lockfile, no credentials: this folder is what gets
        # deployed, so anything in it is public.
        assert not any(name.endswith((".map", ".env", ".key", ".pem")) for name in names)
        assert "node_modules" not in {path.parent.name for path in root.rglob("*")}

    def test_a_folder_without_an_index_is_not_the_panel(self, tmp_path: Path) -> None:
        empty = tmp_path / "nope"
        empty.mkdir()

        assert webui.resolve_asset(empty, "/") is None

    def test_a_url_names_a_file_inside_the_folder(self) -> None:
        root = webui.asset_dir()
        assert root is not None

        found = webui.resolve_asset(root, "/styles.css")

        assert found is not None
        assert found.name == "styles.css"

    def test_the_root_url_is_the_index(self) -> None:
        root = webui.asset_dir()
        assert root is not None

        assert webui.resolve_asset(root, "/") == root / "index.html"

    @pytest.mark.parametrize(
        "path",
        [
            "/../../../etc/passwd",
            "/..%2f..%2f..%2fetc%2fpasswd",
            "/%2e%2e/%2e%2e/pyproject.toml",
            "/./../../pyproject.toml",
            "/index.html\x00.png",
            "/C:\\Windows\\win.ini",
        ],
    )
    def test_nothing_outside_the_folder_is_reachable(self, path: str) -> None:
        root = webui.asset_dir()
        assert root is not None

        assert webui.resolve_asset(root, path) is None

    def test_a_symlink_planted_in_the_panel_does_not_escape(self, tmp_path: Path) -> None:
        root = tmp_path / "panel"
        root.mkdir()
        (root / "index.html").write_text("<html></html>", encoding="utf-8")
        secret = tmp_path / "secret.txt"
        secret.write_text("provider key", encoding="utf-8")
        (root / "leak.txt").symlink_to(secret)

        assert webui.resolve_asset(root, "/leak.txt") is None

    def test_a_directory_is_not_a_file(self) -> None:
        root = webui.asset_dir()
        assert root is not None
        (root / "nested").mkdir(exist_ok=True)
        try:
            assert webui.resolve_asset(root, "/nested") is None
        finally:
            (root / "nested").rmdir()

    def test_the_content_types_are_spelled_out(self, tmp_path: Path) -> None:
        # ``mimetypes`` reads the platform's table: a fresh Windows install does
        # not know .webmanifest, and a wrong type is a panel that does not start.
        assert webui.content_type_for(Path("app.js")) == "text/javascript; charset=utf-8"
        assert (
            webui.content_type_for(Path("m.webmanifest"))
            == "application/manifest+json; charset=utf-8"
        )
        assert webui.content_type_for(Path("i.svg")) == "image/svg+xml"
        assert webui.content_type_for(Path("unknown.bin")) == "application/octet-stream"


# ---------------------------------------------------------------------------
class TestTheConfiguration:
    def test_a_non_loopback_host_is_refused(self) -> None:
        with pytest.raises(ConfigurationError, match="loopback only"):
            webui.WebConfig(host="0.0.0.0")

    def test_a_lan_address_is_refused_too(self) -> None:
        with pytest.raises(ConfigurationError):
            webui.WebConfig(host="192.168.1.20")

    def test_a_token_is_generated_per_run(self) -> None:
        first = webui.WebConfig(port=0)
        second = webui.WebConfig(port=0)

        assert first.token and second.token
        assert first.token != second.token

    def test_the_address_carries_the_token_in_the_fragment(self) -> None:
        config = webui.WebConfig(port=8765, token="abc123")

        # A fragment, not a query string: the browser never sends it, so it
        # cannot reach a log line or a Referer header.
        assert config.url == "http://127.0.0.1:8765/#token=abc123"

    def test_chat_under_an_unanswerable_approval_policy_is_refused(self, tmp_path: Path) -> None:
        settings = settings_for(tmp_path, approval="ask")

        with pytest.raises(ConfigurationError, match="approval prompt"):
            webui.check_chat_allowed(settings)

    def test_chat_is_fine_when_nobody_will_be_asked(self, tmp_path: Path) -> None:
        webui.check_chat_allowed(settings_for(tmp_path, approval="auto"))
        webui.check_chat_allowed(settings_for(tmp_path, approval="dry-run"))

    def test_a_server_refuses_to_start_with_chat_under_ask(self, tmp_path: Path) -> None:
        settings = settings_for(tmp_path, approval="ask")

        with pytest.raises(ConfigurationError):
            webui.build_server(settings, webui.WebConfig(port=0, chat=True))


# ---------------------------------------------------------------------------
class TestThePolicyHeaders:
    def test_the_panel_may_not_load_remote_code_by_default(self) -> None:
        policy = webui.content_security_policy()

        assert policy.startswith("default-src 'none'")
        assert "gstatic" not in policy
        assert "firestore" not in policy

    def test_a_filled_in_firebase_config_relaxes_only_what_it_needs(self, tmp_path: Path) -> None:
        root = tmp_path / "panel"
        root.mkdir()
        (root / "index.html").write_text("<html></html>", encoding="utf-8")
        (root / "firebase-config.js").write_text(
            'export const FIREBASE_CONFIG = { apiKey: "AIza-real", projectId: "mine" };',
            encoding="utf-8",
        )

        policy = webui.content_security_policy(root)

        assert "https://www.gstatic.com" in policy
        assert "https://firestore.googleapis.com" in policy
        # Everything else stays shut: no inline script, no frames, no forms.
        assert "default-src 'none'" in policy
        assert "'unsafe-inline'" not in policy
        assert "frame-ancestors 'none'" in policy

    def test_the_shipped_placeholder_keeps_it_strict(self, tmp_path: Path) -> None:
        root = tmp_path / "panel"
        root.mkdir()
        (root / "firebase-config.js").write_text(
            'export const FIREBASE_CONFIG = { apiKey: "", projectId: "" };', encoding="utf-8"
        )

        assert webui.content_security_policy(root) == webui._BASE_CSP

    def test_the_policy_follows_an_edit(self, tmp_path: Path) -> None:
        root = tmp_path / "panel"
        root.mkdir()
        config = root / "firebase-config.js"
        config.write_text('apiKey: ""', encoding="utf-8")
        before = webui.content_security_policy(root)

        # A different size, so the cached answer cannot be the right one.
        config.write_text('apiKey: "AIza-filled-out-config"', encoding="utf-8")
        after = webui.content_security_policy(root)

        assert "gstatic" not in before
        assert "gstatic" in after

    def test_the_panel_refuses_to_be_framed_by_default(self, panel: Panel) -> None:
        _status, headers, _body = panel.get("/")

        # A panel nobody can see is a panel somebody is clickjacking: an
        # invisible overlay over "Send" is all it takes.
        assert "frame-ancestors 'none'" in headers["content-security-policy"]

    def test_an_origin_somebody_named_may_frame_it(self, tmp_path: Path) -> None:
        live = Panel(tmp_path, allowed_frames="https://dash.example")
        try:
            _status, headers, _body = live.get("/")
            policy = headers["content-security-policy"]

            assert "frame-ancestors 'self' https://dash.example" in policy
            # Everything else about the policy is unchanged.
            assert policy.startswith("default-src 'none'")
            assert "'unsafe-inline'" not in policy
        finally:
            live.close()

    def test_every_response_carries_the_headers(self, panel: Panel) -> None:
        _status, headers, _body = panel.get("/")

        assert headers["content-security-policy"].startswith("default-src 'none'")
        assert headers["x-content-type-options"] == "nosniff"
        assert headers["referrer-policy"] == "no-referrer"
        assert headers["cache-control"] == "no-store"


# ---------------------------------------------------------------------------
class TestGettingIn:
    def test_health_needs_no_token(self, panel: Panel) -> None:
        status, _headers, body = panel.get("/api/health", token=None)

        assert status == 200
        payload = json.loads(body)
        assert payload["deployment"] == "local"
        assert payload["chat"] is False
        # It says something is listening, and nothing about whose machine it is.
        assert "workspace" not in payload

    def test_everything_else_does(self, panel: Panel) -> None:
        for path in ("/api/status", "/api/sessions", "/api/settings", "/api/checkpoints"):
            status, _headers, body = panel.get(path, token=None)
            assert status == 401, path
            assert "token" in json.loads(body)["error"].lower()

    def test_a_wrong_token_is_refused(self, panel: Panel) -> None:
        status, _headers, _body = panel.get("/api/status", token="not-the-token")

        assert status == 401

    def test_the_token_may_ride_in_the_query_for_a_client_that_cannot_set_a_header(
        self, panel: Panel
    ) -> None:
        status, _headers, body = panel.get(f"/api/status?token={panel.config.token}", token=None)

        assert status == 200
        assert json.loads(body)["service"] == "jAIgent"

    def test_a_page_from_somewhere_else_is_refused(self, panel: Panel) -> None:
        status, _headers, _body = panel.get(
            "/api/status", headers={"origin": "https://evil.example"}
        )

        assert status == 403

    def test_a_sandboxed_frame_is_refused_too(self, panel: Panel) -> None:
        # ``Origin: null`` is what a sandboxed iframe sends, and a sandboxed
        # iframe is exactly how a hostile page would try to drive this panel.
        status, _headers, _body = panel.get("/api/status", headers={"origin": "null"})

        assert status == 403

    def test_the_panel_is_its_own_allowed_origin(self, panel: Panel) -> None:
        status, _headers, _body = panel.get(
            "/api/status", headers={"origin": f"http://127.0.0.1:{panel.port}"}
        )

        assert status == 200

    def test_a_rebound_hostname_is_refused(self, panel: Panel) -> None:
        # DNS rebinding: evil.example resolves to 127.0.0.1 and the browser then
        # treats this server as same-origin with the attacker's page. The Host
        # header still names their domain, and that is what is checked.
        status, _headers, _body = panel.get("/api/status", headers={"host": "evil.example"})

        assert status == 403

    def test_a_preflight_from_elsewhere_gets_no_cors_headers(self, panel: Panel) -> None:
        status, headers, _body = panel.request(
            "OPTIONS", "/api/chat", headers={"origin": "https://evil.example"}
        )

        assert status == 403
        assert "access-control-allow-origin" not in {k.lower() for k in headers}

    def test_an_unknown_route_is_a_plain_404(self, panel: Panel) -> None:
        status, _headers, body = panel.get("/api/nothing-here")

        assert status == 404
        assert "nothing-here" in json.loads(body)["error"]


# ---------------------------------------------------------------------------
class TestWhatItShows:
    def test_the_status_describes_the_machine(self, panel: Panel) -> None:
        payload = panel.json_get("/api/status")

        assert payload["service"] == "jAIgent"
        assert payload["model"] == "gpt-4o-mini"
        assert payload["chat"] is False
        assert payload["panel"] is True
        assert payload["workspace"].endswith("workspace")

    def test_the_settings_never_carry_a_secret(self, panel: Panel) -> None:
        payload = panel.json_get("/api/settings")

        assert payload["settings"]["api_key"] == "<set>"
        assert panel.settings.api_key not in json.dumps(payload)

    def test_an_empty_store_lists_nothing(self, panel: Panel) -> None:
        payload = panel.json_get("/api/sessions")

        assert payload == {"sessions": [], "count": 0, "limit": 50, "query": ""}

    def test_a_saved_session_appears_newest_first(self, panel: Panel) -> None:
        older = sessions.Session.new(
            provider="openai", model="gpt-4o-mini", workspace="/w", source="cli"
        )
        older.updated = time.time() - 3600
        older.save()
        newer = sessions.Session.new(
            provider="openai", model="gpt-4o-mini", workspace="/w", source="web"
        )
        newer.rename("the newer one")
        newer.save()

        payload = panel.json_get("/api/sessions")

        assert [row["id"] for row in payload["sessions"]] == [newer.id, older.id]
        assert payload["sessions"][0]["title"] == "the newer one"
        assert payload["sessions"][1]["source"] == "cli"

    def test_one_session_comes_back_with_its_transcript(self, panel: Panel) -> None:
        saved = sessions.Session.new(
            provider="openai", model="gpt-4o-mini", workspace="/w", source="cli"
        )
        saved.messages = [
            {"role": "user", "content": "what changed?"},
            {"role": "assistant", "content": "two files"},
            {"role": "tool", "content": "payload the panel should not show"},
        ]
        saved.save()

        payload = panel.json_get(f"/api/sessions/{saved.id}")

        assert payload["messages"] == [
            {"role": "user", "text": "what changed?"},
            {"role": "assistant", "text": "two files"},
        ]

    @pytest.mark.parametrize(
        "identifier",
        ["../../etc/passwd", "..%2f..%2fetc", "no-such-session", "a/b", "%00.json"],
    )
    def test_a_session_id_is_validated_before_it_reaches_the_disk(
        self, panel: Panel, identifier: str
    ) -> None:
        status, _headers, _body = panel.get(f"/api/sessions/{identifier}")

        assert status == 404

    def test_a_trailing_slash_is_the_listing_not_an_empty_id(self, panel: Panel) -> None:
        assert panel.get("/api/sessions/")[0] == 200

    def test_search_finds_a_conversation_by_its_words(self, panel: Panel) -> None:
        saved = sessions.Session.new(
            provider="openai", model="gpt-4o-mini", workspace="/w", source="cli"
        )
        saved.messages = [{"role": "user", "content": "refactor the payment handler"}]
        saved.save()

        payload = panel.json_get("/api/sessions?q=payment")

        assert [row["id"] for row in payload["sessions"]] == [saved.id]
        assert panel.json_get("/api/sessions?q=nothing-like-it")["sessions"] == []

    def test_a_listing_is_capped(self, panel: Panel) -> None:
        payload = panel.json_get("/api/sessions?limit=100000")

        assert payload["limit"] == webui.MAX_LIST

    def test_checkpoints_are_listed_without_their_contents(self, panel: Panel) -> None:
        from jaigent.checkpoint import CheckpointStore

        target = panel.settings.workspace / "notes.md"
        target.write_text("the original", encoding="utf-8")
        CheckpointStore(panel.settings.workspace).capture([target], label="before write_file")

        payload = panel.json_get("/api/checkpoints")

        assert payload["checkpoints"][0]["label"] == "before write_file"
        assert payload["checkpoints"][0]["files"][0]["path"].endswith("notes.md")
        # The snapshot's bytes stay on disk: the panel reports what exists, not
        # what the file said.
        assert "the original" not in json.dumps(payload)


# ---------------------------------------------------------------------------
class TestServingThePanel:
    def test_the_index_is_html(self, panel: Panel) -> None:
        status, headers, body = panel.get("/")

        assert status == 200
        assert headers["content-type"] == "text/html; charset=utf-8"
        assert b"jAIgent" in body

    def test_the_script_is_javascript_and_the_manifest_is_json(self, panel: Panel) -> None:
        assert panel.get("/app.js")[1]["content-type"] == "text/javascript; charset=utf-8"
        assert (
            panel.get("/manifest.webmanifest")[1]["content-type"]
            == "application/manifest+json; charset=utf-8"
        )

    def test_traversal_over_http_is_a_404(self, panel: Panel) -> None:
        for path in (
            "/../../pyproject.toml",
            "/%2e%2e/%2e%2e/pyproject.toml",
            "/../src/jaigent/cli.py",
        ):
            status, _headers, _body = panel.get(path)
            assert status == 404, path

    def test_a_head_gets_the_headers_and_no_body(self, panel: Panel) -> None:
        # Proxies, uptime checks and link previews all send HEAD. A 501 for a
        # method the server otherwise serves reads as broken from the outside.
        status, headers, body = panel.request("HEAD", "/")

        assert status == 200
        assert body == b""
        assert int(headers["content-length"]) > 0
        assert headers["content-type"] == "text/html; charset=utf-8"

        api_status, api_headers, api_body = panel.request("HEAD", "/api/status")

        assert api_status == 200
        assert api_body == b""
        assert api_headers["content-type"] == "application/json; charset=utf-8"

        # Still behind the token: a HEAD is a request like any other, and it
        # would otherwise be a way to find out what is on this port for free.
        assert panel.request("HEAD", "/api/status", token=None)[0] == 401

    def test_a_missing_file_is_a_404(self, panel: Panel) -> None:
        assert panel.get("/not-a-file.js")[0] == 404

    def test_the_panel_needs_no_token_to_load(self, panel: Panel) -> None:
        # Otherwise the address the CLI prints would have to authenticate before
        # it could even draw the form that asks for the token.
        assert panel.get("/", token=None)[0] == 200
        assert panel.get("/app.js", token=None)[0] == 200


# ---------------------------------------------------------------------------
class TestRunningATurn:
    def test_a_read_only_panel_refuses(self, panel: Panel) -> None:
        status, events = panel.post_chat("do something")

        assert status == 403
        assert "read-only" in events[0][1]["error"]

    def test_the_events_arrive_in_the_order_they_happened(self, chatting: Panel) -> None:
        status, events = chatting.post_chat("summarise notes.md")
        names = [name for name, _data in events]

        assert status == 200
        assert names == ["session", "tool", "tool", "token", "token", "done"]
        assert events[1][1] == {"state": "start", "tool": "read_file"}
        assert events[2][1]["detail"] == "first line"
        assert "".join(data["text"] for name, data in events if name == "token") == "All done."

    def test_the_turn_is_saved_as_a_web_session(self, chatting: Panel) -> None:
        _status, events = chatting.post_chat("summarise notes.md")
        identifier = session_id(events)

        saved = sessions.load(identifier)

        assert saved is not None
        assert saved.source == "web"
        assert saved.title == "summarise notes.md"
        assert saved.turns == 1
        assert saved.usage["total_tokens"] == 42

    def test_the_done_event_carries_what_it_cost(self, chatting: Panel) -> None:
        _status, events = chatting.post_chat("summarise notes.md")
        done = dict(events)["done"]

        assert done["session"] == session_id(events)
        assert done["output"] == "All done."
        assert done["tokens"] == 42
        assert done["usd"] == pytest.approx(0.0011)
        assert done["elapsed"] == 0.25

    def test_a_second_turn_continues_the_same_conversation(self, chatting: Panel) -> None:
        _status, first = chatting.post_chat("the first thing")
        identifier = session_id(first)

        _status, second = chatting.post_chat("and the second", session=identifier)

        assert session_id(second) == identifier
        assert chatting.agent.loaded, "the saved history was never handed back to the agent"
        assert chatting.agent.prompts == ["the first thing", "and the second"]
        assert sessions.load(identifier).turns == 1  # one saved conversation, two turns of history

    def test_an_unknown_session_is_refused_rather_than_invented(self, chatting: Panel) -> None:
        status, events = chatting.post_chat("hello", session="not-a-real-session")

        assert status == 404
        assert "No session" in events[0][1]["error"]

    def test_an_empty_prompt_is_refused(self, chatting: Panel) -> None:
        status, events = chatting.post_chat("   ")

        assert status == 400
        assert "prompt is required" in events[0][1]["error"]

    def test_a_huge_prompt_is_refused(self, chatting: Panel) -> None:
        status, events = chatting.post_chat("x" * (webui.MAX_PROMPT_CHARS + 1))

        assert status == 400
        assert str(webui.MAX_PROMPT_CHARS) in events[0][1]["error"]

    def test_a_body_that_is_not_json_is_refused(self, chatting: Panel) -> None:
        connection = http.client.HTTPConnection("127.0.0.1", chatting.port, timeout=10)
        try:
            connection.request(
                "POST",
                "/api/chat",
                body=b"not json at all",
                headers={
                    "authorization": f"Bearer {chatting.config.token}",
                    "content-type": "application/json",
                },
            )
            response = connection.getresponse()
            payload = json.loads(response.read())
        finally:
            connection.close()

        assert response.status == 400
        assert "prompt is required" in payload["error"]

    def test_a_turn_that_raises_is_reported_and_the_server_survives(self, tmp_path: Path) -> None:
        class Exploding(ScriptedAgent):
            def run(self, prompt: str) -> AgentResult:
                raise RuntimeError("the provider went away")

        live = Panel(tmp_path, chat=True, agent=Exploding())
        try:
            _status, events = live.post_chat("do something")

            assert [name for name, _data in events][-1] == "error"
            assert "the provider went away" in dict(events)["error"]["error"]
            # The next request still works: one failed turn is not a dead panel.
            assert live.get("/api/status")[0] == 200
        finally:
            live.close()

    def test_two_turns_on_one_conversation_do_not_interleave(self, tmp_path: Path) -> None:
        """Two tabs, one conversation: the second waits for the first.

        Without a lock both turns load the same history, and whichever saves
        last wins, so one of the two answers simply disappears from the
        conversation. The assertion is on the outcome rather than on timings:
        after both turns, the saved session has to hold both of them.
        """
        order: list[str] = []
        inside = threading.Event()

        class Slow(ScriptedAgent):
            def run(self, prompt: str) -> AgentResult:
                order.append(f"start {prompt}")
                if prompt == "first":
                    inside.set()
                    time.sleep(0.3)  # long enough for the second request to arrive
                self.history = [
                    *self.history,
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "content": self.output},
                ]
                order.append(f"end {prompt}")
                return AgentResult(output=self.output, usage={"total_tokens": 1})

        live = Panel(tmp_path, chat=True, agent=Slow())
        try:
            saved = sessions.Session.new(
                provider="openai", model="gpt-4o-mini", workspace="/w", source="web"
            )
            saved.messages = [{"role": "user", "content": "earlier"}]
            saved.save()

            results: list[Any] = []
            first = threading.Thread(
                target=lambda: results.append(live.post_chat("first", session=saved.id))
            )
            first.start()
            assert inside.wait(timeout=10), "the first turn never reached the agent"

            second = threading.Thread(
                target=lambda: results.append(live.post_chat("second", session=saved.id))
            )
            second.start()
            first.join(timeout=20)
            second.join(timeout=20)

            assert order == ["start first", "end first", "start second", "end second"], order
            assert all(status == 200 for status, _events in results)
            reloaded = sessions.load(saved.id)
            assert reloaded is not None
            assert [message["content"] for message in reloaded.messages] == [
                "earlier",
                "first",
                "All done.",
                "second",
                "All done.",
            ]
        finally:
            live.close()

    def test_two_different_conversations_run_at_the_same_time(self, tmp_path: Path) -> None:
        started = threading.Event()
        release = threading.Event()

        class Waiting(ScriptedAgent):
            def run(self, prompt: str) -> AgentResult:
                started.set()
                release.wait(timeout=5)
                return super().run(prompt)

        live = Panel(tmp_path, chat=True, agent=Waiting())
        try:
            threads = [
                threading.Thread(target=lambda: live.post_chat("one"), daemon=True),
                threading.Thread(target=lambda: live.post_chat("two"), daemon=True),
            ]
            threads[0].start()
            assert started.wait(timeout=5)
            threads[1].start()
            # The second turn is not behind the first: it reaches run() while the
            # first is still waiting, which is the only way both finish together.
            time.sleep(0.2)
            release.set()
            for thread in threads:
                thread.join(timeout=10)
            assert not any(thread.is_alive() for thread in threads)
        finally:
            release.set()
            live.close()


# ---------------------------------------------------------------------------
class TestThePanelItself:
    """What the shipped folder promises, checked rather than assumed."""

    def test_the_page_loads_one_script_and_no_inline_anything(self) -> None:
        root = webui.asset_dir()
        assert root is not None
        html = (root / "index.html").read_text(encoding="utf-8")

        assert html.count("<script") == 1
        assert 'src="./app.js"' in html
        assert "onclick=" not in html
        assert "<style" not in html

    def test_the_code_never_builds_html_out_of_a_string(self) -> None:
        root = webui.asset_dir()
        assert root is not None
        script = strip_comments((root / "app.js").read_text(encoding="utf-8"))

        # A transcript is model output. If it were ever parsed as markup, one
        # crafted answer would run in the page that shows it.
        for forbidden in (
            "innerHTML",
            "outerHTML",
            "insertAdjacentHTML",
            "document.write",
            "eval(",
        ):
            assert forbidden not in script, forbidden
        # And everything it does draw goes through textContent.
        assert "textContent" in script

    def test_nothing_points_at_another_origin(self) -> None:
        root = webui.asset_dir()
        assert root is not None
        allowed = ("127.0.0.1", "localhost", "gstatic.com", "firebase", "w3.org", "example")

        for path in root.rglob("*"):
            if path.suffix not in {".js", ".html", ".css", ".webmanifest"}:
                continue
            text = strip_comments(path.read_text(encoding="utf-8"))
            for line in text.splitlines():
                if "http://" not in line and "https://" not in line:
                    continue
                # The panel fetches same-origin only; the one remote origin it
                # may ever load is the Firebase SDK, named in the config, and a
                # loopback address in a placeholder is not a request.
                assert any(host in line for host in allowed), (path.name, line.strip()[:120])

    def test_no_credentials_shipped(self) -> None:
        root = webui.asset_dir()
        assert root is not None
        config = (root / "firebase-config.js").read_text(encoding="utf-8")

        # Every field of the exported config is still empty: what is committed
        # here is a shape, not somebody's project.
        body = config.split("export const FIREBASE_CONFIG = {", 1)[1].split("};", 1)[0]
        values = [
            line.split(":", 1)[1].strip().rstrip(",") for line in body.splitlines() if ":" in line
        ]
        assert values and set(values) == {'""'}
        assert "service_account" not in config.lower()
        assert "private_key" not in config.lower()

    def test_the_panel_asks_before_it_mirrors_a_transcript(self) -> None:
        root = webui.asset_dir()
        assert root is not None
        script = (root / "firebase.js").read_text(encoding="utf-8")

        assert "mirrorTranscripts" in script
        assert "syncOptions.mirrorTranscripts" in script

    def test_every_element_the_code_asks_for_is_on_the_page(self) -> None:
        """A renamed id is a blank panel, and nothing else would catch it.

        The page has no framework to complain about a missing node: `byId` throws
        at start-up and the reader sees an empty tab with one line in a console
        they were not told to open.
        """
        import re

        root = webui.asset_dir()
        assert root is not None
        html = (root / "index.html").read_text(encoding="utf-8")
        script = strip_comments((root / "app.js").read_text(encoding="utf-8"))

        on_page = set(re.findall(r'\bid="([^"]+)"', html))
        asked_for = set(re.findall(r'byId\("([^"]+)"\)', script))

        assert asked_for, "the panel stopped asking for elements by id"
        assert asked_for <= on_page, sorted(asked_for - on_page)

    def test_the_tabs_and_the_views_line_up(self) -> None:
        import re

        root = webui.asset_dir()
        assert root is not None
        html = (root / "index.html").read_text(encoding="utf-8")

        tabs = set(re.findall(r'data-view="([^"]+)"', html))
        views = {name[len("view-") :] for name in re.findall(r'id="(view-[^"]+)"', html)}

        assert tabs and tabs == views

    def test_the_worker_hosts_the_same_folder(self) -> None:
        root = webui.asset_dir()
        assert root is not None
        worker = root.parent / "worker.js"
        config = root.parent / "wrangler.toml"

        assert worker.is_file() and config.is_file()
        assert "./public" in config.read_text(encoding="utf-8")
        assert "ASSETS" in worker.read_text(encoding="utf-8")
        # The Worker says plainly that it has no agent, rather than pretending.
        assert "501" in worker.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------
class TestReachingItThroughATunnel:
    """The one setting that widens who may talk to the panel, and its limits.

    A tunnel or a reverse proxy forwards requests under a hostname that is not
    this machine's, so refusing it is what makes the DNS-rebinding check mean
    anything. Naming an origin is therefore an explicit act, and naming one does
    not change anything else: the server still binds loopback, and every request
    still needs the token.
    """

    def test_an_origin_nobody_named_is_refused(self, panel: Panel) -> None:
        status, _headers, _body = panel.get(
            "/api/status", headers={"origin": "https://panel.example.com"}
        )

        assert status == 403

    def test_a_named_origin_is_accepted(self, tmp_path: Path) -> None:
        live = Panel(tmp_path, allowed_origins="https://panel.example.com")
        try:
            status, _headers, _body = live.get(
                "/api/status", headers={"origin": "https://panel.example.com"}
            )

            assert status == 200
        finally:
            live.close()

    def test_the_proxy_hostname_is_accepted_in_host_too(self, tmp_path: Path) -> None:
        # A reverse proxy that rewrites nothing sends its own hostname, and the
        # rebinding check would refuse the request before the route ran.
        live = Panel(tmp_path, allowed_origins="https://panel.example.com")
        try:
            status, _headers, _body = live.get("/api/status", headers={"host": "panel.example.com"})

            assert status == 200
        finally:
            live.close()

    def test_naming_one_origin_does_not_name_another(self, tmp_path: Path) -> None:
        live = Panel(tmp_path, allowed_origins="https://panel.example.com")
        try:
            for origin in (
                "https://other.example.com",
                "http://panel.example.com",
                "https://panel.example.com:8443",
                "https://sub.panel.example.com",
            ):
                status, _headers, _body = live.get("/api/status", headers={"origin": origin})
                assert status == 403, origin
        finally:
            live.close()

    def test_an_allowed_origin_still_needs_the_token(self, tmp_path: Path) -> None:
        live = Panel(tmp_path, allowed_origins="https://panel.example.com")
        try:
            status, _headers, body = live.get(
                "/api/status", token=None, headers={"origin": "https://panel.example.com"}
            )

            assert status == 401
            assert "token" in json.loads(body)["error"].lower()
        finally:
            live.close()

    def test_it_still_binds_loopback(self, tmp_path: Path) -> None:
        with pytest.raises(ConfigurationError, match="loopback only"):
            webui.WebConfig(host="0.0.0.0", allowed_origins="https://panel.example.com")

    @pytest.mark.parametrize("raw", ["*", "https://*.example.com", "*.example.com"])
    def test_a_wildcard_is_refused(self, raw: str) -> None:
        # One wildcard would undo the whole Origin check, which is the only thing
        # between this panel and every other page open in the same browser.
        with pytest.raises(ConfigurationError, match="wildcard"):
            webui.parse_origins(raw)

    @pytest.mark.parametrize("raw", ["panel.example.com", "ftp://panel.example.com", "//x", "::::"])
    def test_something_that_is_not_an_origin_is_refused(self, raw: str) -> None:
        with pytest.raises(ConfigurationError, match="not an origin"):
            webui.parse_origins(raw)

    def test_origins_are_normalised_once(self) -> None:
        parsed = webui.parse_origins(
            ["https://Panel.Example.com/", "https://panel.example.com", "http://127.0.0.1:9000/x"]
        )

        assert parsed == ("http://127.0.0.1:9000", "https://panel.example.com")

    def test_a_default_port_is_not_part_of_the_origin(self) -> None:
        assert webui.parse_origins("https://panel.example.com:443") == (
            "https://panel.example.com",
        )
        assert webui.parse_origins("http://panel.example.com:80") == ("http://panel.example.com",)
        assert webui.parse_origins("https://panel.example.com:8443") == (
            "https://panel.example.com:8443",
        )

    def test_a_comma_separated_environment_value_is_read(self) -> None:
        parsed = webui.parse_origins("https://one.example, https://two.example")

        assert parsed == ("https://one.example", "https://two.example")

    def test_the_command_passes_the_flag_through(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from jaigent import cli

        seen: dict[str, Any] = {}

        class ServerStub:
            server_address = ("127.0.0.1", 8765)

            def serve_forever(self) -> None:
                pass

            def server_close(self) -> None:
                pass

        def build_server(active: Any, config: Any, factory: Any = None) -> Any:
            seen["config"] = config
            return ServerStub()

        monkeypatch.setattr(cli, "resolve_settings", lambda _args: settings_for(tmp_path))
        monkeypatch.setattr(cli.webui, "build_server", build_server)
        monkeypatch.setattr(cli, "console", _quiet_console())

        code = cli.main(
            [
                "web",
                "--no-browser",
                "--allow-origin",
                "https://one.example",
                "--allow-origin",
                "https://two.example/extra",
            ]
        )

        assert code == 0
        assert seen["config"].allowed_origins == ("https://one.example", "https://two.example")

    def test_the_environment_is_used_when_no_flag_is_given(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from jaigent import cli

        seen: dict[str, Any] = {}

        class ServerStub:
            server_address = ("127.0.0.1", 8765)

            def serve_forever(self) -> None:
                pass

            def server_close(self) -> None:
                pass

        def build_server(active: Any, config: Any, factory: Any = None) -> Any:
            seen["config"] = config
            return ServerStub()

        monkeypatch.setenv("JAIGENT_WEB_ALLOWED_ORIGINS", "https://env.example,")
        monkeypatch.setattr(cli, "resolve_settings", lambda _args: settings_for(tmp_path))
        monkeypatch.setattr(cli.webui, "build_server", build_server)
        monkeypatch.setattr(cli, "console", _quiet_console())

        assert cli.main(["web", "--no-browser"]) == 0
        assert seen["config"].allowed_origins == ("https://env.example",)


# ---------------------------------------------------------------------------
class TestTheCommand:
    """`jaigent web`, from the argument line to the address it prints."""

    @staticmethod
    def _run(
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        argv: list[str],
        *,
        approval: str = "auto",
        stub_server: bool = True,
    ) -> tuple[int, dict[str, Any], list[str]]:
        """Run the command with the server stubbed out and the browser recorded."""
        from jaigent import cli

        seen: dict[str, Any] = {}
        opened: list[str] = []
        settings = settings_for(tmp_path, approval=approval)

        def build_server(active: Any, config: Any, factory: Any = None) -> Any:
            seen["config"] = config
            seen["settings"] = active
            seen["factory"] = factory

            class ServerStub:
                # Echo the port back, the way a real socket does: the command
                # reads the bound address rather than trusting the argument, so
                # `--port 0` reports the port the operating system chose.
                server_address = ("127.0.0.1", config.port)

                def serve_forever(self) -> None:
                    seen["served"] = True

                def server_close(self) -> None:
                    seen["closed"] = True

            return ServerStub()

        monkeypatch.setattr(cli, "resolve_settings", lambda _args: settings)
        if stub_server:
            monkeypatch.setattr(cli.webui, "build_server", build_server)
        # cmd_web imports webbrowser inside the function, so patch the module
        # it will resolve rather than an attribute of cli.
        import webbrowser

        monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url) or True)
        monkeypatch.setattr(cli, "console", _quiet_console())
        code = cli.main(argv)
        return code, seen, opened

    def test_it_serves_the_panel_on_loopback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        code, seen, opened = self._run(tmp_path, monkeypatch, ["web", "--no-browser"])

        assert code == 0
        assert seen["served"] and seen["closed"]
        assert seen["config"].host == "127.0.0.1"
        assert seen["config"].chat is False
        assert opened == []

    def test_it_opens_the_address_it_prints(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        code, seen, opened = self._run(tmp_path, monkeypatch, ["web"])

        assert code == 0
        # The token rides in the fragment, so opening the address is enough: no
        # second step, and nothing for a log to capture.
        assert opened == [seen["config"].url]
        assert opened[0].endswith(f"#token={seen['config'].token}")

    def test_the_port_flag_reaches_the_server(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _code, seen, _opened = self._run(
            tmp_path, monkeypatch, ["web", "--port", "9101", "--no-browser"]
        )

        assert seen["config"].port == 9101

    def test_a_fixed_token_is_honoured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _code, seen, _opened = self._run(
            tmp_path, monkeypatch, ["web", "--token", "bookmark-me", "--no-browser"]
        )

        assert seen["config"].token == "bookmark-me"

    def test_chat_needs_an_approval_policy_somebody_can_live_with(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The real build_server, because the refusal is its job: a stub would
        # happily serve a panel that cannot answer an approval prompt.
        code, seen, _opened = self._run(
            tmp_path,
            monkeypatch,
            ["web", "--chat", "--no-browser"],
            approval="ask",
            stub_server=False,
        )

        assert code == 78  # EX_CONFIG
        assert "served" not in seen, "it must refuse before it binds a port"

    def test_chat_with_yes_starts(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        code, seen, _opened = self._run(
            tmp_path, monkeypatch, ["web", "--chat", "--yes", "--no-browser"], approval="auto"
        )

        assert code == 0
        assert seen["config"].chat is True

    def test_a_port_already_taken_is_reported(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from jaigent import cli

        def refuse(*_args: Any, **_kwargs: Any):  # noqa: ANN202
            raise OSError(98, "Address already in use")

        monkeypatch.setattr(cli, "resolve_settings", lambda _args: settings_for(tmp_path))
        monkeypatch.setattr(cli.webui, "build_server", refuse)
        monkeypatch.setattr(cli, "console", _quiet_console())
        monkeypatch.setattr(cli, "err_console", _quiet_console())

        assert cli.main(["web", "--no-browser"]) == 1

    def test_the_command_is_registered_and_reachable_by_its_aliases(self) -> None:
        from jaigent import cli

        assert "web" in cli.COMMANDS
        assert cli.build_parser().parse_args(["web"]).command == "web"
        for word in ("panel", "ui", "dashboard", "browser"):
            assert cli.ALIASES[word] == ["web"]
            assert cli.normalise_argv([word, "--no-browser"])[0] == "web"

    def test_the_help_says_what_it_is(self, capsys: pytest.CaptureFixture[str]) -> None:
        from jaigent import cli

        with pytest.raises(SystemExit):
            cli.build_parser().parse_args(["web", "--help"])

        text = capsys.readouterr().out
        assert "loopback" in text or "browser" in text
        assert "--chat" in text
