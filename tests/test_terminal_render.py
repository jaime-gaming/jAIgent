"""What a real terminal actually ends up showing.

The rest of the suite asserts on the strings jaigent writes. That is not the
same thing as what the user sees: escape sequences move a cursor around, and an
off-by-one there leaves the previous output stranded on screen. These tests
feed the output through a terminal emulator and assert on the resulting screen.
"""

from __future__ import annotations

import io

import pytest
from rich.console import Console

from jaigent import cli

pyte = pytest.importorskip("pyte")


def screen_after(
    text: str,
    *,
    width: int = 60,
    height: int = 24,
    chunk: int = 7,
    preamble: str = '$ jaigent "explain this"\n',
    markdown: bool = True,
) -> list[str]:
    """Stream ``text`` chunk by chunk and return the non-blank screen rows."""
    buffer = io.StringIO()
    console = Console(
        width=width,
        height=height,
        file=buffer,
        force_terminal=True,
        color_system="truecolor",
    )
    console.file.write(preamble)

    printer = cli._StreamPrinter(console, markdown=markdown)
    for start in range(0, len(text), chunk):
        printer(text[start : start + chunk])
    printer.finish()

    emulator = pyte.Screen(width, height)
    # A tty in cooked mode turns \n into CR+LF; pyte does not, unless told.
    emulator.set_mode(pyte.modes.LNM)
    pyte.Stream(emulator).feed(buffer.getvalue())
    return [row.rstrip() for row in emulator.display if row.strip()]


class TestStreamedAnswerOnScreen:
    def test_the_raw_markup_does_not_survive(self) -> None:
        rows = screen_after("Here is **bold** text.")

        assert "Here is bold text." in rows
        assert not any("**" in row for row in rows), rows

    def test_the_line_above_is_untouched(self) -> None:
        rows = screen_after("Here is **bold** text.")

        assert rows[0] == '$ jaigent "explain this"'

    def test_the_answer_appears_exactly_once(self) -> None:
        rows = screen_after("Here is **bold** text.")

        assert sum("bold text" in row for row in rows) == 1

    def test_a_list_is_rendered_as_bullets(self) -> None:
        rows = screen_after("Options:\n\n- one\n- two\n")

        assert any("one" in row for row in rows)
        assert not any(row.strip().startswith("- ") for row in rows), rows

    def test_a_code_fence_loses_its_backticks(self) -> None:
        rows = screen_after("Try this:\n\n```python\nx = 1\n```")

        assert any("x = 1" in row for row in rows)
        assert not any("```" in row for row in rows), rows

    def test_a_heading_loses_its_hashes(self) -> None:
        rows = screen_after("# Title\n\nBody text.")

        assert any("Title" in row for row in rows)
        assert not any(row.strip().startswith("#") for row in rows), rows

    def test_a_paragraph_wider_than_the_terminal_leaves_no_debris(self) -> None:
        rows = screen_after("word " * 40, width=40)

        assert not any("**" in row for row in rows)
        # Rendered once: the word count on screen matches the answer, not double.
        assert sum(row.count("word") for row in rows) == 40

    @pytest.mark.parametrize("width", [20, 40, 80, 120])
    def test_no_debris_at_any_width(self, width: int) -> None:
        rows = screen_after("Here is **bold** and `code` text.", width=width)

        assert not any("**" in row for row in rows), rows

    @pytest.mark.parametrize("chunk", [1, 3, 50, 5000])
    def test_no_debris_at_any_chunk_size(self, chunk: int) -> None:
        rows = screen_after("Here is **bold** text.", chunk=chunk)

        assert not any("**" in row for row in rows), rows

    def test_content_taller_than_the_window_is_left_as_streamed(self) -> None:
        # It has already scrolled off; rewinding would erase the wrong rows.
        body = "\n".join(f"- item {i}" for i in range(30))
        rows = screen_after(body, width=40, height=10)

        assert any(row.strip() == "- item 29" for row in rows), rows

    def test_markdown_disabled_keeps_the_raw_text(self) -> None:
        rows = screen_after("Here is **bold** text.", markdown=False)

        assert any("**bold**" in row for row in rows)
