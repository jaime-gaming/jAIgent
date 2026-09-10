"""Regression tests for the 0.5.5 bug-fix batch.

Each class pins one fixed bug: the test fails on the old code and passes on
the new. They live in one file because they were found and fixed in one sweep;
see CHANGELOG.md for the matching entries.
"""

from __future__ import annotations

import argparse
import json
import types
from pathlib import Path
from typing import Any

import httpx
import pytest

from jaigent import schedule
from jaigent import session as sessions
from jaigent.agent import Agent
from jaigent.approval import preview
from jaigent.checkpoint import Checkpoint, CheckpointStore, FileState
from jaigent.commands import create_command
from jaigent.config import Settings
from jaigent.errors import ToolError
from jaigent.llm.anthropic import AnthropicProvider
from jaigent.llm.base import AssistantMessage, normalize_messages, text_content
from jaigent.llm.gemini import GeminiProvider
from jaigent.llm.openai import OpenAIProvider
from jaigent.mcp import MCPServer
from jaigent.memory import load_memory
from jaigent.session import Session
from jaigent.settings_store import read as read_settings_file
from jaigent.settings_store import set_value, unset_value
from jaigent.skills import create_skill
from jaigent.tools import Tool, ToolRegistry
from jaigent.tools.ask import build_ask_tools
from jaigent.tools.files import edit_file, list_files, search_files
from jaigent.tools.sandbox import is_secret_path
from jaigent.tools.shell import run_command
from jaigent.tools.web import fetch_page, search_tavily
from jaigent.updater import _is_jaigent_project

# ----------------------------------------------------------------------
# Cross-provider message normalisation
# ----------------------------------------------------------------------


OPENAI_HISTORY = [
    {"role": "system", "content": "be brief"},
    {"role": "user", "content": "list files"},
    {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": "call_1",
                "type": "function",
                "function": {"name": "list_files", "arguments": '{"path": "."}'},
            }
        ],
    },
    {"role": "tool", "tool_call_id": "call_1", "name": "list_files", "content": "a.py"},
]

ANTHROPIC_HISTORY = [
    {"role": "user", "content": "list files"},
    {
        "role": "assistant",
        "content": [
            {"type": "text", "text": "on it"},
            {
                "type": "tool_use",
                "id": "toolu_1",
                "name": "list_files",
                "input": {"path": "."},
            },
        ],
    },
    {
        "role": "user",
        "content": [{"type": "tool_result", "tool_use_id": "toolu_1", "content": "a.py"}],
    },
]


def _capture_post(monkeypatch: pytest.MonkeyPatch, module: Any, payload: dict) -> list[dict]:
    """Capture the provider's outgoing JSON, answering with ``payload``."""
    sent: list[dict] = []

    class FakeClient:
        def __init__(self, **kwargs: Any) -> None:
            pass

        def __enter__(self) -> FakeClient:
            return self

        def __exit__(self, *args: Any) -> None:
            return None

        def post(self, url: str, json: dict | None = None, **kwargs: Any):  # noqa: A002, ANN001, ANN202
            sent.append({"url": url, "json": json})
            return httpx.Response(
                200, json=payload, request=httpx.Request("POST", "https://api.test/x")
            )

    monkeypatch.setattr(module.httpx, "Client", FakeClient)
    return sent


class TestNormalizeMessages:
    def test_openai_tool_history_becomes_canonical(self) -> None:
        assert normalize_messages(OPENAI_HISTORY) == [
            {"role": "system", "content": "be brief"},
            {"role": "user", "content": "list files"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call_1",
                        "name": "list_files",
                        "arguments": {"path": "."},
                        "raw": '{"path": "."}',
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "call_1",
                "name": "list_files",
                "content": "a.py",
            },
        ]

    def test_anthropic_blocks_become_canonical(self) -> None:
        assert normalize_messages(ANTHROPIC_HISTORY) == [
            {"role": "user", "content": "list files"},
            {
                "role": "assistant",
                "content": "on it",
                "tool_calls": [
                    {
                        "id": "toolu_1",
                        "name": "list_files",
                        "arguments": {"path": "."},
                        "raw": None,
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "toolu_1",
                "name": "list_files",
                "content": "a.py",
            },
        ]

    def test_gemini_positional_results_pair_with_calls(self) -> None:
        history = [
            {
                "role": "assistant",
                "content": "x",
                "tool_calls": [
                    {"function": {"name": "a", "arguments": {}}},
                    {"function": {"name": "b", "arguments": {}}},
                ],
            },
            {"role": "tool", "name": "a", "content": "out-a"},
            {"role": "tool", "name": "b", "content": "out-b"},
        ]

        normalised = normalize_messages(history)

        calls = normalised[0]["tool_calls"]
        assert [t["tool_call_id"] for t in normalised[1:]] == [c["id"] for c in calls]
        assert [t["name"] for t in normalised[1:]] == ["a", "b"]

    def test_unparseable_arguments_become_empty_dict(self) -> None:
        history = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [{"id": "c1", "function": {"name": "x", "arguments": "{oops"}}],
            }
        ]

        (assistant,) = normalize_messages(history)

        assert assistant["tool_calls"] == [
            {"id": "c1", "name": "x", "arguments": {}, "raw": "{oops"}
        ]

    def test_non_dict_entries_are_dropped_and_input_untouched(self) -> None:
        history: list[Any] = [{"role": "user", "content": "hi"}, "junk", None]
        snapshot = [dict(m) if isinstance(m, dict) else m for m in history]

        assert normalize_messages(history) == [{"role": "user", "content": "hi"}]
        assert history == snapshot


class TestCrossProviderRoundTrips:
    def test_openai_history_sends_to_anthropic(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import jaigent.llm.anthropic as mod

        sent = _capture_post(
            monkeypatch,
            mod,
            {"content": [{"type": "text", "text": "ok"}]},
        )
        AnthropicProvider(api_key="k", model="m", base_url="https://api.test").complete(
            OPENAI_HISTORY
        )

        assert sent[0]["json"]["system"] == "be brief"
        assert sent[0]["json"]["messages"] == [
            {"role": "user", "content": "list files"},
            {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "call_1",
                        "name": "list_files",
                        "input": {"path": "."},
                    }
                ],
            },
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "call_1", "content": "a.py"}],
            },
        ]

    def test_anthropic_history_sends_to_openai(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import jaigent.llm.openai as mod

        sent = _capture_post(monkeypatch, mod, {"choices": [{"message": {"content": "ok"}}]})
        OpenAIProvider(api_key="k", model="m", base_url="https://api.test").complete(
            ANTHROPIC_HISTORY
        )

        assert sent[0]["json"]["messages"] == [
            {"role": "user", "content": "list files"},
            {
                "role": "assistant",
                "content": "on it",
                "tool_calls": [
                    {
                        "id": "toolu_1",
                        "type": "function",
                        "function": {
                            "name": "list_files",
                            "arguments": json.dumps({"path": "."}),
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "tool_call_id": "toolu_1",
                "name": "list_files",
                "content": "a.py",
            },
        ]

    def test_openai_history_sends_to_gemini(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import jaigent.llm.gemini as mod

        captured: dict[str, Any] = {}

        def handler(request: httpx.Request) -> httpx.Response:
            captured["body"] = json.loads(request.read().decode())
            return httpx.Response(
                200,
                json={"candidates": [{"content": {"parts": [{"text": "ok"}]}}]},
            )

        class FakeClient(httpx.Client):
            def __init__(self, **kwargs: Any) -> None:
                super().__init__(transport=httpx.MockTransport(handler))

        monkeypatch.setattr(mod.httpx, "Client", FakeClient)
        GeminiProvider(api_key="k", model="m", base_url="https://api.test").complete(OPENAI_HISTORY)

        assert captured["body"]["contents"] == [
            {"role": "user", "parts": [{"text": "list files"}]},
            {
                "role": "model",
                "parts": [{"functionCall": {"name": "list_files", "args": {"path": "."}}}],
            },
            {
                "role": "user",
                "parts": [
                    {
                        "functionResponse": {
                            "name": "list_files",
                            "response": {"result": "a.py"},
                        }
                    }
                ],
            },
        ]

    def test_native_openai_arguments_resend_byte_identical(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import jaigent.llm.openai as mod

        sent = _capture_post(monkeypatch, mod, {"choices": [{"message": {"content": "ok"}}]})
        OpenAIProvider(api_key="k", model="m", base_url="https://api.test").complete(OPENAI_HISTORY)

        rendered = sent[0]["json"]["messages"]
        assert rendered == OPENAI_HISTORY


class TestProviderNullGuards:
    def test_openai_null_choices_is_a_provider_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import jaigent.llm.openai as mod
        from jaigent.errors import ProviderError

        _capture_post(monkeypatch, mod, {"choices": None})
        provider = OpenAIProvider(api_key="k", model="m", base_url="https://api.test")

        with pytest.raises(ProviderError):
            provider.complete([{"role": "user", "content": "hi"}])

    def test_openai_null_message_content_is_empty_not_crash(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import jaigent.llm.openai as mod

        _capture_post(
            monkeypatch,
            mod,
            {"choices": [{"message": {"role": "assistant", "content": None}}]},
        )
        reply = OpenAIProvider(api_key="k", model="m", base_url="https://api.test").complete(
            [{"role": "user", "content": "hi"}]
        )

        assert reply.content == ""
        assert reply.wants_tools is False

    def test_anthropic_empty_reply_stays_valid_history(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import jaigent.llm.anthropic as mod

        _capture_post(monkeypatch, mod, {"content": []})
        provider = AnthropicProvider(api_key="k", model="m", base_url="https://api.test")
        reply = provider.complete([{"role": "user", "content": "hi"}])

        assert reply.content == ""
        formatted = provider.format_assistant_message(reply)
        assert formatted["content"] == [{"type": "text", "text": "(empty reply)"}]

    def test_gemini_empty_reply_stays_valid_history(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import jaigent.llm.gemini as mod

        _capture_post(monkeypatch, mod, {"candidates": [{"content": {"parts": []}}]})
        provider = GeminiProvider(api_key="k", model="m", base_url="https://api.test")
        reply = provider.complete([{"role": "user", "content": "hi"}])

        assert reply.content == ""
        contents, _ = provider._to_contents([provider.format_assistant_message(reply)])
        assert contents == [{"role": "model", "parts": [{"text": "(empty reply)"}]}]

    def test_anthropic_skips_a_blank_system_prompt(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import jaigent.llm.anthropic as mod

        sent = _capture_post(monkeypatch, mod, {"content": [{"type": "text", "text": "ok"}]})
        AnthropicProvider(api_key="k", model="m", base_url="https://api.test").complete(
            [{"role": "system", "content": ""}, {"role": "user", "content": "hi"}]
        )

        assert "system" not in sent[0]["json"]


# ----------------------------------------------------------------------
# Agent
# ----------------------------------------------------------------------


class TestAgentGuards:
    def test_max_steps_zero_means_zero_not_default(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from conftest import FakeProvider

        provider = FakeProvider([AssistantMessage(content="done")])
        monkeypatch.setattr("jaigent.agent.get_provider", lambda *a, **k: provider)
        agent = Agent(settings, tools=ToolRegistry(), provider=provider)

        result = agent.run("hi", max_steps=0)

        # The loop body never runs: one final-answer call carrying the
        # "0 tool steps" note, instead of a full-budget run.
        assert len(provider.calls) == 1
        assert "0 tool steps" in json.dumps(provider.calls[0])
        assert result.output == "done"

    def test_empty_system_prompt_means_no_system_prompt(
        self, settings: Settings, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from conftest import FakeProvider

        provider = FakeProvider([AssistantMessage(content="done")])
        agent = Agent(settings, tools=ToolRegistry(), provider=provider, system_prompt="")

        assert agent.system_prompt == ""
        agent.run("hi")

        assert provider.calls[0][0] == {"role": "system", "content": ""}


# ----------------------------------------------------------------------
# CLI
# ----------------------------------------------------------------------


class TestRunTaskSummary:
    def test_whitespace_only_output_does_not_crash(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import jaigent.cli as cli

        task = schedule.Task(id="t1", prompt="say hi", interval="hourly")
        monkeypatch.setattr(cli.schedule, "update", lambda task: None)

        class FakeAgent:
            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

            def run(self, prompt: str) -> Any:
                return types.SimpleNamespace(
                    output="   \n  ",
                    cost=types.SimpleNamespace(total_tokens=0, summary=lambda: ""),
                )

        monkeypatch.setattr(cli, "Agent", FakeAgent)
        monkeypatch.setattr(cli, "build_default_registry", lambda *a, **k: ToolRegistry())
        args = argparse.Namespace(workspace=str(tmp_path), no_cost=True)

        assert cli.run_task(task, args) is True


class TestInitValidation:
    def _answers(self, monkeypatch: pytest.MonkeyPatch, replies: list[str]) -> None:
        answers = iter(replies)
        monkeypatch.setattr("jaigent.cli.console.input", lambda *a, **k: next(answers))

        class Reply:
            output = "ready"
            cost = types.SimpleNamespace(usd=None, format_usd=lambda: "$0")

        fake_agent = types.SimpleNamespace(run=lambda prompt: Reply())
        monkeypatch.setattr("jaigent.cli.Agent", lambda *a, **k: fake_agent)

    def test_provider_zero_falls_back_to_first(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import jaigent.cli as cli

        monkeypatch.chdir(tmp_path)
        self._answers(monkeypatch, ["0", "sk-x", "", ""])

        assert cli.main(["init"]) == 0

        body = (tmp_path / ".env").read_text(encoding="utf-8")
        assert f"JAIGENT_PROVIDER={cli.KNOWN_PROVIDERS[0]}" in body

    def test_eof_instead_of_traceback(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import jaigent.cli as cli

        monkeypatch.chdir(tmp_path)

        def boom(*args: Any, **kwargs: Any) -> str:
            raise EOFError

        monkeypatch.setattr("jaigent.cli.console.input", boom)

        assert cli.main(["init"]) == 1


class TestChatPromptBackslash:
    def test_double_backslash_sends_one(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import jaigent.cli as cli

        replies = iter(["abc\\\\"])
        monkeypatch.setattr("jaigent.cli.console.input", lambda *a, **k: next(replies))

        assert cli._read_chat_prompt() == "abc\\"

    def test_triple_backslash_continues_the_line(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import jaigent.cli as cli

        replies = iter(["abc\\\\\\", "def"])
        monkeypatch.setattr("jaigent.cli.console.input", lambda *a, **k: next(replies))

        assert cli._read_chat_prompt() == "abc\\\ndef"

    def test_single_backslash_still_continues(self, monkeypatch: pytest.MonkeyPatch) -> None:
        import jaigent.cli as cli

        replies = iter(["abc\\", "def"])
        monkeypatch.setattr("jaigent.cli.console.input", lambda *a, **k: next(replies))

        assert cli._read_chat_prompt() == "abc\ndef"


# ----------------------------------------------------------------------
# Approval preview
# ----------------------------------------------------------------------


class TestApprovalPreview:
    def test_garbage_count_does_not_crash_the_preview(self, tmp_path: Path) -> None:
        (tmp_path / "f.txt").write_text("aaa", encoding="utf-8")

        preview(
            "edit_file",
            {"path": "f.txt", "old_text": "a", "new_text": "b", "count": "many"},
            tmp_path,
        )


# ----------------------------------------------------------------------
# File tools
# ----------------------------------------------------------------------


class TestEditFileCount:
    def test_zero_and_negative_counts_are_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "f.txt").write_text("aaa", encoding="utf-8")

        with pytest.raises(ToolError, match="count must be"):
            edit_file(tmp_path, "f.txt", "a", "b", count=0)
        with pytest.raises(ToolError, match="count must be"):
            edit_file(tmp_path, "f.txt", "a", "b", count=-2)

    def test_a_numeric_string_count_is_accepted(self, tmp_path: Path) -> None:
        (tmp_path / "f.txt").write_text("aaa", encoding="utf-8")

        edit_file(tmp_path, "f.txt", "a", "b", count="2")  # type: ignore[arg-type]

        assert (tmp_path / "f.txt").read_text(encoding="utf-8") == "bba"


class TestListFilesDotfiles:
    def test_non_recursive_listing_includes_dotfiles(self, tmp_path: Path) -> None:
        (tmp_path / ".hidden").write_text("x", encoding="utf-8")
        (tmp_path / "visible.txt").write_text("x", encoding="utf-8")

        out = list_files(tmp_path, path=".", recursive=False)

        assert ".hidden" in out
        assert "visible.txt" in out


class TestSearchFilesGuards:
    def test_zero_max_results_is_rejected(self, tmp_path: Path) -> None:
        (tmp_path / "f.txt").write_text("hello", encoding="utf-8")

        with pytest.raises(ToolError, match="max_results"):
            search_files(tmp_path, "hello", max_results=0)


# ----------------------------------------------------------------------
# Registry and MCP error reporting
# ----------------------------------------------------------------------


class TestCallDetailed:
    def test_success_reports_not_failed(self) -> None:
        registry = ToolRegistry()
        registry.register(Tool(name="ok", description="ok", parameters={}, func=lambda: "fine"))

        output, failed = registry.call_detailed("ok", {})

        assert (output, failed) == ("fine", False)

    def test_unknown_tool_reports_failed(self) -> None:
        output, failed = ToolRegistry().call_detailed("nope", {})

        assert failed is True
        assert output.startswith("ERROR:")

    def test_error_prefixed_output_is_not_a_failure(self) -> None:
        registry = ToolRegistry()
        registry.register(
            Tool(name="ok", description="ok", parameters={}, func=lambda: "ERROR: not found")
        )

        output, failed = registry.call_detailed("ok", {})

        assert (output, failed) == ("ERROR: not found", False)

    def test_unserialisable_results_do_not_crash(self) -> None:
        registry = ToolRegistry()
        registry.register(Tool(name="s", description="s", parameters={}, func=lambda: {1, 2}))
        circular: list[Any] = []
        circular.append(circular)
        registry.register(Tool(name="c", description="c", parameters={}, func=lambda: circular))

        out_set, failed_set = registry.call_detailed("s", {})
        out_circ, failed_circ = registry.call_detailed("c", {})

        assert failed_set is False
        assert "1" in out_set
        assert failed_circ is False
        assert "[...]" in out_circ

    def test_call_keeps_its_old_contract(self) -> None:
        registry = ToolRegistry()
        registry.register(Tool(name="ok", description="ok", parameters={}, func=lambda: "fine"))

        assert registry.call("ok", {}) == "fine"
        assert registry.call("nope", {}).startswith("ERROR:")


class TestMcpIsError:
    def test_legit_error_prefixed_output_is_not_flagged(self, settings: Settings) -> None:
        server = MCPServer(settings)
        tool = Tool(
            name="grepish",
            description="g",
            parameters={},
            func=lambda: "ERROR: 0 matches",
        )
        server.registry.register(tool)
        server._tool_map["grepish"] = tool
        line = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "grepish", "arguments": {}},
            }
        )

        response = json.loads(server._handle_line(line) or "{}")

        assert response["result"]["content"][0]["text"] == "ERROR: 0 matches"
        assert "isError" not in response["result"]

    def test_real_failures_are_still_flagged(self, settings: Settings) -> None:
        def boom() -> str:
            raise ToolError("it broke")

        server = MCPServer(settings)
        tool = Tool(name="boom", description="b", parameters={}, func=boom)
        server.registry.register(tool)
        server._tool_map["boom"] = tool
        line = json.dumps(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {"name": "boom", "arguments": {}},
            }
        )

        response = json.loads(server._handle_line(line) or "{}")

        assert response["result"]["isError"] is True


# ----------------------------------------------------------------------
# Sandbox, memory, ask, shell
# ----------------------------------------------------------------------


class TestSecretPaths:
    @pytest.mark.parametrize("name", ["id_list.csv", "id_lookup.json"])
    def test_id_prefix_alone_is_not_a_key(self, name: str) -> None:
        assert is_secret_path(Path(f"data/{name}")) is False

    @pytest.mark.parametrize("name", ["id_rsa", "id_rsa_work", "id_ed25519_backup", ".envrc"])
    def test_keys_and_envrc_stay_blocked(self, name: str) -> None:
        assert is_secret_path(Path(f"data/{name}")) is True


class TestMemoryDecode:
    def test_undecodable_memory_file_yields_nothing(self, tmp_path: Path) -> None:
        (tmp_path / "MEMORY.md").write_bytes(b"\xff\xfe not utf-8 \x80")

        assert load_memory(tmp_path) == ""


class TestAskOptions:
    def test_a_bare_string_is_refused(self) -> None:
        (tool,) = build_ask_tools(interactive=False, input_fn=lambda prompt: "x")

        with pytest.raises(ToolError, match="must be a list"):
            tool.func("pick?", options="abc")


class TestShellGuards:
    def test_non_numeric_timeout_is_a_tool_error(self, tmp_path: Path) -> None:
        with pytest.raises(ToolError, match="timeout must be"):
            run_command(tmp_path, "echo hi", timeout="soon")  # type: ignore[arg-type]

    def test_invalid_utf8_output_is_replaced_not_crashed(self, tmp_path: Path) -> None:
        # 0x81: invalid UTF-8, and also undefined in Windows-1252, so the
        # replacement happens under both decodings (0x80 is a valid euro there).
        out = run_command(tmp_path, "printf 'hi\\201'")

        assert "hi�" in out


# ----------------------------------------------------------------------
# Web tools
# ----------------------------------------------------------------------


def _patch_web_client(monkeypatch: pytest.MonkeyPatch, handler) -> None:  # noqa: ANN001
    import jaigent.tools.web as web_module

    def factory(timeout: float = 30.0) -> httpx.Client:
        return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)

    monkeypatch.setattr(web_module, "_new_client", factory)


class TestTavilyParsing:
    def test_non_dict_payload_is_a_tool_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_web_client(monkeypatch, lambda request: httpx.Response(200, json=[1, 2, 3]))

        with pytest.raises(ToolError, match="not a JSON object"):
            search_tavily("x", api_key="k")

    def test_non_list_results_is_a_tool_error(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_web_client(monkeypatch, lambda request: httpx.Response(200, json={"results": {}}))

        with pytest.raises(ToolError, match="no results list"):
            search_tavily("x", api_key="k")

    def test_non_dict_items_are_skipped(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_web_client(
            monkeypatch,
            lambda request: httpx.Response(
                200,
                json={"results": ["junk", {"title": "t", "url": "u", "content": "c"}]},
            ),
        )

        (result,) = search_tavily("x", api_key="k")

        assert result.title == "t"


class TestFetchPageGuards:
    def test_oversize_body_is_refused(self, monkeypatch: pytest.MonkeyPatch) -> None:
        _patch_web_client(
            monkeypatch,
            lambda request: httpx.Response(
                200,
                headers={"content-type": "text/html", "content-length": "99999999"},
                text="too big",
            ),
        )

        with pytest.raises(ToolError, match="larger than"):
            fetch_page("https://example.com/huge")

    def test_bad_max_chars_fails_before_fetching(self, monkeypatch: pytest.MonkeyPatch) -> None:
        called = False

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal called
            called = True
            return httpx.Response(200, text="x")

        _patch_web_client(monkeypatch, handler)

        with pytest.raises(ToolError, match="max_chars must be"):
            fetch_page("https://example.com/", max_chars="lots")  # type: ignore[arg-type]

        assert called is False


# ----------------------------------------------------------------------
# Skills and commands
# ----------------------------------------------------------------------


class TestCreateOverwrite:
    def test_recreating_a_skill_is_refused(self, tmp_path: Path) -> None:
        create_skill("mine", "does things", "body", scope="project", start=tmp_path)

        with pytest.raises(ToolError, match="already exists"):
            create_skill("mine", "other", "other", scope="project", start=tmp_path)

    def test_recreating_a_command_is_refused(self, tmp_path: Path) -> None:
        create_command("mine", "does things", "body", scope="project", start=tmp_path)

        with pytest.raises(ToolError, match="already exists"):
            create_command("mine", "other", "other", scope="project", start=tmp_path)

    def test_newlines_cannot_inject_front_matter(self, tmp_path: Path) -> None:
        skill = create_skill(
            "mine", "line one\nname: forged", "body", scope="project", start=tmp_path
        )
        command = create_command(
            "mine", "line one\nname: forged", "body", scope="project", start=tmp_path
        )

        for path in (skill, command):
            # One front-matter line, not an injected second key: exactly one
            # line may start with "name:".
            names = [
                line
                for line in path.read_text(encoding="utf-8").splitlines()
                if line.startswith("name:")
            ]
            assert names == ["name: mine"]


# ----------------------------------------------------------------------
# Gateway keys
# ----------------------------------------------------------------------


class TestGatewayKeys:
    @pytest.fixture(autouse=True)
    def _isolated_keys(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        path = tmp_path / "keys.json"
        monkeypatch.setenv("JAIGENT_KEYS_FILE", str(path))
        return path

    def test_revoke_with_an_empty_identifier_revokes_nothing(self) -> None:
        from jaigent.gateway import create_key, load_keys, revoke_key

        create_key("one")

        assert revoke_key("") is None
        assert load_keys()[0].revoked is False

    def test_non_dict_store_yields_nothing(self, _isolated_keys: Path) -> None:
        from jaigent.gateway import load_keys

        _isolated_keys.write_text('["not", "a", "store"]', encoding="utf-8")

        assert load_keys() == []


# ----------------------------------------------------------------------
# Schedule
# ----------------------------------------------------------------------


class TestScheduleGuards:
    @pytest.fixture(autouse=True)
    def _isolated_store(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
        path = tmp_path / "schedules.json"
        monkeypatch.setenv("JAIGENT_SCHEDULE_FILE", str(path))
        return path

    def test_get_with_an_empty_id_finds_nothing(self) -> None:
        schedule.add("do things", "hourly")

        assert schedule.get("") is None

    def test_reschedule_honours_an_explicit_now(self) -> None:
        task = schedule.add("do things", "hourly")

        task.reschedule(now=0.0)

        assert task.next_run == schedule.next_occurrence("hourly", after=0.0)


# ----------------------------------------------------------------------
# Checkpoints
# ----------------------------------------------------------------------


class TestCheckpointGuards:
    def test_non_string_digest_is_coerced(self) -> None:
        state = FileState.from_dict({"path": "f.txt", "digest": 12345, "size": 3, "skipped": False})

        assert state.digest == "12345"

    def test_get_with_an_empty_id_finds_nothing(self, tmp_path: Path) -> None:
        store = CheckpointStore(tmp_path)

        assert store.get("") is None

    def test_restore_cannot_escape_the_workspace(self, tmp_path: Path) -> None:
        store = CheckpointStore(tmp_path)
        outside = tmp_path / "outside.txt"
        outside.write_text("precious", encoding="utf-8")
        checkpoint = Checkpoint(
            id="c1",
            label="evil",
            files=[FileState(path="../outside.txt", digest=None)],
        )

        changed = store.restore(checkpoint)

        assert changed == []
        assert outside.read_text(encoding="utf-8") == "precious"


# ----------------------------------------------------------------------
# Sessions
# ----------------------------------------------------------------------


class TestSessionFixes:
    @pytest.fixture(autouse=True)
    def _isolated_sessions(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(sessions, "session_dir", lambda: tmp_path)

    def test_tool_results_do_not_count_as_turns(self) -> None:
        session = Session(id="s1", title="", provider="", model="", workspace="")
        session.messages = [
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": [{"type": "tool_use", "id": "t", "name": "x", "input": {}}],
            },
            {
                "role": "user",
                "content": [{"type": "tool_result", "tool_use_id": "t"}],
            },
        ]

        assert session.turns == 1

    def test_garbage_usage_values_are_dropped(self) -> None:
        session = Session.from_dict(
            {
                "id": "s1",
                "messages": [],
                "usage": {"total_tokens": "many", "input_tokens": "12"},
            }
        )

        assert session.usage == {"input_tokens": 12}

    def test_transcript_renders_block_content(self) -> None:
        session = Session(id="s1", title="", provider="", model="", workspace="")
        session.messages = [
            {"role": "user", "content": "hi"},
            {
                "role": "assistant",
                "content": [{"type": "text", "text": "hello"}],
                "tool_calls": [],
            },
        ]

        assert session.transcript() == [("user", "hi"), ("assistant", "hello")]


# ----------------------------------------------------------------------
# Settings store
# ----------------------------------------------------------------------


class TestSettingsStoreLenient:
    @pytest.fixture
    def _stores(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> tuple[Path, Path]:
        from jaigent.settings_store import project_settings_path, user_settings_path

        home = tmp_path / "home"
        project = tmp_path / "project"
        project.mkdir()
        monkeypatch.setenv("JAIGENT_HOME", str(home))
        monkeypatch.chdir(project)
        return user_settings_path(), project_settings_path()

    def test_lenient_read_skips_uncoercible_values(self, _stores: tuple[Path, Path]) -> None:
        user_path, _ = _stores
        user_path.parent.mkdir(parents=True, exist_ok=True)
        user_path.write_text(json.dumps({"model": "x", "temperature": "boiling"}), encoding="utf-8")

        assert read_settings_file(user_path, strict=False) == {"model": "x"}

    def test_set_heals_a_leniently_read_file(self, _stores: tuple[Path, Path]) -> None:
        user_path, _ = _stores
        user_path.parent.mkdir(parents=True, exist_ok=True)
        user_path.write_text(json.dumps({"model": "x", "temperature": "boiling"}), encoding="utf-8")

        set_value("model", "y", scope="user")

        assert json.loads(user_path.read_text(encoding="utf-8")) == {"model": "y"}

    def test_unset_removes_a_raw_form_key(self, _stores: tuple[Path, Path]) -> None:
        _, project_path = _stores
        project_path.parent.mkdir(parents=True, exist_ok=True)
        project_path.write_text(json.dumps({"temperature": "boiling"}), encoding="utf-8")

        assert unset_value("temperature", scope="project") is True
        assert json.loads(project_path.read_text(encoding="utf-8")) == {}


# ----------------------------------------------------------------------
# Updater
# ----------------------------------------------------------------------


class TestUpdaterProjectGate:
    def test_jaigent_checkout_is_recognised(self, tmp_path: Path) -> None:
        package = tmp_path / "src" / "jaigent"
        package.mkdir(parents=True)
        (package / "__init__.py").write_text("", encoding="utf-8")
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "x"\n', encoding="utf-8")

        assert _is_jaigent_project(tmp_path) is True

    def test_a_user_project_is_not(self, tmp_path: Path) -> None:
        (tmp_path / "pyproject.toml").write_text('[project]\nname = "my-app"\n', encoding="utf-8")
        (tmp_path / ".git").mkdir()

        assert _is_jaigent_project(tmp_path) is False


# ----------------------------------------------------------------------
# text_content
# ----------------------------------------------------------------------


class TestTextContent:
    def test_blocks_concatenate_and_other_types_coerce(self) -> None:
        assert text_content([{"type": "text", "text": "a"}, {"type": "x"}]) == "a"
        assert text_content(None) == ""
        assert text_content(42) == "42"
        assert text_content({"type": "tool_result", "content": "out"}) == "out"
