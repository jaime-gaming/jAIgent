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
import os
import sys
import threading
import time
from pathlib import Path
from stat import S_IMODE
from typing import Any

import pytest

from jaigent import session as sessions
from jaigent import webui
from jaigent.agent import AgentResult
from jaigent.approval import Approver, Mode
from jaigent.checkpoint import CheckpointStore
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

    def test_chat_is_on_unless_somebody_took_it_away(self) -> None:
        # A panel that can be opened but not written to is a panel that gets
        # opened once. Writing is the default and --no-chat is the opt-out, so
        # the default is part of the contract and not an accident of a flag.
        assert webui.WebConfig(port=0).chat is True
        assert webui.WebConfig(port=0, chat=False).chat is False

    def test_an_ask_policy_starts_instead_of_being_refused(self, tmp_path: Path) -> None:
        """Every policy is one the panel can run under.

        ``ask`` used to be refused outright, on the reasoning that the prompt
        would be printed on a terminal nobody was watching. It is asked in the
        browser that started the turn instead, so there is nothing left to
        refuse, and a server has to be buildable to prove it.
        """
        settings = settings_for(tmp_path, approval="ask")
        server = webui.build_server(settings, webui.WebConfig(port=0))
        try:
            assert server.config.chat is True
            assert isinstance(server.approvals, webui.ApprovalDesk)
            assert server.approvals.pending() == []
        finally:
            server.server_close()

    def test_the_status_says_whether_the_panel_can_write(self, tmp_path: Path) -> None:
        # The editor has to know before the reader presses Save, not after.
        for approval, writes in (("auto", True), ("ask", True), ("dry-run", False)):
            payload = webui.status_payload(
                settings_for(tmp_path, approval=approval), webui.WebConfig(port=0)
            )
            assert payload["writes"] is writes, approval
            assert payload["approval_timeout"] == webui.APPROVAL_TIMEOUT


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


class TestTheFirebaseLogin:
    """The sign-in summary `jaigent web login` reads, on a config you wrote."""

    def test_the_shipped_config_reports_no_login(self) -> None:
        summary = webui.firebase_auth_summary()

        assert summary["configured"] is False
        assert summary["auth_enabled"] is False
        # The example values in the file's own doc comment are not the reader's.
        assert summary["project_id"] == ""
        assert summary["auth_domain"] == ""

    def test_a_filled_config_reports_the_project_and_the_gate(self, tmp_path: Path) -> None:
        root = tmp_path / "panel"
        root.mkdir()
        (root / "index.html").write_text("<html></html>", encoding="utf-8")
        (root / "firebase-config.js").write_text(
            '/* an example: projectId: "doc-example" */\n'
            'export const FIREBASE_CONFIG = { apiKey: "AIza",'
            ' authDomain: "acme.firebaseapp.com", projectId: "acme", appId: "1:web:x" };\n'
            "export const FIREBASE_AUTH = { enabled: true,"
            " providers: { google: true, password: false } };",
            encoding="utf-8",
        )

        summary = webui.firebase_auth_summary(root)

        assert summary["configured"] is True
        assert summary["auth_enabled"] is True
        # Read from the code, not from the doc comment's example.
        assert summary["project_id"] == "acme"
        assert summary["auth_domain"] == "acme.firebaseapp.com"
        assert summary["providers"] == {"google": True, "password": False}

    def test_the_gate_can_be_switched_off(self, tmp_path: Path) -> None:
        root = tmp_path / "panel"
        root.mkdir()
        (root / "firebase-config.js").write_text(
            'export const FIREBASE_CONFIG = { apiKey: "AIza",'
            ' authDomain: "a.firebaseapp.com", projectId: "a", appId: "1" };\n'
            "export const FIREBASE_AUTH = { enabled: false, providers: { google: true } };",
            encoding="utf-8",
        )

        summary = webui.firebase_auth_summary(root)

        assert summary["configured"] is True
        assert summary["auth_enabled"] is False

    def test_an_unfilled_project_never_enables_the_gate(self, tmp_path: Path) -> None:
        # Even with `enabled: true`, an empty config has nowhere to sign in to,
        # so the gate stays off rather than stranding the reader on a login.
        root = tmp_path / "panel"
        root.mkdir()
        (root / "firebase-config.js").write_text(
            'export const FIREBASE_CONFIG = { apiKey: "", projectId: "" };\n'
            "export const FIREBASE_AUTH = { enabled: true, providers: { google: true } };",
            encoding="utf-8",
        )

        summary = webui.firebase_auth_summary(root)

        assert summary["configured"] is False
        assert summary["auth_enabled"] is False

    def test_the_panel_carries_a_sign_in_and_a_way_out(self) -> None:
        root = webui.asset_dir()
        assert root is not None
        html = (root / "index.html").read_text(encoding="utf-8")

        for needed in ("gate-auth", "auth-google", "auth-signin", "auth-email", "signout"):
            assert f'id="{needed}"' in html, needed
        # The loopback token form is still there: a local panel never signed in.
        assert 'id="gate-form"' in html


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
        # Two `saved` events, and they are not noise: the first is the prompt on
        # disk before the provider was even called, the second is the finished
        # conversation. A panel that only hears about a save at the end loses
        # whatever died in the middle.
        assert names == [
            "session",
            "saved",
            "tool",
            "tool",
            "token",
            "token",
            "saved",
            "done",
        ]
        started = [data for name, data in events if name == "tool" and data["state"] == "start"]
        ended = [data for name, data in events if name == "tool" and data["state"] == "end"]
        assert started == [
            {
                "state": "start",
                "tool": "read_file",
                "call": "c1",
                "arguments": {"path": "notes.md"},
            }
        ]
        assert ended[0]["detail"] == "first line"
        # One call, one line: the ending carries the id of the beginning, which
        # is what lets the panel settle the line it drew instead of adding a
        # second one and making one read look like two things happening.
        assert ended[0]["call"] == started[0]["call"]
        assert "".join(data["text"] for name, data in events if name == "token") == "All done."

    def test_the_saved_events_carry_what_the_rail_needs(self, chatting: Panel) -> None:
        _status, events = chatting.post_chat("summarise notes.md")
        saved = [data for name, data in events if name == "saved"]

        assert len(saved) == 2
        assert saved[0]["title"] == "summarise notes.md"
        assert saved[-1]["id"] == session_id(events)
        assert saved[-1]["turns"] >= 1
        assert saved[-1]["tokens"] == 42

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
        # Chat is what the panel is for, so a bare `jaigent web` serves one that
        # can be written to. Taking that away is a flag, not the default.
        assert seen["config"].chat is True
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

    def test_an_ask_policy_starts_and_says_the_browser_will_answer(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The command that used to exit 78 now serves, and says so.

        The refusal was honest about the terminal it was written for: an
        approval prompt printed there had nobody to read it. The panel asks in
        the tab that started the turn, so the same combination now starts, and
        the line it prints is the one that tells somebody whether a write from
        the browser will stop and ask them first.
        """
        code, seen, _opened = self._run(
            tmp_path, monkeypatch, ["web", "--ask", "--no-browser"], approval="ask"
        )

        assert code == 0
        assert seen["served"], "an ask policy is no longer a reason to refuse"
        assert seen["config"].chat is True
        assert seen["settings"].approval == "ask"

    def test_no_chat_serves_a_panel_that_cannot_write(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        code, seen, _opened = self._run(tmp_path, monkeypatch, ["web", "--no-chat", "--no-browser"])

        assert code == 0
        assert seen["config"].chat is False

    def test_read_only_is_the_same_opt_out_serve_spells_that_way(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        _code, seen, _opened = self._run(
            tmp_path, monkeypatch, ["web", "--read-only", "--no-browser"]
        )

        assert seen["config"].chat is False

    def test_chat_is_still_accepted_by_a_script_that_spells_it(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # It is the default now, and an address or a shell alias written before
        # that keeps working rather than failing on an unknown flag.
        code, seen, _opened = self._run(tmp_path, monkeypatch, ["web", "--chat", "--no-browser"])

        assert code == 0
        assert seen["config"].chat is True

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

    def test_login_with_no_firebase_explains_itself(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import io

        from rich.console import Console

        from jaigent import cli

        captured = Console(file=io.StringIO(), force_terminal=False, width=100, no_color=True)
        monkeypatch.setattr(cli, "console", captured)

        # The shipped, empty config: no project, so no login to perform.
        code = cli.main(["web", "login"])

        assert code == 0
        assert "not configured" in captured.file.getvalue()

    def test_login_opens_the_hosted_panel_when_configured(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import io
        import webbrowser

        from rich.console import Console

        from jaigent import cli

        root = tmp_path / "panel"
        root.mkdir()
        (root / "index.html").write_text("<html></html>", encoding="utf-8")
        (root / "firebase-config.js").write_text(
            'export const FIREBASE_CONFIG = { apiKey: "AIza",'
            ' authDomain: "acme.firebaseapp.com", projectId: "acme", appId: "1" };\n'
            "export const FIREBASE_AUTH = { enabled: true, providers: { google: true } };",
            encoding="utf-8",
        )
        captured = Console(file=io.StringIO(), force_terminal=False, width=100, no_color=True)
        opened: list[str] = []
        monkeypatch.setattr(cli.webui, "asset_dir", lambda: root)
        monkeypatch.setattr(cli, "console", captured)
        monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url) or True)

        code = cli.main(["web", "login", "--url", "https://panel.example"])

        assert code == 0
        assert opened == ["https://panel.example"]
        assert "Sign in at" in captured.file.getvalue()

    def test_login_without_a_url_says_how_to_give_one(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import io
        import webbrowser

        from rich.console import Console

        from jaigent import cli

        root = tmp_path / "panel"
        root.mkdir()
        (root / "firebase-config.js").write_text(
            'export const FIREBASE_CONFIG = { apiKey: "AIza",'
            ' authDomain: "acme.firebaseapp.com", projectId: "acme", appId: "1" };\n'
            "export const FIREBASE_AUTH = { enabled: true, providers: { google: true } };",
            encoding="utf-8",
        )
        captured = Console(file=io.StringIO(), force_terminal=False, width=100, no_color=True)
        opened: list[str] = []
        monkeypatch.setattr(cli.webui, "asset_dir", lambda: root)
        monkeypatch.setattr(cli, "console", captured)
        monkeypatch.setattr(webbrowser, "open", lambda url: opened.append(url) or True)
        monkeypatch.delenv("JAIGENT_WEB_URL", raising=False)

        code = cli.main(["web", "login"])

        assert code == 0
        assert opened == []  # nothing to open until the reader names the panel
        assert "--url" in captured.file.getvalue()

    def test_the_help_says_what_it_is(self, capsys: pytest.CaptureFixture[str]) -> None:
        from jaigent import cli

        with pytest.raises(SystemExit):
            cli.build_parser().parse_args(["web", "--help"])

        text = capsys.readouterr().out
        assert "loopback" in text or "browser" in text
        assert "--chat" in text


# ---------------------------------------------------------------------------
class AskingAgent(ScriptedAgent):
    """An agent that stops and asks before it writes, as the real one does.

    It carries a real :class:`~jaigent.approval.Approver`, which is the only
    thing that makes the panel's approval bridge reachable: the panel swaps its
    own approver in over the top of this one, and a stub without the attribute
    would silently take the never-asks path.
    """

    def __init__(self, workspace: Path, *, calls: int = 1) -> None:
        super().__init__(script=[], output="Done.")
        self.approver = Approver(Mode.ASK, workspace=workspace)
        self.calls = calls
        self.decisions: list[Any] = []

    def run(self, prompt: str) -> AgentResult:
        self.prompts.append(prompt)
        for index in range(self.calls):
            arguments = {"path": f"file{index}.md", "content": f"change {index}\n"}
            if self.on_tool_start:
                self.on_tool_start("write_file", arguments)
            decision = self.approver.check("write_file", arguments, mutates=True)
            self.decisions.append(decision)
            if self.on_tool_call:
                self.on_tool_call(
                    "write_file",
                    arguments,
                    "Wrote it" if decision.allowed else f"ERROR: {decision.reason}",
                )
        answer = "Done."
        self.history = [
            *self.history,
            {"role": "user", "content": prompt},
            {"role": "assistant", "content": answer},
        ]
        return AgentResult(
            output=answer,
            usage={"total_tokens": 11},
            cost=Cost(input_tokens=8, output_tokens=3, usd=0.0002),
            elapsed=0.2,
        )


class FailingAgent(ScriptedAgent):
    """An agent that dies partway through a turn, after the prompt was taken."""

    def __init__(self, message: str = "the provider went away") -> None:
        super().__init__(script=[], output="")
        self.message = message

    def run(self, prompt: str) -> AgentResult:
        self.prompts.append(prompt)
        self.history = [*self.history, {"role": "user", "content": prompt}]
        raise RuntimeError(self.message)


def answer_from_another_thread(
    panel: Panel, decision: str, *, timeout: float = 12.0
) -> tuple[threading.Thread, list[Any]]:
    """Watch for the first approval and answer it, while the turn is blocked.

    The turn holds its own connection open until it is answered, so the answer
    has to come from somewhere else: this is the shape a second browser tab, or
    the same tab's own button, has from the server's point of view.
    """
    seen: list[Any] = []

    def worker() -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            waiting = panel.json_get("/api/approvals")["approvals"]
            if waiting:
                seen.append(waiting[0])
                seen.append(
                    panel.request(
                        "POST", "/api/approve", body={"id": waiting[0]["id"], "decision": decision}
                    )
                )
                return
            time.sleep(0.04)

    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    return thread, seen


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    folder = tmp_path / "workspace"
    folder.mkdir(exist_ok=True)
    return folder


@pytest.fixture
def asking(tmp_path: Path, workspace: Path):  # noqa: ANN201
    """A panel under an ask policy, with an agent that really does ask."""
    agent = AskingAgent(workspace)
    live = Panel(
        tmp_path,
        chat=True,
        agent=agent,
        settings_overrides={"approval": "ask"},
    )
    live.asking_agent = agent  # type: ignore[attr-defined]
    try:
        yield live
    finally:
        live.close()


class TestAskingInTheBrowser:
    """An ask policy, answered where the reader is instead of being refused."""

    def test_a_change_is_asked_about_and_allowed(self, asking: Panel) -> None:
        thread, seen = answer_from_another_thread(asking, "allow")
        try:
            status, events = asking.post_chat("write a file")
        finally:
            thread.join(timeout=10)

        assert status == 200
        names = [name for name, _data in events]
        assert "approval" in names
        asked = dict(events)["approval"]
        assert asked["tool"] == "write_file"
        # The same diff the terminal would have printed, as text: one
        # description of one change, whichever surface asks for it.
        assert "write_file" in asked["preview"]
        assert "+change 0" in asked["preview"]
        assert asked["arguments"]["path"] == "file0.md"
        assert asked["timeout"] == webui.APPROVAL_TIMEOUT

        assert seen, "nothing was waiting to be answered"
        assert seen[1][0] == 200
        agent = asking.asking_agent  # type: ignore[attr-defined]
        assert agent.decisions and agent.decisions[0].allowed is True
        assert names[-1] == "done"

    def test_a_change_can_be_declined(self, asking: Panel) -> None:
        thread, _seen = answer_from_another_thread(asking, "deny")
        try:
            _status, events = asking.post_chat("write a file")
        finally:
            thread.join(timeout=10)

        agent = asking.asking_agent  # type: ignore[attr-defined]
        assert agent.decisions[0].allowed is False
        assert "declined" in agent.decisions[0].reason
        # The refusal reaches the panel as the tool's own result, which is where
        # a reader looks for why nothing happened.
        ended = [data for name, data in events if name == "tool" and data["state"] == "end"]
        assert ended and ended[0]["ok"] is False

    def test_always_allow_stops_the_second_question(self, tmp_path: Path, workspace: Path) -> None:
        agent = AskingAgent(workspace, calls=2)
        live = Panel(
            tmp_path,
            chat=True,
            agent=agent,
            settings_overrides={"approval": "ask"},
        )
        thread, _seen = answer_from_another_thread(live, "always")
        try:
            _status, events = live.post_chat("write two files")
        finally:
            thread.join(timeout=10)
            live.close()

        asked = [name for name, _data in events if name == "approval"]
        assert len(asked) == 1, "always means always, for the rest of the run"
        assert len(agent.decisions) == 2
        assert all(decision.allowed for decision in agent.decisions)

    def test_always_carries_to_the_next_turn_of_the_same_conversation(
        self, asking: Panel, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """`always` outlives the turn it was given in, as it does on a terminal.

        ``jaigent chat`` builds one approver for the whole session, so ``[a]lways``
        there means the rest of the conversation. A panel that rebuilt the set on
        every turn asked the same question again one message later, and a reader
        who had just answered it had no way to tell that from being ignored. The
        wait is shortened so that a regression fails rather than blocks.
        """
        monkeypatch.setattr(webui, "APPROVAL_TIMEOUT", 0.4)
        monkeypatch.setattr(webui, "APPROVAL_HEARTBEAT", 0.05)

        thread, _seen = answer_from_another_thread(asking, "always")
        try:
            _status, first = asking.post_chat("write a file")
        finally:
            thread.join(timeout=10)
        conversation = dict(first)["session"]["id"]

        # Nothing is going to answer this one. If it asks, that is the failure,
        # and the shortened timeout is what makes it a failure instead of a hang.
        _status, second = asking.post_chat("and another", session=conversation)

        assert [name for name, _data in second if name == "approval"] == []
        agent = asking.asking_agent  # type: ignore[attr-defined]
        assert len(agent.decisions) == 2, "the second turn never reached its write"
        assert all(decision.allowed for decision in agent.decisions)

    def test_always_does_not_carry_to_another_conversation(self, asking: Panel) -> None:
        """An answer given in one conversation is not permission for another.

        The grant is as wide as the conversation it was given in and no wider,
        which is the width a terminal chat's is: two tabs discussing two things
        do not share a "stop asking me", and a conversation picked up again in a
        later panel asks rather than inheriting a grant nobody remembers making.
        """
        thread, _seen = answer_from_another_thread(asking, "always")
        try:
            asking.post_chat("write a file")
        finally:
            thread.join(timeout=10)

        thread, seen = answer_from_another_thread(asking, "allow")
        try:
            _status, events = asking.post_chat("write a file")
        finally:
            thread.join(timeout=10)

        assert [name for name, _data in events if name == "approval"], (
            "a new conversation asked nothing"
        )
        assert seen and seen[0]["tool"] == "write_file"

    def test_nobody_answering_is_a_no(self, asking: Panel, monkeypatch: pytest.MonkeyPatch) -> None:
        # The wait is real, so the timeout is shortened rather than slept
        # through: what matters is which way it falls when nobody answers.
        monkeypatch.setattr(webui, "APPROVAL_TIMEOUT", 0.12)
        monkeypatch.setattr(webui, "APPROVAL_HEARTBEAT", 0.04)

        _status, events = asking.post_chat("write a file")

        agent = asking.asking_agent  # type: ignore[attr-defined]
        assert agent.decisions[0].allowed is False
        assert "Nobody answered" in agent.decisions[0].reason
        assert "Do not retry" in agent.decisions[0].reason
        assert [name for name, _data in events][-1] == "done"

    def test_the_question_is_listed_while_it_is_waiting(self, asking: Panel) -> None:
        thread, seen = answer_from_another_thread(asking, "allow")
        try:
            asking.post_chat("write a file")
        finally:
            thread.join(timeout=10)

        assert seen and seen[0]["tool"] == "write_file"
        # A tab that arrives late, or one that was reloaded, can still answer:
        # nothing is waiting once the turn is over.
        assert asking.json_get("/api/approvals")["approvals"] == []

    def test_an_answer_for_nothing_is_refused(self, asking: Panel) -> None:
        assert (
            asking.request("POST", "/api/approve", body={"id": "nope", "decision": "allow"})[0]
            == 404
        )
        assert (
            asking.request("POST", "/api/approve", body={"id": "x", "decision": "maybe"})[0] == 400
        )
        assert asking.request("POST", "/api/approve", body={"decision": "allow"})[0] == 400
        assert (
            asking.request(
                "POST", "/api/approve", body={"id": "x", "decision": "allow"}, token=None
            )[0]
            == 401
        )


class TestEditingTheRealFile:
    """What the panel writes, it writes here: to the workspace on this machine."""

    @pytest.fixture
    def panel(self, tmp_path: Path, workspace: Path):  # noqa: ANN201
        # Bytes, not text: a text-mode write translates "\n" to "\r\n" on
        # Windows, and a listing that reports sizes has to be checked against a
        # file whose size is the same on every platform.
        (workspace / "notes.md").write_bytes(b"hello\n")
        (workspace / "sub").mkdir()
        (workspace / "sub" / "app.py").write_bytes(b"print('hi')\n")
        live = Panel(tmp_path, chat=True)
        try:
            yield live
        finally:
            live.close()

    def test_a_folder_is_listed_with_what_the_panel_may_write(
        self, panel: Panel, workspace: Path
    ) -> None:
        payload = panel.json_get("/api/files")

        assert payload["path"] == "."
        assert payload["parent"] == ""
        assert [(entry["name"], entry["kind"]) for entry in payload["entries"]] == [
            ("sub", "dir"),
            ("notes.md", "file"),
        ]
        assert payload["entries"][1]["editable"] is True
        assert payload["entries"][1]["size"] == 6

    def test_a_subfolder_knows_where_it_came_from(self, panel: Panel) -> None:
        payload = panel.json_get("/api/files?path=sub")

        assert payload["path"] == "sub"
        assert payload["parent"] == "."
        assert [entry["name"] for entry in payload["entries"]] == ["app.py"]

    def test_a_file_is_read_from_disk(self, panel: Panel) -> None:
        payload = panel.json_get("/api/file?path=notes.md")

        assert payload["path"] == "notes.md"
        assert payload["text"] == "hello\n"
        assert payload["lines"] == 1
        assert payload["editable"] is True
        assert payload["modified"] > 0

    def test_saving_writes_the_file_and_takes_a_checkpoint(
        self, panel: Panel, workspace: Path
    ) -> None:
        before = len(CheckpointStore(workspace).history(limit=50))
        read = panel.json_get("/api/file?path=notes.md")

        status, _headers, payload = panel.request(
            "POST",
            "/api/file",
            body={"path": "notes.md", "text": "edited here\n", "base_modified": read["modified"]},
        )
        written = json.loads(payload)

        assert status == 200
        assert (workspace / "notes.md").read_text(encoding="utf-8") == "edited here\n"
        assert written["ok"] is True and written["path"] == "notes.md"
        assert written["checkpoint"], "an edit from a browser has to be undoable too"
        assert len(CheckpointStore(workspace).history(limit=50)) == before + 1
        # The checkpoint says where it came from, so the terminal's list of them
        # does not blame the agent for something a person typed.
        latest = CheckpointStore(workspace).latest()
        assert latest is not None and "web panel" in latest.label

    def test_a_save_keeps_the_permissions_of_the_file_it_replaces(
        self, panel: Panel, workspace: Path
    ) -> None:
        script = workspace / "run.sh"
        script.write_bytes(b"#!/bin/sh\necho one\n")
        script.chmod(0o755)
        before = S_IMODE(script.stat().st_mode)

        panel.request("POST", "/api/file", body={"path": "run.sh", "text": "#!/bin/sh\necho two\n"})

        assert script.read_text(encoding="utf-8") == "#!/bin/sh\necho two\n"
        # The mode it had, rather than a mode this test likes: Windows has no
        # execute bit and its chmod only moves the read-only flag, so 0o755 comes
        # back as 0o666 there. What every platform can promise is that saving
        # through a temporary file does not reset what the file was.
        after = S_IMODE(script.stat().st_mode)
        assert after == before
        if sys.platform != "win32":
            assert after & 0o111, "a script saved from a browser must still run"
        assert not list(workspace.glob(".*jaigent-saving")), "no temporary file left behind"

    def test_a_save_keeps_the_line_endings_the_file_had(
        self, panel: Panel, workspace: Path
    ) -> None:
        """Editing one line does not re-end every other line.

        A text box holds ``\\n`` for every line whatever the file had, and
        :meth:`Path.write_text` would then write this platform's own ending over
        the top of that: a CRLF file re-ended to LF here, and an LF file re-ended
        to CRLF on Windows, where a repository whose ``.gitattributes`` asks for
        LF would show a one-line edit as a change to every line in the file. So
        the ending is read off the file being replaced.
        """
        dos = workspace / "notes.bat"
        dos.write_bytes(b"@echo off\r\necho one\r\n")
        unix = workspace / "run.sh"
        unix.write_bytes(b"#!/bin/sh\necho one\n")

        assert panel.json_get("/api/file?path=notes.bat")["eol"] == "\r\n"
        assert panel.json_get("/api/file?path=run.sh")["eol"] == "\n"

        panel.request(
            "POST", "/api/file", body={"path": "notes.bat", "text": "@echo off\necho two\n"}
        )
        panel.request("POST", "/api/file", body={"path": "run.sh", "text": "#!/bin/sh\necho two\n"})

        assert dos.read_bytes() == b"@echo off\r\necho two\r\n"
        assert unix.read_bytes() == b"#!/bin/sh\necho two\n"

    def test_a_file_that_mixes_endings_has_no_convention_to_keep(self, workspace: Path) -> None:
        mixed = workspace / "mixed.md"
        mixed.write_bytes(b"one\r\ntwo\n")

        # Documented rather than incidental: with no one ending to preserve, the
        # file gets the one this machine writes, which is what it got before.
        assert webui.line_ending_of(mixed) == os.linesep
        assert webui.line_ending_of(workspace / "missing.md") == os.linesep

    def test_a_file_that_changed_underneath_is_not_replaced(self, panel: Panel) -> None:
        read = panel.json_get("/api/file?path=notes.md")

        status, _headers, payload = panel.request(
            "POST",
            "/api/file",
            body={"path": "notes.md", "text": "late\n", "base_modified": read["modified"] - 30},
        )

        assert status == 409
        assert "changed on disk" in json.loads(payload)["error"]

    def test_dry_run_does_not_write(self, tmp_path: Path, workspace: Path) -> None:
        (workspace / "notes.md").write_text("hello\n", encoding="utf-8")
        live = Panel(tmp_path, chat=True, settings_overrides={"approval": "dry-run"})
        try:
            status, _headers, payload = live.request(
                "POST", "/api/file", body={"path": "notes.md", "text": "nope\n"}
            )
        finally:
            live.close()

        assert status == 403
        assert "dry-run" in json.loads(payload)["error"]
        assert (workspace / "notes.md").read_text(encoding="utf-8") == "hello\n"

    def test_an_ask_policy_wants_the_reader_to_have_said_yes(
        self, tmp_path: Path, workspace: Path
    ) -> None:
        (workspace / "notes.md").write_text("hello\n", encoding="utf-8")
        live = Panel(tmp_path, chat=True, settings_overrides={"approval": "ask"})
        try:
            refused = live.request("POST", "/api/file", body={"path": "notes.md", "text": "x\n"})
            allowed = live.request(
                "POST", "/api/file", body={"path": "notes.md", "text": "x\n", "approved": True}
            )
        finally:
            live.close()

        assert refused[0] == 428
        assert allowed[0] == 200
        assert (workspace / "notes.md").read_text(encoding="utf-8") == "x\n"

    @pytest.mark.parametrize(
        "path",
        ["../outside.md", "/etc/passwd", "sub/../../escape.md", ".git/config", ".git"],
    )
    def test_the_workspace_is_the_whole_world(self, panel: Panel, path: str) -> None:
        assert panel.get(f"/api/file?path={path}")[0] in {403, 404}
        status, _headers, payload = panel.request(
            "POST", "/api/file", body={"path": path, "text": "x"}
        )
        assert status in {403, 404}, (status, payload[:200])

    def test_a_binary_file_is_not_offered_to_an_editor(self, panel: Panel, workspace: Path) -> None:
        (workspace / "image.bin").write_bytes(b"\x00\x01\x02binary")

        status, _headers, payload = panel.get("/api/file?path=image.bin")

        assert status == 404
        assert "not a text file" in json.loads(payload)["error"]

    def test_writing_needs_a_token_and_a_path(self, panel: Panel) -> None:
        assert (
            panel.request("POST", "/api/file", body={"path": "x.md", "text": "y"}, token=None)[0]
            == 401
        )
        assert panel.request("POST", "/api/file", body={"text": "y"})[0] == 400
        assert panel.request("POST", "/api/file", body={"path": "x.md"})[0] == 400
        assert panel.get("/api/file")[0] == 400

    def test_the_listing_needs_nothing_the_panel_does_not_already_know(self, panel: Panel) -> None:
        assert panel.get("/api/files", token=None)[0] == 401
        assert panel.get("/api/files?limit=not-a-number")[0] == 200


class TestKeepingWhatHappened:
    """A conversation is saved before it can be lost, and after however it ended."""

    def test_a_prompt_is_on_disk_before_the_provider_is_called(self, chatting: Panel) -> None:
        _status, events = chatting.post_chat("the first thing")
        saved = [data for name, data in events if name == "saved"]

        assert saved[0]["title"] == "the first thing"
        assert saved[0]["tokens"] == 0, "nothing had been spent when the prompt was kept"

    def test_a_turn_that_dies_halfway_keeps_the_prompt(self, tmp_path: Path) -> None:
        agent = FailingAgent()
        live = Panel(tmp_path, chat=True, agent=agent)
        try:
            status, events = live.post_chat("something that will fail")
        finally:
            live.close()

        assert status == 200
        assert [name for name, _data in events][-1] == "error"
        identifier = session_id(events)
        kept = sessions.load(identifier)
        assert kept is not None, "a failed turn used to take the whole exchange with it"
        assert kept.title == "something that will fail"
        assert [message["role"] for message in kept.messages] == ["user"]
        assert kept.messages[0]["content"] == "something that will fail"

    def test_a_turn_that_compacts_is_not_put_back_the_long_way(self, tmp_path: Path) -> None:
        """The save keeps the agent's own history, not the longest one it saw.

        A long conversation is compacted to a summary plus the last few
        messages, so the history a turn ends with can be shorter than the one it
        started with. Saving whichever was longer would undo the compaction on
        disk and the next turn would pay for it twice.
        """

        class Compacting(ScriptedAgent):
            def run(self, prompt: str) -> AgentResult:
                self.prompts.append(prompt)
                self.history = [
                    {"role": "system", "content": "a summary of a long conversation"},
                    {"role": "user", "content": prompt},
                    {"role": "assistant", "content": "Done."},
                ]
                return AgentResult(output="Done.", usage={"total_tokens": 3})

        agent = Compacting(script=[])
        live = Panel(tmp_path, chat=True, agent=agent)
        try:
            live.post_chat("one")
            identifier = ""
            first = sessions.list_sessions(limit=1)
            identifier = first[0].id
            seeded = sessions.load(identifier)
            assert seeded is not None
            seeded.touch(
                [
                    {"role": "user", "content": "a"},
                    {"role": "assistant", "content": "b"},
                    {"role": "user", "content": "c"},
                    {"role": "assistant", "content": "d"},
                ]
            )
            seeded.save()
            live.post_chat("two", session=identifier)
        finally:
            live.close()

        kept = sessions.load(identifier)
        assert kept is not None
        roles = [message["role"] for message in kept.messages]
        assert roles == ["system", "user", "assistant"], roles


class TestThePanelYouCanWriteIn:
    """What the shipped page promises about writing, checked rather than assumed.

    These are the parts of the panel a reader meets first and a regression hides
    easily: where the typing lands, whether a paste arrives, whether one tool
    call reads as one thing happening, how far the box grows, and whether an
    edit really goes to the file.
    """

    @pytest.fixture
    def page(self) -> dict[str, str]:  # noqa: ANN201
        root = webui.asset_dir()
        assert root is not None
        return {
            "html": (root / "index.html").read_text(encoding="utf-8"),
            "js": strip_comments((root / "app.js").read_text(encoding="utf-8")),
            "css": strip_comments((root / "styles.css").read_text(encoding="utf-8")),
        }

    def test_the_box_that_takes_the_typing_says_it_is_the_one(self, page: dict[str, str]) -> None:
        # A focus ring the browser draws by default was taken away, so the panel
        # has to put one back: without it there is nothing on screen to say
        # where a keystroke will land.
        assert 'id="composer-focus"' in page["html"]
        assert "setComposerFocus" in page["js"]
        assert ".composer-focused" in page["css"]
        assert 'addEventListener("focus", () => setComposerFocus(true))' in page["js"]
        assert 'addEventListener("blur", () => setComposerFocus(false))' in page["js"]

    def test_a_paste_arrives_whole_and_with_unix_line_endings(self, page: dict[str, str]) -> None:
        assert 'addEventListener("paste"' in page["js"]
        # Normalised, because a paste from Windows carries CRLF and a prompt full
        # of characters nobody typed is a prompt nobody can read back.
        assert r'replace(/\r\n?/g, "\n")' in page["js"]
        # And inserted at the caret rather than assigned over the whole box, so
        # the browser's own undo still undoes the paste.
        assert "setRangeText" in page["js"]
        # Ctrl+V with nothing focused goes to the message box: somebody reading
        # a transcript who copies an error is not going to click first.
        assert 'document.addEventListener("paste"' in page["js"]

    def test_typing_with_nothing_focused_goes_to_the_message_box(
        self, page: dict[str, str]
    ) -> None:
        assert "insertIntoPrompt(event.key)" in page["js"]
        # Shortcuts, fields and buttons keep their keystrokes.
        assert 'target.closest("button, a, [role=button]")' in page["js"]

    def test_the_box_grows_to_hold_what_is_in_it(self, page: dict[str, str]) -> None:
        # Two ceilings that have to agree: the script grows towards one and the
        # stylesheet caps at the other, and a stylesheet that wins quietly turns
        # a pasted page into one visible line with a scrollbar in it.
        assert "max-height: 70vh" in page["css"]
        assert "window.innerHeight * 0.7" in page["js"]
        assert "40vh" not in page["css"], "the old ceiling still caps the box"
        # A resized window leaves a height set for the old one.
        assert 'window.addEventListener("resize"' in page["js"]

    def test_the_editor_says_when_it_is_keeping_windows_endings(self, page: dict[str, str]) -> None:
        # A text box holds LF for every line, so the ending the file had is the
        # server's job to put back. Saying it out loud is what stops a reader
        # wondering why a Windows script did not come back re-ended.
        assert "eol: String(file.eol" in page["js"]
        assert "CRLF kept" in page["js"]

    def test_one_tool_call_is_one_line_that_settles(self, page: dict[str, str]) -> None:
        assert "function toolStart" in page["js"]
        assert "function toolEnd" in page["js"]
        assert "stream.lines.get(call)" in page["js"]
        assert 'line.classList.remove("tool-line-running")' in page["js"]
        assert ".tool-line-running" in page["css"]

    def test_the_always_button_says_what_it_is_always_about(self, page: dict[str, str]) -> None:
        # "Always" is the one answer here that keeps working after the card is
        # gone, so the button names the tool it is about to stop asking about and
        # says how long that lasts. A reader who cannot tell how wide the grant is
        # either does not press it or presses it and is surprised later.
        assert 'id="approval-always"' in page["html"]
        assert "Always allow ${data.tool}" in page["js"]
        assert "in this conversation" in page["js"]

    def test_the_card_that_stops_the_turn_says_what_it_is_stopping_for(
        self, page: dict[str, str]
    ) -> None:
        # The turn waits on this card, so it is a dialog and not a group, and it
        # takes its name from the title app.js fills in: a fixed label says "a
        # change" where the reader needs to hear "write_file wants to make a
        # change". The wait and the diff are the description, and the focus moves
        # in, which is what makes a screen reader read any of it out.
        assert 'role="alertdialog"' in page["html"]
        assert 'aria-labelledby="approval-title"' in page["html"]
        assert 'aria-describedby="approval-wait approval-preview"' in page["html"]
        assert 'byId("approval-allow").focus()' in page["js"]

    def test_the_editor_points_a_screen_reader_at_its_own_hint(self, page: dict[str, str]) -> None:
        # The hint is the only place that says whether a save would write over a
        # change made on disk, and whether CRLF is being kept. Pointing the text
        # box at it costs one attribute and turns both into something read aloud
        # rather than something only a sighted reader sees.
        assert 'id="editor-text"' in page["html"]
        assert 'aria-describedby="editor-hint"' in page["html"]
        assert 'id="editor-hint"' in page["html"]

    def test_an_answer_is_drawn_once_per_frame_not_once_per_token(
        self, page: dict[str, str]
    ) -> None:
        assert "requestAnimationFrame" in page["js"]
        assert "function flushText" in page["js"]
        assert "stream.buffer" in page["js"]
        # The caret says an answer is still arriving, and is taken away when it
        # is not: a caret left blinking at the end of a finished answer reads as
        # a panel that has hung.
        assert "turn-bubble-streaming" in page["js"]
        assert 'classList.remove("turn-bubble-streaming")' in page["js"]

    def test_a_status_line_repeated_is_not_a_status_line_redrawn(
        self, page: dict[str, str]
    ) -> None:
        assert "state.busyLabel" in page["js"]

    def test_a_save_updates_the_rail_instead_of_rebuilding_it(self, page: dict[str, str]) -> None:
        assert '"saved"' in page["js"]
        assert "function applySaved" in page["js"]
        assert "state.sessions.find" in page["js"]

    def test_a_change_is_asked_about_where_the_reader_is(self, page: dict[str, str]) -> None:
        for identifier in ("approval", "approval-preview", "approval-allow", "approval-deny"):
            assert f'id="{identifier}"' in page["html"], identifier
        assert "function showApproval" in page["js"]
        assert "function answerApproval" in page["js"]
        assert '"/api/approve"' in page["js"]
        # The preview is text drawn into a pre, never markup parsed.
        assert 'byId("approval-preview").textContent' in page["js"]
        # And a question asked while the tab was away is picked up, because the
        # turn is still waiting on it.
        assert "function resumeApprovals" in page["js"]
        assert '"/api/approvals"' in page["js"]

    def test_the_editor_writes_the_real_file(self, page: dict[str, str]) -> None:
        assert 'data-view="files"' in page["html"]
        assert 'id="view-files"' in page["html"]
        for identifier in ("file-list", "crumbs", "editor", "editor-text", "editor-save"):
            assert f'id="{identifier}"' in page["html"], identifier
        # Built as template literals, so the query is part of a longer string.
        assert "/api/files?path=" in page["js"]
        assert "/api/file?path=" in page["js"]
        assert 'method: "POST"' in page["js"]
        # What makes it an edit of the file on this machine and not a copy in
        # the browser: the save carries the mtime it read, so a change that
        # landed underneath is refused rather than replaced.
        assert "base_modified" in page["js"]
        assert "error.status === 409" in page["js"]
        # Ctrl+S saves, which is the only shortcut anybody looks for in an
        # editor and the one that is missing in every web tool that forgot it.
        assert 'event.key.toLowerCase() === "s"' in page["js"]
        # And the reader is told an edit can be undone, since the checkpoint is
        # taken on the server where they cannot see it happen.
        assert "jaigent undo" in page["js"]

    def test_a_short_message_about_a_save_is_not_an_error(self, page: dict[str, str]) -> None:
        assert 'id="toast"' in page["html"]
        assert "function toast" in page["js"]
        assert ".toast" in page["css"]

    def test_the_movement_stops_for_somebody_who_asked_it_to(self, page: dict[str, str]) -> None:
        reduced = page["css"].split("@media (prefers-reduced-motion: reduce)")[1]
        # Every new animation has to leave its meaning behind when the movement
        # goes: a spinner that stops spinning is a dot, so it becomes one.
        assert ".tool-line-running::before" in reduced
        assert "animation: none" in reduced
        assert ".focus-caret" in reduced
        assert ".approval-dot" in reduced
