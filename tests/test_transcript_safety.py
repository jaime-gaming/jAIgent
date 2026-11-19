"""What the user typed, and what a model wrote, must not drive the terminal.

Two failure modes, both reachable from ordinary use:

* **Markup.** Rich reads ``[red]`` in any string it prints. A prompt, a tool
  argument or a provider error containing brackets is either obeyed as colour
  or, if it holds half a tag, raises ``MarkupError`` and ends the session.
* **Control characters.** A file's contents or a provider's error can carry
  ``ESC [ 2 J`` (clear the screen) or a carriage return that overwrites the
  line just printed.

These tests drive the real functions and inspect what reached the stream.
"""

from __future__ import annotations

import io
from pathlib import Path

import pytest
from rich.console import Console
from rich.text import Text

import jaigent.cli as cli
from jaigent import approval, ui
from jaigent.composer import (
    PASTE,
    Composer,
    KeyReader,
    LiveComposer,
    apply_key,
    render_composer,
)

ATTACK = "[red]HIJACK[/red] \x1b[2J\x1b]0;title\x07 evil\r\nline two"


class ScriptedReads:
    """A byte stream handed out one chunk per call, like a terminal's."""

    def __init__(self, *chunks: bytes) -> None:
        self.chunks = list(chunks)

    def __call__(self) -> bytes:
        return self.chunks.pop(0) if self.chunks else b""


def terminal(width: int = 100) -> tuple[Console, io.StringIO]:
    stream = io.StringIO()
    console = Console(file=stream, force_terminal=True, color_system="truecolor", width=width)
    return console, stream


# --------------------------------------------------------------- the prompt
class TestThePromptEcho:
    """The ``you`` line is the most-seen path for a typed prompt."""

    @pytest.fixture(autouse=True)
    def _terminal_console(self):
        self.console, self.stream = terminal()
        self.saved = cli.console
        cli.console = self.console
        yield
        cli.console = self.saved

    def test_a_bracket_is_printed_and_not_obeyed(self) -> None:
        cli._echo_prompt("[red]HIJACK[/red]")

        out = self.stream.getvalue()
        assert "\x1b[31mHIJACK\x1b[0m" not in out, "the prompt was rendered as markup"
        assert "[red]HIJACK[/red]" in out, "the prompt should read as typed"

    def test_half_a_tag_does_not_end_the_session(self) -> None:
        # MarkupError, raised from inside a print, takes the whole chat down.
        cli._echo_prompt("see the notes in [my file")

        assert "[my file" in self.stream.getvalue()

    def test_control_characters_do_not_reach_the_terminal(self) -> None:
        cli._echo_prompt(ATTACK)

        out = self.stream.getvalue()
        for sequence in ("\x1b[2J", "\x1b]0;", "\x07", "\r"):
            assert sequence not in out, f"{sequence!r} reached the terminal"
        assert "HIJACK" in out, "the text itself should still be readable"

    def test_a_long_prompt_is_bounded(self) -> None:
        # It still wraps in a narrow terminal, the way any long line does;
        # what it must not do is run to the whole of a pasted document, or
        # carry a newline of its own.
        preview = cli._prompt_preview("word " * 400)

        assert len(preview) <= 400
        assert preview.endswith("…")
        assert "\n" not in preview


# ------------------------------------------------------------ error messages
class TestErrorMessages:
    """Errors quote providers and file systems: text nobody here wrote."""

    def test_an_error_with_brackets_prints_instead_of_raising(self) -> None:
        console, stream = terminal()
        saved = cli.err_console
        cli.err_console = console
        try:
            err_console = cli.err_console
            err_console.print(f"[red]{cli.clean_line('[bold]bad[/bold] request')}[/]")
        finally:
            cli.err_console = saved

        body = stream.getvalue()
        assert "bad" in body and "request" in body
        assert "\x1b[1m" not in body, "the brackets were obeyed as styling"

    def test_a_prompt_preview_is_escaped_not_obeyed(self) -> None:
        # The escape is what the preview *is*: console.print parses the string,
        # and "\\[" is how a literal bracket survives that.
        preview = cli._prompt_preview("[red]/etc[/red]")

        assert preview == "\\[red]/etc\\[/red]"

    def test_an_error_is_one_line(self) -> None:
        assert cli.clean_line("first\nsecond") == "first second"
        assert cli.clean_line("a\x1b[2Jb") == "ab"


# --------------------------------------------------------------- the tools
class TestToolTrace:
    """Tool arguments and outputs are model text in a one-line trace."""

    def test_an_argument_cannot_break_the_line_in_two(self) -> None:
        line = ui.tool_line("read_file", "path=two\nlines\x1b[2J")

        assert "\n" not in line.plain
        assert "\x1b" not in line.plain
        assert "path=two lines" in line.plain

    def test_a_command_in_the_trace_is_flattened(self) -> None:
        line = ui.activity_line("Ran", "ls\nrm -rf /\x07")

        assert "\x07" not in line.plain
        assert "\n" not in line.plain

    def test_a_plan_title_is_one_line(self) -> None:
        lines = ui.plan_lines([{"title": "first second" * 30, "status": "pending"}])

        assert all("\n" not in line.plain for line in lines)
        assert len(lines[1].plain) < 100

    def test_a_tool_result_is_flattened(self) -> None:
        line = ui.result_line("ok\x1b[2J\nnext", ok=True)

        assert "\x1b" not in line.plain and "\n" not in line.plain


# ------------------------------------------------------------- the approval
class TestApprovalPreview:
    def test_a_model_written_command_is_not_markup(self, tmp_path: Path) -> None:
        console, stream = terminal()
        panel = approval.preview("run_command", {"command": "echo [red]x[/red] \x1b[2J"}, tmp_path)
        console.print(panel, highlight=False)

        body = stream.getvalue()
        assert "[red]x[/red]" in body
        assert "\x1b[2J" not in body

    def test_a_path_with_brackets_does_not_raise(self, tmp_path: Path) -> None:
        console, _ = terminal()
        panel = approval.preview("write_file", {"path": "[notes", "content": "x"}, tmp_path)

        console.print(panel)  # would raise MarkupError if the path were raw


# ----------------------------------------------------------------- the paste
class TestPaste:
    """A pasted block arrives in one read and must land in one piece."""

    def test_a_multi_line_block_is_one_key(self) -> None:
        reader = KeyReader(ScriptedReads(b"first line\nsecond line\n"), more=lambda: False)

        assert reader.read() == PASTE + "first line\nsecond line\n"

    def test_a_bracketed_paste_drops_its_markers(self) -> None:
        reader = KeyReader(ScriptedReads(b"\x1b[200~one\ntwo\x1b[201~"), more=lambda: False)

        assert reader.read() == PASTE + "one\ntwo"

    def test_a_split_bracketed_paste_waits_for_the_rest(self) -> None:
        reads = ScriptedReads(b"\x1b[200~half", b" of it\x1b[201~x")
        reader = KeyReader(reads, more=lambda: bool(reads.chunks))

        assert reader.read() == PASTE + "half of it"

    def test_a_block_lands_in_the_box_in_one_piece(self) -> None:
        state = Composer()
        reader = KeyReader(ScriptedReads(b"one\ntwo\nthree\n"), more=lambda: False)

        outcome = apply_key(state, reader.read())

        assert outcome != "submit", "a paste must not press Enter"
        assert state.text == "one two three"

    def test_a_paste_appends_where_the_cursor_is(self) -> None:
        state = Composer(text="ad", cursor=1)

        apply_key(state, PASTE + "b\nc")

        assert state.text == "ab cd"

    def test_two_arrow_keys_are_still_two_keys(self) -> None:
        # The regression guard: the paste path must not swallow batched keys.
        reader = KeyReader(ScriptedReads(b"\x1b[D\x1b[D"), more=lambda: False)

        assert [reader.read(), reader.read()] == ["left", "left"]

    def test_enter_on_its_own_still_submits(self) -> None:
        state = Composer(text="hello")
        reader = KeyReader(ScriptedReads(b"\n"), more=lambda: False)

        assert apply_key(state, reader.read()) == "submit"


# ---------------------------------------------------------------- the frame
class TestTheFrame:
    """Geometry, at every width, in every state."""

    @pytest.mark.parametrize("width", [12, 13, 16, 24, 33, 40, 62, 80, 120, 200])
    @pytest.mark.parametrize("busy", [False, True])
    @pytest.mark.parametrize("notice", ["", "queued"])
    def test_every_row_is_exactly_the_box_width(self, width: int, busy: bool, notice: str) -> None:
        for text in ("", "hello", "x" * 300, "日本語" * 40, "e\u0301" * 50):
            state = Composer()
            state.insert(text)
            render = render_composer(
                state,
                width=width,
                meta="gpt-4o-mini · ask me first",
                label="Enter: queue" if busy else "Enter: send",
                busy=busy,
                phase=7,
                caret=True,
                notice=notice,
                queued=("a", "b") if busy else (),
            )

            # Cells, not characters: a CJK glyph is two of them, and that is
            # what decides whether the frame is pushed off the row.
            widths = [Text(row).cell_len for row in render.plain.split("\n")]
            assert all(cells == width for cells in widths), (width, text, widths)

    def test_the_caret_never_pushes_a_cell_out(self) -> None:
        # The caret was drawn *after* the row had been filled to the frame,
        # which made the row one cell wider at narrow widths.
        state = Composer()
        state.insert("x" * 200)

        render = render_composer(state, width=16, caret=True)

        assert all(Text(row).cell_len == 16 for row in render.plain.split("\n"))

    @pytest.mark.parametrize("width", [20, 26, 30])
    def test_a_cut_placeholder_ends_at_a_word(self, width: int) -> None:
        from jaigent.composer import IDLE_PLACEHOLDER

        row = render_composer(Composer(), width=width).plain.split("\n")[1]
        hint = row.split("❯ ", 1)[1].split("…")[0].rstrip()

        # "Ask anything, or ty…" reads like a glitch; a cut at a word reads
        # like a hint that was shortened on purpose. Where the whole hint fits,
        # there is nothing to cut.
        assert IDLE_PLACEHOLDER.startswith(hint), hint
        assert IDLE_PLACEHOLDER[len(hint)] in {" ", "…"}, hint
        if len(IDLE_PLACEHOLDER.strip()) > len(hint):
            assert "…" in row


class TestTheFlash:
    """A sent line leaves a light on the frame, not a row of chrome."""

    def _box(self) -> LiveComposer:
        console, _ = terminal()
        return LiveComposer(console, read_key=lambda: "eof")

    def test_the_frame_lights_up_when_a_line_leaves(self) -> None:
        box = self._box()
        box.state.insert("hello")

        box.flash()

        assert box.flashing, "nothing acknowledged the send"
        styles = {str(span.style).upper() for span in box.render().spans}
        assert any("FFB347" in style for style in styles)

    def test_the_flash_goes_away_on_its_own(self) -> None:
        box = self._box()

        box.flash(seconds=0)

        assert not box.flashing

    def test_a_queued_message_flashes_too(self) -> None:
        box = self._box()
        box.state.insert("typed ahead")

        box._queue_current()

        assert box.pending == ["typed ahead"]
        assert box.flashing

    def test_the_flash_survives_animation_being_off(self) -> None:
        # --no-animation turns off the travelling glow, not the answer to
        # "did my message go?".
        box = self._box()
        box.set_mode(animate=False)

        box.flash()

        assert box.flashing
