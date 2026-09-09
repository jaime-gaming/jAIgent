"""The ask_user tool: a dedicated UI for model questions."""

from __future__ import annotations

import io
import sys

import pytest
from rich.console import Console

from jaigent.errors import ToolError
from jaigent.tools import ToolRegistry, build_ask_tools


def _ask(**kwargs):  # noqa: ANN003, ANN202
    """The tool function with a silent console."""
    kwargs.setdefault("console", Console(file=io.StringIO()))
    return build_ask_tools(**kwargs)[0].func


def test_it_returns_the_users_answer() -> None:
    ask = _ask(input_fn=lambda prompt: "the blue one", interactive=True)

    assert ask("Which colour?", ["red", "blue"]) == "The user answered: the blue one"


def test_a_number_picks_the_option() -> None:
    ask = _ask(input_fn=lambda prompt: "2", interactive=True)

    assert ask("Which colour?", ["red", "blue"]) == "The user answered: blue"


def test_an_out_of_range_number_is_kept_verbatim() -> None:
    ask = _ask(input_fn=lambda prompt: "9", interactive=True)

    assert ask("Which colour?", ["red", "blue"]) == "The user answered: 9"


def test_only_six_options_are_offered() -> None:
    seen: list[str] = []
    ask = _ask(input_fn=lambda prompt: seen.append(prompt) or "1", interactive=True)

    assert ask("Pick one.", [f"option {i}" for i in range(10)]) == "The user answered: option 0"


def test_an_empty_answer_means_use_your_judgment() -> None:
    ask = _ask(input_fn=lambda prompt: "  ", interactive=True)

    assert "best judgment" in ask("Which colour?")


def test_a_closed_prompt_means_use_your_judgment() -> None:
    def closed(prompt: str) -> str:
        raise EOFError

    ask = _ask(input_fn=closed, interactive=True)

    assert "best judgment" in ask("Which colour?")


def test_cancelling_propagates() -> None:
    def cancel(prompt: str) -> str:
        raise KeyboardInterrupt

    ask = _ask(input_fn=cancel, interactive=True)

    with pytest.raises(KeyboardInterrupt):
        ask("Which colour?")


def test_a_missing_question_is_rejected() -> None:
    ask = _ask(input_fn=lambda prompt: "x", interactive=True)

    with pytest.raises(ToolError, match="needs a question"):
        ask("   ")


def test_non_interactive_sessions_get_no_prompt() -> None:
    def fail(prompt: str) -> str:
        pytest.fail("nobody is there to answer")

    ask = _ask(input_fn=fail, interactive=False)

    assert "non-interactive" in ask("Which colour?", ["red", "blue"])


def test_a_pipe_is_detected_as_non_interactive(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
    ask = _ask(input_fn=lambda prompt: pytest.fail("must not prompt on a pipe"))

    assert "non-interactive" in ask("Which colour?")


def test_the_question_renders_as_a_panel() -> None:
    console = Console(width=80, record=True)
    ask = _ask(console=console, input_fn=lambda prompt: "x", interactive=True)

    ask("Which colour?", ["red", "blue"])

    screen = console.export_text()
    assert "jAI has a question" in screen
    assert "Which colour?" in screen
    assert "1. red" in screen
    assert "2. blue" in screen


def test_it_survives_the_registry_error_contract() -> None:
    """Anything raised becomes an ERROR string for the model, never a crash."""
    registry = ToolRegistry()
    registry.extend(build_ask_tools(interactive=False))

    assert registry.call("ask_user", {"question": "x?"}).startswith("The user cannot")
    assert registry.call("ask_user", {"question": "  "}).startswith("ERROR:")
