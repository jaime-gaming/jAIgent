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


class TestThePicker:
    """With options and scripted keys, the question becomes an arrow-key pick."""

    def ask(self, **kwargs):  # noqa: ANN003, ANN202
        kwargs.setdefault("console", Console(file=io.StringIO()))
        kwargs.setdefault("interactive", True)
        kwargs.setdefault("keys", ["down", "enter"])
        return build_ask_tools(**kwargs)[0].func

    def test_arrows_and_enter_pick_an_option(self) -> None:
        ask = self.ask(keys=["down", "enter"])

        assert ask("Which colour?", ["red", "blue"]) == "The user answered: blue"

    def test_a_digit_picks_directly(self) -> None:
        ask = self.ask(keys=["2"])

        assert ask("Which colour?", ["red", "blue"]) == "The user answered: blue"

    def test_the_answer_leaves_a_one_line_summary_behind(self) -> None:
        console = Console(width=80, record=True)
        ask = self.ask(console=console, keys=["enter"])

        ask("Which colour?", ["red", "blue"])

        screen = console.export_text()
        assert "Which colour?" in screen
        assert "red" in screen

    def test_esc_switches_to_a_typed_answer(self) -> None:
        ask = self.ask(keys=["esc"], input_fn=lambda prompt: "the green one")

        assert ask("Which colour?", ["red", "blue"]) == "The user answered: the green one"

    def test_a_typed_custom_answer_is_summarised_too(self) -> None:
        console = Console(width=80, record=True)
        ask = self.ask(console=console, keys=["esc"], input_fn=lambda prompt: "green")

        ask("Which colour?", ["red", "blue"])

        assert "green" in console.export_text()

    def test_an_empty_custom_answer_means_use_your_judgment(self) -> None:
        ask = self.ask(keys=["esc"], input_fn=lambda prompt: "  ")

        assert "best judgment" in ask("Which colour?", ["red", "blue"])

    def test_a_closed_custom_prompt_means_use_your_judgment(self) -> None:
        def closed(prompt: str) -> str:
            raise EOFError

        ask = self.ask(keys=["esc"], input_fn=closed)

        assert "closed the prompt" in ask("Which colour?", ["red", "blue"])

    def test_cancelling_the_picker_cancels_the_run(self) -> None:
        ask = self.ask(keys=["ctrl-c"])

        with pytest.raises(KeyboardInterrupt):
            ask("Which colour?", ["red", "blue"])

    def test_a_closed_picker_means_use_your_judgment(self) -> None:
        ask = self.ask(keys=["ctrl-d"])

        assert "closed the prompt" in ask("Which colour?", ["red", "blue"])

    def test_scripted_keys_take_precedence_over_the_typed_fallback(self) -> None:
        ask = self.ask(keys=["1"])

        assert ask("Which colour?", ["red", "blue"]) == "The user answered: red"
