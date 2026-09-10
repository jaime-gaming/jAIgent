"""The arrow-key option picker: keys, state machine, rendering, scripted picks."""

from __future__ import annotations

import io

import pytest
from rich.console import Console

from jaigent.picker import (
    CANCELLED,
    CUSTOM,
    EOF,
    PICKED,
    STAY,
    UNAVAILABLE,
    PickerState,
    apply_key,
    key_name,
    pick_option,
    picker_available,
    render_picker,
)


def render(renderable, width: int = 80) -> str:
    console = Console(width=width, no_color=True)
    with console.capture() as capture:
        console.print(renderable)
    return capture.get()


class TestKeyNames:
    def test_control_keys_are_named(self) -> None:
        assert key_name("\r") == "enter"
        assert key_name("\n") == "enter"
        assert key_name("\x1b[A") == "up"
        assert key_name("\x1b[B") == "down"
        assert key_name("\x1b") == "esc"
        assert key_name("\t") == "tab"
        assert key_name("\x7f") == "backspace"
        assert key_name("\x03") == "ctrl-c"
        assert key_name("\x04") == "ctrl-d"

    def test_printable_keys_pass_through(self) -> None:
        assert key_name("q") == "q"
        assert key_name("2") == "2"
        assert key_name(" ") == " "


class TestStateMachine:
    def options(self) -> PickerState:
        return PickerState(["red", "blue", "green"])

    def test_enter_picks_the_current_option(self) -> None:
        assert apply_key(self.options(), "enter") == PICKED

    def test_down_moves_and_wraps(self) -> None:
        state = self.options()
        apply_key(state, "down")
        assert state.index == 1
        apply_key(state, "down")
        apply_key(state, "down")
        assert state.index == 0  # wrapped

    def test_up_moves_backwards_and_wraps(self) -> None:
        state = self.options()
        apply_key(state, "up")
        assert state.index == 2  # wrapped to the end

    def test_vim_keys_move_too(self) -> None:
        state = self.options()
        assert apply_key(state, "j") == STAY
        assert state.index == 1
        apply_key(state, "k")
        assert state.index == 0

    def test_a_digit_jumps_and_picks(self) -> None:
        state = self.options()
        assert apply_key(state, "3") == PICKED
        assert state.index == 2

    def test_an_out_of_range_digit_is_ignored(self) -> None:
        state = self.options()
        assert apply_key(state, "9") == STAY
        assert state.index == 0

    def test_zero_is_not_an_option_number(self) -> None:
        assert apply_key(self.options(), "0") == STAY

    def test_esc_asks_for_a_custom_answer(self) -> None:
        assert apply_key(self.options(), "esc") == CUSTOM
        assert apply_key(self.options(), "tab") == CUSTOM
        assert apply_key(self.options(), "e") == CUSTOM

    def test_ctrl_c_cancels(self) -> None:
        assert apply_key(self.options(), "ctrl-c") == CANCELLED

    def test_ctrl_d_is_eof(self) -> None:
        assert apply_key(self.options(), "ctrl-d") == EOF

    def test_unknown_keys_are_ignored(self) -> None:
        state = self.options()
        assert apply_key(state, "x") == STAY
        assert apply_key(state, "F9") == STAY
        assert state.index == 0

    def test_a_single_option_cannot_move(self) -> None:
        state = PickerState(["only"])
        apply_key(state, "down")
        assert state.index == 0


class TestRendering:
    def test_shows_the_question_options_and_hints(self) -> None:
        out = render(render_picker("Which colour?", ["red", "blue"], 0, unicode_ok=True))

        assert "Which colour?" in out
        assert "red" in out
        assert "blue" in out
        assert "jAI has a question" in out
        assert "move" in out
        assert "enter confirm" in out
        assert "own answer" in out

    def test_number_hint_matches_the_option_count(self) -> None:
        out = render(render_picker("Pick.", ["a", "b", "c"], 0, unicode_ok=True))
        assert "1-3 pick" in out

    def test_a_single_option_needs_no_number_hint(self) -> None:
        out = render(render_picker("Sure?", ["yes"], 0, unicode_ok=True))
        assert "1-1" not in out

    def test_the_selected_row_gets_the_pointer(self) -> None:
        out = render(render_picker("Pick.", ["red", "blue"], 1, unicode_ok=True))

        assert "❯" in out
        assert "● blue" in out
        assert "○ red" in out

    def test_pulse_swaps_the_selected_marker(self) -> None:
        out = render(render_picker("Pick.", ["red"], 0, unicode_ok=True, pulse=True))
        assert "◉" in out

    def test_ascii_fallback_keeps_everything_readable(self) -> None:
        out = render(render_picker("Pick.", ["red", "blue"], 0, unicode_ok=False))

        assert "❯" not in out
        assert "●" not in out
        assert "(*) red" in out
        assert "( ) blue" in out
        assert "^v move" in out


class TestScriptedPicks:
    def pick(self, keys: list[str], options: list[str] | None = None) -> object:
        console = Console(file=io.StringIO())
        choices = options if options is not None else ["red", "blue"]
        return pick_option(console, "Which colour?", choices, keys=keys)

    def test_enter_takes_the_first_option(self) -> None:
        result = self.pick(["enter"])
        assert result.kind == PICKED
        assert result.value == "red"

    def test_arrows_then_enter(self) -> None:
        result = self.pick(["down", "enter"])
        assert result.value == "blue"

    def test_up_wraps_to_the_last_option(self) -> None:
        result = self.pick(["up", "enter"])
        assert result.value == "blue"

    def test_a_digit_picks_directly(self) -> None:
        result = self.pick(["2"])
        assert result.value == "blue"

    def test_esc_wants_a_custom_answer(self) -> None:
        assert self.pick(["esc"]).kind == CUSTOM

    def test_ctrl_c_cancels(self) -> None:
        assert self.pick(["ctrl-c"]).kind == CANCELLED

    def test_ctrl_d_is_eof(self) -> None:
        assert self.pick(["ctrl-d"]).kind == EOF

    def test_exhausted_keys_are_eof(self) -> None:
        assert self.pick([]).kind == EOF

    def test_ignored_keys_do_not_end_the_pick(self) -> None:
        assert self.pick(["x", "F9", "down", "enter"]).value == "blue"

    def test_no_options_is_unavailable(self) -> None:
        assert self.pick(["enter"], options=[]).kind == UNAVAILABLE

    def test_scripted_keys_render_nothing(self) -> None:
        buffer = io.StringIO()
        console = Console(file=buffer, force_terminal=True)
        pick_option(console, "Q?", ["a"], keys=["enter"])
        assert buffer.getvalue() == ""


class TestAvailability:
    def test_a_piped_console_cannot_pick(self) -> None:
        assert picker_available(Console(file=io.StringIO())) is False

    def test_scripted_keys_are_always_available(self) -> None:
        assert picker_available(Console(file=io.StringIO()), keys=["enter"]) is True

    def test_a_terminal_without_a_tty_stdin_cannot_pick(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        import sys

        monkeypatch.setattr(sys.stdin, "isatty", lambda: False)
        assert picker_available(Console(force_terminal=True)) is False
