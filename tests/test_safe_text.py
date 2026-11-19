"""Text from anywhere else, made safe to print.

Every case here is a string that used to be able to either crash the session
or drive the terminal: rich markup that gets interpreted, a bracket that makes
rich raise, and ANSI sequences that a file or a model can contain.
"""

from __future__ import annotations

import io

import pytest
from rich.console import Console

from jaigent.safe_text import clean, escape_markup, one_line, printable, quote


def rendered(text: str) -> str:
    """Print ``text`` the way the CLI does, and return what hit the stream.

    ``highlight=False`` is how every call site does it: rich's highlighter eats
    backslashes, which would undo the escape.
    """
    stream = io.StringIO()
    console = Console(width=80, file=stream, force_terminal=True, color_system="truecolor")
    console.print(text, highlight=False)
    return stream.getvalue()


class TestMarkup:
    def test_a_tag_is_printed_not_obeyed(self) -> None:
        plain = escape_markup("[red]not colour[/red]")

        out = rendered(plain)

        assert "\x1b[31m" not in out, "the tag was obeyed"
        assert out == "[red]not colour[/red]\n", "the tag should read as text"

    def test_a_lone_bracket_does_not_crash(self) -> None:
        # "[notes" is not a valid tag: rich raises MarkupError on it.
        assert escape_markup("see [notes") == "see \\[notes"
        assert rendered(escape_markup("see [notes")) == "see [notes\n"

    def test_a_filename_with_brackets_is_printable(self) -> None:
        text = "read failed: [draft].md"

        assert rendered(escape_markup(text))

    def test_none_is_empty(self) -> None:
        assert escape_markup(None) == ""

    def test_backslashes_are_left_alone(self) -> None:
        # Windows paths keep their separators; only brackets are escaped.
        assert escape_markup(r"C:\Users\a\notes.txt") == r"C:\Users\a\notes.txt"


class TestControlCharacters:
    @pytest.mark.parametrize(
        "raw",
        [
            "\x1b[2J",  # clear the screen
            "\x1b]0;title\x07",  # rewrite the window title
            "\x1b[?25l",  # hide the cursor and never bring it back
            "\x00",
            "\x7f",
        ],
    )
    def test_terminal_commands_are_removed(self, raw: str) -> None:
        assert clean(f"before{raw}after") == "beforeafter"

    def test_a_carriage_return_cannot_overwrite_the_line(self) -> None:
        # "trusted\rmalicious" would print "malicious" over "trusted".
        assert clean("trusted\rmalicious") == "trusted\nmalicious"

    def test_tabs_become_spaces(self) -> None:
        assert clean("a\tb") == "a    b"

    def test_newlines_survive_when_wanted(self) -> None:
        assert clean("one\ntwo") == "one\ntwo"

    def test_newlines_collapse_for_one_line_values(self) -> None:
        assert clean("one\ntwo", keep_newlines=False) == "one two"

    def test_printable_keeps_text_but_drops_escapes(self) -> None:
        # The whole sequence goes, not just the ESC byte: a half-stripped
        # colour code would leave "[31m" printed in the middle of a sentence.
        assert printable("a\x1b[31mb\r\nc") == "ab\nc"

    def test_a_colour_code_leaves_no_debris(self) -> None:
        assert clean("\x1b[1;31mred\x1b[0m") == "red"


class TestLimits:
    def test_a_limit_marks_what_it_cut(self) -> None:
        result = clean("x" * 500, limit=40)

        assert len(result) == 40
        assert result.endswith("…")

    def test_a_short_string_is_untouched(self) -> None:
        assert clean("short", limit=40) == "short"

    def test_one_line_is_a_single_line(self) -> None:
        assert one_line("a\nb\nc") == "a b c"

    def test_quote_wraps_and_escapes(self) -> None:
        assert quote("[x]") == "'\\[x]'"

    def test_one_line_bounds(self) -> None:
        assert len(one_line("y" * 100, limit=20)) == 20
