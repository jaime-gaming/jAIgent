"""The arrow-key option picker: keys, state machine, rendering, scripted picks."""

from __future__ import annotations

import io
import os
import sys

import pytest
from rich.console import Console

from jaigent.mouse import (
    MOUSE_DISABLE,
    MOUSE_ENABLE,
    MouseEvent,
    mouse_key,
    parse_sgr,
    should_enable_mouse,
)
from jaigent.picker import (
    CANCELLED,
    CUSTOM,
    EOF,
    PICKED,
    STAY,
    UNAVAILABLE,
    PickerState,
    _read_key_posix,
    apply_key,
    key_name,
    pick_option,
    picker_available,
    render_picker,
    screen_is_taken,
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

    def test_a_console_that_is_already_rendering_cannot_pick(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """rich draws only the outermost Live, so a panel under one is invisible.

        That was the "cannot answer the agent's question" bug: the picker took
        the keyboard and put the terminal in raw mode, and its panel was never
        rendered because the chat's live region was still the outermost one.
        """
        import sys

        from rich.live import Live
        from rich.text import Text

        from jaigent import picker as picker_module

        # Everything else about this console says "yes": a terminal, a stdin
        # somebody is typing at, a platform with raw keys. The only thing left
        # to refuse is the live region already holding the screen.
        monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
        monkeypatch.setattr(picker_module, "raw_keys_available", lambda: True)
        console = Console(file=io.StringIO(), force_terminal=True, width=60)
        assert picker_available(console) is True

        region = Live(Text("working"), console=console)
        region.start()
        try:
            assert screen_is_taken(console) is True
            assert picker_available(console) is False
            assert pick_option(console, "Which?", ["a", "b"]).kind == UNAVAILABLE
        finally:
            region.stop()

        assert screen_is_taken(console) is False
        assert picker_available(console) is True

    def test_scripted_keys_still_pick_under_a_running_region(self) -> None:
        # The scripted path touches no terminal and renders nothing, so there
        # is no nesting to worry about and it must not be reported as one.
        from rich.live import Live
        from rich.text import Text

        console = Console(file=io.StringIO(), force_terminal=True, width=60)
        region = Live(Text("working"), console=console)
        region.start()
        try:
            result = pick_option(console, "Which?", ["a", "b"], keys=["enter"])
        finally:
            region.stop()

        assert result.kind == PICKED
        assert result.value == "a"


def _read_from(raw: bytes) -> str:
    """Feed bytes to the real key reader through a pipe and read one key back."""
    read_fd, write_fd = os.pipe()
    try:
        os.write(write_fd, raw)
        os.close(write_fd)  # whatever was written stays buffered until read
        return _read_key_posix(read_fd)
    finally:
        os.close(read_fd)


class _Tty:
    def isatty(self) -> bool:
        return True


class TestMouse:
    """A click, a tap and the wheel become keys the picker already knows."""

    def test_the_toggles_are_the_right_private_modes(self) -> None:
        # 1000 for press and release, 1006 for the SGR encoding, and never 1002
        # or 1003: nothing here follows a drag, so motion is left off.
        assert "\x1b[?1000h" in MOUSE_ENABLE and "\x1b[?1006h" in MOUSE_ENABLE
        assert "\x1b[?1000l" in MOUSE_DISABLE and "\x1b[?1006l" in MOUSE_DISABLE
        assert "1002" not in MOUSE_ENABLE and "1003" not in MOUSE_ENABLE

    @pytest.mark.parametrize(
        ("sequence", "button", "x", "y", "press"),
        [
            ("\x1b[<0;12;5M", 0, 12, 5, True),
            ("\x1b[<0;12;5m", 0, 12, 5, False),
            ("\x1b[<64;1;1M", 64, 1, 1, True),
            ("\x1b[<65;80;24M", 65, 80, 24, True),
            ("\x1b[<1;3;9M", 1, 3, 9, True),
        ],
    )
    def test_a_report_decodes(self, sequence, button, x, y, press) -> None:
        assert parse_sgr(sequence) == MouseEvent(button=button, x=x, y=y, press=press)

    @pytest.mark.parametrize(
        "sequence",
        ["", "q", "\x1b[A", "\x1b[<0;5", "\x1b[<0;5;5X", "\x1b[<a;b;cM", "\x1b[<0;5M"],
    )
    def test_anything_that_is_not_a_whole_report_is_nothing(self, sequence) -> None:
        # A partial or malformed report is dropped, never guessed at.
        assert parse_sgr(sequence) is None

    def test_the_wheel_moves_and_a_left_press_confirms(self) -> None:
        assert mouse_key(parse_sgr("\x1b[<64;1;1M")) == "up"
        assert mouse_key(parse_sgr("\x1b[<65;1;1M")) == "down"
        assert mouse_key(parse_sgr("\x1b[<0;1;1M")) == "enter"

    def test_a_release_and_other_buttons_do_nothing(self) -> None:
        assert mouse_key(parse_sgr("\x1b[<0;1;1m")) == ""
        assert mouse_key(parse_sgr("\x1b[<1;1;1M")) == ""
        assert mouse_key(parse_sgr("\x1b[<2;1;1M")) == ""
        assert mouse_key(None) == ""

    def test_the_coordinates_are_ignored_on_purpose(self) -> None:
        # The panel is inline, so its absolute row is not knowable; a click
        # confirms whatever is highlighted rather than guessing which row it hit.
        assert mouse_key(parse_sgr("\x1b[<0;1;1M")) == mouse_key(parse_sgr("\x1b[<0;99;99M"))

    def test_a_tap_confirms_the_highlighted_option(self) -> None:
        state = PickerState(["a", "b", "c"], index=1)
        key = mouse_key(parse_sgr("\x1b[<0;40;7M"))
        assert apply_key(state, key) == PICKED
        assert state.index == 1  # the highlighted one, not the clicked row

    def test_the_wheel_walks_the_selection(self) -> None:
        state = PickerState(["a", "b", "c"], index=0)
        assert apply_key(state, mouse_key(parse_sgr("\x1b[<65;1;1M"))) == STAY
        assert state.index == 1
        assert apply_key(state, mouse_key(parse_sgr("\x1b[<64;1;1M"))) == STAY
        assert state.index == 0

    def test_the_reader_consumes_a_whole_report(self) -> None:
        # The point of the reader change: a report arrives as one key, and its
        # digits never leak out to be read as the next four keystrokes.
        assert _read_from(b"\x1b[<65;12;9M") == "down"
        assert _read_from(b"\x1b[<0;12;9M") == "enter"
        assert _read_from(b"\x1b[<0;12;9m") == ""

    def test_the_reader_still_reads_keys_exactly_as_before(self) -> None:
        assert _read_from(b"\x1b[A") == "up"
        assert _read_from(b"\x1b[B") == "down"
        assert _read_from(b"q") == "q"
        assert _read_from(b"3") == "3"

    def test_a_truncated_report_is_dropped_not_misread(self) -> None:
        assert _read_from(b"\x1b[<0;12") == ""

    def test_mouse_is_off_unless_a_terminal_wants_it(self, monkeypatch) -> None:
        for var in ("JAIGENT_NO_MOUSE", "JAIGENT_NO_ANIMATION", "CI"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.delenv("TERM", raising=False)
        monkeypatch.setattr(sys, "stdin", _Tty())
        assert should_enable_mouse() is True

    @pytest.mark.parametrize(
        "env",
        [
            {"JAIGENT_NO_MOUSE": "1"},
            {"JAIGENT_NO_ANIMATION": "1"},
            {"CI": "true"},
            {"TERM": "dumb"},
        ],
    )
    def test_the_usual_switches_quiet_it(self, monkeypatch, env) -> None:
        for var in ("JAIGENT_NO_MOUSE", "JAIGENT_NO_ANIMATION", "CI", "TERM"):
            monkeypatch.delenv(var, raising=False)
        monkeypatch.setattr(sys, "stdin", _Tty())
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        assert should_enable_mouse() is False

    def test_no_tty_no_mouse(self, monkeypatch) -> None:
        for var in ("JAIGENT_NO_MOUSE", "JAIGENT_NO_ANIMATION", "CI", "TERM"):
            monkeypatch.delenv(var, raising=False)

        class _NotATty:
            def isatty(self) -> bool:
                return False

        monkeypatch.setattr(sys, "stdin", _NotATty())
        assert should_enable_mouse() is False
