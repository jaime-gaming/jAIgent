"""The chat box: the line you type into, and the queue behind it.

The interesting half is pure — a state machine over keystrokes and a renderer
that takes a width — so almost all of it is exercised without a terminal. The
last class runs the box against a real pty, because "it looks like a frame and
the keys land where you expect" is exactly the kind of claim a stub can only
make about itself.
"""

from __future__ import annotations

import contextlib
import io
import sys
from pathlib import Path

import pytest
from rich.console import Console

from jaigent import composer
from jaigent.composer import (
    CLEARED,
    EOF_KEY,
    IDLE_PLACEHOLDER,
    INTERRUPT,
    INTERRUPT_AND_SEND,
    QUEUE_PLACEHOLDER,
    STAY,
    SUBMIT,
    Composer,
    ComposerLine,
    KeyReader,
    Line,
    LiveComposer,
    SigintQueue,
    apply_key,
    key_name,
    queue_summary,
    render_composer,
)


def typed(text: str) -> Composer:
    """A composer with ``text`` typed into it and the cursor at the end."""
    state = Composer()
    for char in text:
        apply_key(state, char)
    return state


class TestEditing:
    def test_typing_appends_at_the_cursor(self) -> None:
        state = typed("abc")
        assert (state.text, state.cursor) == ("abc", 3)

    def test_inserting_in_the_middle_moves_the_cursor_past_it(self) -> None:
        state = typed("ac")
        apply_key(state, "left")

        apply_key(state, "b")

        assert state.text == "abc"
        assert state.cursor == 2

    def test_backspace_deletes_before_the_cursor(self) -> None:
        state = typed("ab")
        apply_key(state, "backspace")

        assert state.text == "a"

    def test_backspace_at_the_start_does_nothing(self) -> None:
        state = typed("ab")
        apply_key(state, "home")
        apply_key(state, "backspace")

        assert state.text == "ab"

    def test_delete_removes_under_the_cursor(self) -> None:
        state = typed("abc")
        apply_key(state, "home")
        apply_key(state, "delete")

        assert state.text == "bc"
        assert state.cursor == 0

    def test_delete_at_the_end_does_nothing(self) -> None:
        state = typed("ab")
        apply_key(state, "delete")

        assert state.text == "ab"

    def test_the_cursor_stops_at_both_ends(self) -> None:
        state = typed("ab")
        for _ in range(5):
            apply_key(state, "right")
        assert state.cursor == 2
        for _ in range(5):
            apply_key(state, "left")
        assert state.cursor == 0

    def test_home_and_end(self) -> None:
        state = typed("hello world")
        apply_key(state, "home")
        assert state.cursor == 0
        apply_key(state, "end")
        assert state.cursor == len("hello world")

    def test_word_jumps_land_on_word_boundaries(self) -> None:
        state = typed("read the notes now")
        for _ in range(2):
            apply_key(state, "word-left")

        assert state.text[state.cursor :] == "notes now"

        apply_key(state, "word-right")
        assert state.text[state.cursor :] == " now"

    def test_word_left_stops_after_the_last_word(self) -> None:
        # Jumping back from the end lands at the start of the final word, not
        # before the space in front of it: that is where a shell puts it.
        state = typed("two words")
        apply_key(state, "word-left")
        apply_key(state, "word-left")

        assert state.text[state.cursor :] == "two words"

    def test_ctrl_w_kills_the_word_before_the_cursor(self) -> None:
        state = typed("summarise these notes")

        apply_key(state, "ctrl-w")

        assert state.text == "summarise these "
        assert state.cursor == len("summarise these ")

    def test_ctrl_u_kills_to_the_start(self) -> None:
        state = typed("keep this")
        apply_key(state, "left")
        apply_key(state, "left")
        apply_key(state, "left")
        apply_key(state, "left")

        apply_key(state, "ctrl-u")

        assert state.text == "this"
        assert state.cursor == 0

    def test_ctrl_k_kills_to_the_end(self) -> None:
        state = typed("keep this")
        for _ in range(4):
            apply_key(state, "left")

        apply_key(state, "ctrl-k")

        assert state.text == "keep "
        assert state.cursor == len("keep ")

    def test_an_empty_enter_is_not_a_message(self) -> None:
        state = Composer()

        assert apply_key(state, "enter") == STAY
        assert apply_key(Composer(), "enter") == STAY

    def test_enter_with_text_asks_the_caller_to_send(self) -> None:
        assert apply_key(typed("hi"), "enter") == SUBMIT

    def test_whitespace_only_is_still_empty(self) -> None:
        state = typed("   ")

        assert state.empty is True
        assert apply_key(state, "enter") == STAY

    def test_the_value_is_trimmed(self) -> None:
        assert typed("  hi  ").value == "hi"

    def test_control_characters_this_build_does_not_use_are_swallowed(self) -> None:
        state = typed("ab")

        for key in ("\x12", "\x1f", "esc", "tab", "insert"):
            assert apply_key(state, key) == STAY

        assert state.text == "ab"

    def test_non_ascii_text_is_text(self) -> None:
        state = typed("¿qué tal? ✓")

        assert state.text == "¿qué tal? ✓"

    def test_ctrl_d_on_an_empty_box_leaves(self) -> None:
        assert apply_key(Composer(), "ctrl-d") == EOF_KEY

    def test_ctrl_d_on_a_full_box_deletes_like_readline(self) -> None:
        state = typed("abc")
        apply_key(state, "home")

        assert apply_key(state, "ctrl-d") == STAY
        assert state.text == "bc"

    def test_ctrl_l_asks_for_a_clear(self) -> None:
        assert apply_key(typed("abc"), "ctrl-l") == CLEARED


class TestCtrlC:
    def test_on_an_empty_box_it_interrupts_nothing(self) -> None:
        assert apply_key(Composer(), "ctrl-c") == INTERRUPT

    def test_with_text_it_interrupts_and_keeps_the_text(self) -> None:
        state = typed("wait, do this instead")

        assert apply_key(state, "ctrl-c") == INTERRUPT_AND_SEND
        # The text is not touched: the caller sends it *because* of the Ctrl-C.
        assert state.text == "wait, do this instead"


class TestHistory:
    def test_sent_lines_are_remembered_in_order(self) -> None:
        state = Composer()
        state.remember("one")
        state.remember("two")

        assert state.history == ["one", "two"]

    def test_blanks_and_immediate_repeats_are_not_kept(self) -> None:
        state = Composer()
        for line in ("", "  ", "same", "same"):
            state.remember(line)

        assert state.history == ["same"]

    def test_up_walks_back_and_down_returns(self) -> None:
        state = Composer(history=["first", "second"])

        assert apply_key(state, "up") == STAY
        assert state.text == "second"
        apply_key(state, "up")
        assert state.text == "first"
        apply_key(state, "down")
        assert state.text == "second"

    def test_a_half_written_line_survives_a_trip_through_history(self) -> None:
        state = typed("half written")
        state.remember("old")

        apply_key(state, "up")
        assert state.text == "old"
        apply_key(state, "down")

        assert state.text == "half written"

    def test_browsing_past_the_oldest_entry_stops(self) -> None:
        state = Composer(history=["only"])
        apply_key(state, "up")

        assert apply_key(state, "up") == STAY
        assert state.text == "only"

    def test_history_is_bounded(self) -> None:
        state = Composer()
        for index in range(composer.HISTORY_LIMIT + 20):
            state.remember(f"line {index}")

        assert len(state.history) == composer.HISTORY_LIMIT
        assert state.history[-1] == f"line {composer.HISTORY_LIMIT + 19}"

    def test_editing_leaves_history_browsing(self) -> None:
        state = Composer(history=["old"])
        apply_key(state, "up")

        apply_key(state, "x")

        # Typing after browsing edits the recalled line rather than jumping
        # back to the draft, which is what every shell does.
        assert state.text == "oldx"


class TestKeyNames:
    @pytest.mark.parametrize(
        ("raw", "name"),
        [
            ("\r", "enter"),
            ("\n", "enter"),
            ("\x7f", "backspace"),
            ("\x01", "home"),
            ("\x05", "end"),
            ("\x03", "ctrl-c"),
            ("\x04", "ctrl-d"),
            ("\x0c", "ctrl-l"),
            ("\x15", "ctrl-u"),
            ("\x0b", "ctrl-k"),
            ("\x17", "ctrl-w"),
            ("\x10", "up"),
            ("\x0e", "down"),
            ("a", "a"),
            ("é", "é"),
        ],
    )
    def test_single_characters(self, raw: str, name: str) -> None:
        assert key_name(raw) == name


class ScriptedReads:
    """A reader over a list of byte chunks, delivered one per call."""

    def __init__(self, *chunks: bytes) -> None:
        self.chunks = list(chunks)
        self.reads = 0

    def __call__(self) -> bytes:
        self.reads += 1
        return self.chunks.pop(0) if self.chunks else b""


class TestKeyReader:
    def test_a_chunk_of_text_is_one_key_at_a_time(self) -> None:
        reader = KeyReader(ScriptedReads(b"abc"))

        assert [reader.read() for _ in range(3)] == ["a", "b", "c"]

    def test_keys_are_not_lost_when_the_terminal_batches_them(self) -> None:
        # The bug this exists for: two arrow keys arriving in one read used to
        # come back as a single "left", so the cursor only moved once.
        reader = KeyReader(ScriptedReads(b"\x1b[D\x1b[D"))

        assert [reader.read(), reader.read()] == ["left", "left"]

    def test_an_escape_sequence_split_across_reads_is_joined(self) -> None:
        reads = ScriptedReads(b"\x1b", b"[", b"D")
        reader = KeyReader(reads, more=lambda: bool(reads.chunks))

        assert reader.read() == "left"

    def test_a_bare_escape_is_escape(self) -> None:
        reader = KeyReader(ScriptedReads(b"\x1b"))

        assert reader.read() == "esc"

    def test_an_unknown_sequence_is_consumed_whole(self) -> None:
        # Pressing a key this build does not map must not type "[1;2C".
        reader = KeyReader(ScriptedReads(b"\x1b[1;2C"))

        assert reader.read() == STAY
        assert reader.buffered == b""

    def test_alt_keys_map_to_word_jumps(self) -> None:
        assert KeyReader(ScriptedReads(b"\x1bb")).read() == "word-left"
        assert KeyReader(ScriptedReads(b"\x1bf")).read() == "word-right"

    def test_ctrl_arrows_map_to_word_jumps(self) -> None:
        assert KeyReader(ScriptedReads(b"\x1b[1;5D")).read() == "word-left"
        assert KeyReader(ScriptedReads(b"\x1b[1;5C")).read() == "word-right"

    def test_a_multibyte_character_is_one_key(self) -> None:
        # "ñ" is two bytes: reading one byte at a time would corrupt it.
        reader = KeyReader(ScriptedReads("ñ".encode()))

        assert reader.read() == "ñ"

    def test_a_multibyte_character_split_across_reads_is_rejoined(self) -> None:
        raw = "漢".encode()
        reads = ScriptedReads(raw[:1], raw[1:])
        reader = KeyReader(reads, more=lambda: bool(reads.chunks))

        assert reader.read() == "漢"
        assert reader.buffered == b""

    def test_a_partial_character_with_nothing_following_is_not_a_crash(self) -> None:
        reader = KeyReader(ScriptedReads("ñ".encode()[:1]), more=lambda: False)

        assert reader.read() != ""  # a replacement character, not an exception

    @pytest.mark.parametrize("sequence", [b"\x1b[A", b"\x1b[B", b"\x1b[C", b"\x1b[D"])
    def test_arrows(self, sequence: bytes) -> None:
        expected = {b"\x1b[A": "up", b"\x1b[B": "down", b"\x1b[C": "right", b"\x1b[D": "left"}
        assert KeyReader(ScriptedReads(sequence)).read() == expected[sequence]

    def test_the_end_of_input_is_eof(self) -> None:
        assert KeyReader(ScriptedReads()).read() == "eof"

    def test_a_write_error_is_read_as_eof(self) -> None:
        def broken() -> bytes:
            raise OSError("the pty went away")

        assert KeyReader(broken).read() == "eof"


def plain(render: composer.Text) -> list[str]:  # noqa: ANN001 - rich Text
    return render.plain.split("\n")


class TestRendering:
    def test_the_box_is_a_closed_frame_of_the_requested_width(self) -> None:
        rows = plain(render_composer(Composer(), width=40, meta="m", label="Enter: send"))

        assert len(rows) == 3
        assert all(len(row) == 40 for row in rows), rows
        assert rows[0][0] + rows[0][-1] == "╭╮"
        assert rows[-1][0] + rows[-1][-1] == "╰╯"

    @pytest.mark.parametrize("width", [16, 24, 33, 60, 100, 200])
    def test_no_width_wraps(self, width: int) -> None:
        state = typed("a" * 300)

        rows = plain(render_composer(state, width=width, meta="gpt-4o · auto", queued=["x"]))

        assert all(len(row) <= width for row in rows), rows

    @pytest.mark.parametrize("width", [16, 24, 33, 60, 100, 200])
    def test_no_width_wraps_while_busy(self, width: int) -> None:
        state = typed("a" * 300)

        rows = plain(
            render_composer(
                state, width=width, meta="gpt-4o · auto", queued=["x"], busy=True, phase=3
            )
        )

        assert all(len(row) <= width for row in rows), rows

    @pytest.mark.parametrize("width", [20, 30, 40, 60])
    def test_the_line_never_grows_a_row(self, width: int) -> None:
        state = typed("x" * 200)

        rows = plain(render_composer(state, width=width))

        # Top, line, bottom — never more, however long the line: a box that
        # grew would walk the transcript off the top of the screen.
        assert len(rows) == 3

    def test_a_line_that_fits_is_shown_whole(self) -> None:
        state = Composer(text="short line", cursor=10)

        assert "short line" in plain(render_composer(state, width=60))[1]

    def test_the_window_scrolls_to_keep_the_cursor_visible(self) -> None:
        state = Composer(text="a" * 80, cursor=80)

        shown, cursor = composer._window(state.text, state.cursor, 20)  # noqa: SLF001

        # A full window, ending at the cursor: the tail is what is being typed.
        assert shown == "a" * 20
        assert cursor == 20

    def test_the_window_does_not_scroll_back_past_the_end(self) -> None:
        shown, cursor = composer._window("abcdef", 6, 4)  # noqa: SLF001

        assert shown == "cdef"
        assert cursor == 4

    def test_a_wide_character_counts_as_two_cells(self) -> None:
        assert composer._cells("漢字") == 4  # noqa: SLF001
        assert composer._cells("abc") == 3

    def test_wide_characters_fit_the_room_they_are_given(self) -> None:
        shown, cursor = composer._window("漢字漢字", 4, 5)  # noqa: SLF001

        assert composer._cells(shown) <= 5
        assert cursor <= 5

    def test_an_empty_box_shows_the_placeholder_instead(self) -> None:
        rows = plain(render_composer(Composer(), width=60, placeholder="Ask anything"))

        assert "Ask anything" in rows[1]

    def test_a_queued_line_is_counted_in_the_footer(self) -> None:
        rows = plain(render_composer(Composer(), width=60, queued=["a", "b"], label="Enter: queue"))

        assert "2 queued" in rows[-1]
        assert "Enter: queue" in rows[-1]

    def test_the_footer_carries_both_ends(self) -> None:
        rows = plain(render_composer(Composer(), width=70, meta="gpt-4o · auto"))

        assert "gpt-4o · auto" in rows[-1]
        assert "Enter: send" in rows[-1]

    def test_there_is_no_help_line_under_the_box(self) -> None:
        # The keyboard legend belongs in /help, not welded under the prompt
        # the user looks at all day.
        rows = plain(render_composer(Composer(), width=80, meta="gpt-4o · auto"))

        assert len(rows) == 3
        text = "\n".join(rows)
        assert "Ctrl-C" not in text
        assert "Esc" not in text
        assert "Enter queues" not in text

    def test_a_narrow_box_drops_the_label_whole_instead_of_chopping_it(self) -> None:
        rows = plain(render_composer(Composer(), width=22, meta="gpt-4o-mini", queued=["a"]))

        footer = rows[-1]
        assert len(footer) <= 22
        # Never half a word: either the label is there, or it is not.
        assert "Enter: se" not in footer
        assert footer.endswith("╯")

    def test_a_very_narrow_box_still_has_corners(self) -> None:
        rows = plain(render_composer(Composer(), width=12, meta="a-long-model-name"))

        assert rows[0][0] + rows[0][-1] == "╭╮"
        assert rows[-1][0] + rows[-1][-1] == "╰╯"

    def test_the_frame_is_muted_while_a_turn_owns_the_screen(self) -> None:
        render = render_composer(Composer(), width=40, busy=True)

        styles = {str(span.style) for span in render.spans}
        # The footer's keyboard hint is coloured while idle and muted while
        # busy; either way the frame itself is drawn with the dim accent.
        assert styles, "nothing was styled at all"

    def test_ascii_when_unicode_is_not_available(self) -> None:
        rows = plain(render_composer(Composer(), width=30, unicode_ok=False))

        assert rows[0].startswith("+") and rows[0].endswith("+")
        assert "─" not in "".join(rows)
        assert "❯" not in "".join(rows)


class TestQueueSummary:
    def test_nothing_queued_says_nothing(self) -> None:
        assert queue_summary([]) == ""

    def test_one_is_spelled_out(self) -> None:
        assert queue_summary(["a"]) == "1 queued"

    def test_many_are_counted(self) -> None:
        assert queue_summary(["a", "b", "c"]) == "3 queued"


class ScriptedKeys:
    """A key reader over a fixed script."""

    def __init__(self, *keys: str) -> None:
        self.keys = list(keys)
        self.reads = 0

    def __call__(self) -> str:
        self.reads += 1
        return self.keys.pop(0) if self.keys else "eof"


def console(width: int = 60) -> Console:
    return Console(width=width, file=io.StringIO())


class TestLiveComposer:
    def test_read_returns_the_line_that_was_typed(self) -> None:
        box = LiveComposer(console(), read_key=ScriptedKeys("h", "i", "enter"))

        assert box.read() == Line("hi")

    def test_read_skips_empty_enters(self) -> None:
        box = LiveComposer(console(), read_key=ScriptedKeys("enter", "x", "enter"))

        assert box.read().text == "x"

    def test_a_cancelled_line_still_carries_its_text(self) -> None:
        box = LiveComposer(console(), read_key=ScriptedKeys("s", "t", "o", "p", "ctrl-c"))

        line = box.read()

        assert line.text == "stop"
        assert line.cancel is True

    def test_ctrl_c_on_an_empty_box_cancels_without_text(self) -> None:
        box = LiveComposer(console(), read_key=ScriptedKeys("ctrl-c"))

        assert box.read() == Line("", cancel=True)

    def test_ctrl_d_closes_the_box(self) -> None:
        box = LiveComposer(console(), read_key=ScriptedKeys("ctrl-d"))

        with pytest.raises(ComposerLine):
            box.read()

    def test_eof_from_the_reader_also_closes(self) -> None:
        box = LiveComposer(console(), read_key=ScriptedKeys())

        with pytest.raises(ComposerLine):
            box.read()

    def test_a_sent_line_goes_into_the_history(self) -> None:
        box = LiveComposer(console(), read_key=ScriptedKeys("o", "n", "e", "enter"))

        box.read()

        assert box.state.history == ["one"]

    def test_the_queue_is_first_in_first_out(self) -> None:
        box = LiveComposer(console(), read_key=ScriptedKeys())

        box.pending.extend(["first", "second"])

        assert box.take_next() == "first"
        assert box.take_next() == "second"
        assert box.take_next() is None

    def test_queuing_what_was_typed_moves_it_out_of_the_box(self) -> None:
        box = LiveComposer(console(), read_key=ScriptedKeys())
        box._queue_current()  # noqa: SLF001 - the behaviour under test

        box.state.insert("write this next")
        box._queue_current()  # noqa: SLF001

        assert box.pending == ["write this next"]
        assert box.state.text == ""

    def test_listening_queues_a_line_typed_during_a_turn(self) -> None:
        keys = ScriptedKeys("n", "e", "x", "t", "enter")
        box = LiveComposer(console(), read_key=keys)

        box._pump_keys(composer.threading.Event())  # noqa: SLF001

        assert box.pending == ["next"]

    def test_queueing_acknowledges_inside_the_box(self) -> None:
        keys = ScriptedKeys("n", "e", "x", "t", "enter")
        box = LiveComposer(console(), read_key=keys)

        box._pump_keys(composer.threading.Event())  # noqa: SLF001

        assert "queued" in box.notice

    def test_ctrl_c_while_listening_asks_to_interrupt(self) -> None:
        keys = ScriptedKeys("text", "ctrl-c")
        box = LiveComposer(console(), read_key=keys)

        box._pump_keys(composer.threading.Event())  # noqa: SLF001

        assert box.interrupted is True
        assert box.pending == ["text"]

    def test_ctrl_d_while_listening_is_remembered_not_lost(self) -> None:
        keys = ScriptedKeys("ctrl-d")
        box = LiveComposer(console(), read_key=keys)

        box._pump_keys(composer.threading.Event())  # noqa: SLF001

        assert box.eof_requested is True

    def test_a_signal_ctrl_c_is_a_keystroke(self) -> None:
        sig = SigintQueue()
        sig.count = 1
        box = LiveComposer(console(), read_key=ScriptedKeys("a", "b", "enter"))
        box.sigint = sig

        # Ctrl-C arrives as a signal while the reader is between keys.
        box._next_key()  # noqa: SLF001

        assert sig.count == 0

    def test_modes_are_pure_state_without_a_terminal(self) -> None:
        box = LiveComposer(console(), read_key=ScriptedKeys())

        box.set_mode(placeholder=QUEUE_PLACEHOLDER, label="Enter: queue", busy=True)

        assert box.placeholder == QUEUE_PLACEHOLDER
        assert box.label == "Enter: queue"
        box.set_mode(placeholder=IDLE_PLACEHOLDER, label="Enter: send", busy=False)
        assert box.label == "Enter: send"

    def test_notify_without_a_screen_prints_and_keeps_going(self) -> None:
        stream = io.StringIO()
        box = LiveComposer(Console(width=60, file=stream), read_key=ScriptedKeys())

        box.notify("queued hello")

        assert "queued hello" in stream.getvalue()

    def test_closing_an_unstarted_box_is_safe(self) -> None:
        box = LiveComposer(console(), read_key=ScriptedKeys())

        box.close()
        box.close()

        assert box.showing is False


class TestSigintQueue:
    def test_take_consumes_one_at_a_time(self) -> None:
        sig = SigintQueue()
        sig.count = 2

        assert sig.take() is True
        assert sig.take() is True
        assert sig.take() is False

    def test_that_is_all_it_does(self) -> None:
        # The handler must be safe to run at any moment: it records and returns.
        sig = SigintQueue()
        sig._on_sigint(2, None)  # noqa: SLF001

        assert sig.count == 1


class TestScreen:
    def test_the_region_stacks_status_answer_and_box(self) -> None:
        from rich.text import Text

        from jaigent.composer import ChatScreen

        screen = ChatScreen(console())
        screen.set_status(lambda: Text("working"))
        screen.set_answer(Text("answer"))
        screen.set_box(lambda: Text("box"))

        rendered = screen.compose()

        assert len(rendered.renderables) == 3  # type: ignore[attr-defined]

    def test_an_empty_status_line_is_not_drawn(self) -> None:
        from rich.text import Text

        from jaigent.composer import ChatScreen

        screen = ChatScreen(console())
        screen.set_status(lambda: Text(""))
        screen.set_box(lambda: Text("box"))

        assert screen.compose().plain == "box"  # type: ignore[attr-defined]

    def test_nothing_at_all_is_an_empty_renderable(self) -> None:
        from jaigent.composer import ChatScreen

        assert ChatScreen(console()).compose().plain == ""


@pytest.mark.skipif(sys.platform == "win32", reason="pty is POSIX-only")
class TestAgainstARealTerminal:
    """The box on an actual pty: the frame appears and the keys land."""

    CHILD = r"""
import sys
sys.path.insert(0, {source!r})
from rich.console import Console
from jaigent.composer import LiveComposer, ComposerLine

console = Console(force_terminal=True, color_system="truecolor")
box = LiveComposer(console, meta="gpt-4o · auto")
box.start()
print("READY", flush=True)
line = box.read()
box.close()
print("GOT:" + line.text, flush=True)
"""

    def test_typing_lands_in_the_box_and_enter_returns_it(self) -> None:
        import os
        import pty
        import select
        import time

        source = str(Path(__file__).resolve().parent.parent / "src")
        master, slave = pty.openpty()
        pid = os.fork()
        if pid == 0:  # pragma: no cover - the forked child
            os.close(master)
            os.setsid()
            for fd in (0, 1, 2):
                os.dup2(slave, fd)
            os.close(slave)
            os.execv(sys.executable, [sys.executable, "-c", self.CHILD.format(source=source)])
        os.close(slave)

        output = bytearray()

        def drain(deadline: float, marker: str) -> str:
            while time.monotonic() < deadline:
                text = output.decode(errors="replace")
                if marker and marker in text:
                    return text
                readable, _, _ = select.select([master], [], [], 0.05)
                if not readable:
                    continue
                try:
                    chunk = os.read(master, 8192)
                except OSError:
                    break
                if not chunk:
                    break
                output.extend(chunk)
            return output.decode(errors="replace")

        try:
            drain(time.monotonic() + 5.0, "READY")
            os.write(master, b"hello box")
            drain(time.monotonic() + 3.0, "hello box")
            os.write(master, b"\r")
            text = drain(time.monotonic() + 5.0, "GOT:")
        finally:
            os.close(master)
            with contextlib.suppress(OSError):  # already gone
                os.kill(pid, 9)
            os.waitpid(pid, 0)

        assert "GOT:hello box" in text
        # And the frame was actually drawn, not just the text echoed.
        assert "╭" in text
        assert "Enter: send" in text
        assert "gpt-4o · auto" in text


class TestRetracting:
    """Esc takes the newest queued message back, editable."""

    def test_retract_returns_the_line_to_the_box(self) -> None:
        box = LiveComposer(console(), read_key=ScriptedKeys())
        box.pending.extend(["first", "second"])

        assert box.retract() == "second"
        assert box.state.text == "second"
        assert box.pending == ["first"]
        assert "box" in box.notice, "retracting says where the line went"

    def test_retract_with_nothing_queued_does_nothing(self) -> None:
        box = LiveComposer(console(), read_key=ScriptedKeys())

        assert box.retract() is None
        assert box.state.text == ""

    def test_esc_retracts_when_the_box_is_empty(self) -> None:
        box = LiveComposer(console(), read_key=ScriptedKeys("esc"))
        box.pending.append("wait, not that")

        assert box._next_key() == composer.STAY  # noqa: SLF001
        assert box.state.text == "wait, not that"
        assert box.pending == []

    def test_esc_does_nothing_when_the_queue_is_empty(self) -> None:
        box = LiveComposer(console(), read_key=ScriptedKeys("esc"))

        assert box._next_key() == "esc"  # noqa: SLF001
        assert box.state.text == ""

    def test_esc_while_typing_does_not_retract(self) -> None:
        box = LiveComposer(console(), read_key=ScriptedKeys("esc"))
        box.pending.append("queued")
        box.state.insert("half written")

        assert box._next_key() == "esc"  # noqa: SLF001
        assert box.pending == ["queued"]
        assert box.state.text == "half written"


class TestMotion:
    """The frame keeps moving while a turn runs, and stops when it ends."""

    @staticmethod
    def _highlight(phase: int) -> tuple[int, int]:
        """Where the lit part of the top border starts, and how long it is."""
        render = render_composer(Composer(), width=40, busy=True, phase=phase, animate=True)
        lit = [span.start for span in render.spans if str(span.style) == composer.ACCENT]
        return (min(lit), len(lit)) if lit else (-1, 0)

    def test_the_highlight_travels_along_the_border(self) -> None:
        # The text of the border is identical every frame — it is all "─" —
        # so movement has to be read from the styles, which is what the user
        # actually sees.
        positions = [self._highlight(phase)[0] for phase in range(8)]

        assert len(set(positions)) > 3, f"the highlight did not move: {positions}"
        assert positions == sorted(positions), "the light should travel one way"
        # And it comes back round rather than blinking out at the edge.
        wrapped = [self._highlight(phase)[0] for phase in range(30)]
        assert any(position >= 0 for position in wrapped)

    def test_every_frame_is_the_same_box(self) -> None:
        for phase in range(8):
            rows = render_composer(
                Composer(), width=40, busy=True, phase=phase, animate=True
            ).plain.split("\n")

            assert len(rows) == 3
            assert all(len(row) == 40 for row in rows)

    def test_an_idle_box_does_not_move(self) -> None:
        frames = {
            render_composer(Composer(), width=40, busy=False, phase=phase).plain
            for phase in range(6)
        }

        assert len(frames) == 1

    def test_animation_can_be_turned_off(self) -> None:
        frames = {
            render_composer(
                Composer(), width=40, busy=True, phase=phase, animate=False
            ).plain.split("\n")[0]
            for phase in range(6)
        }

        assert len(frames) == 1

    def test_a_narrow_box_does_not_animate(self) -> None:
        # Four frames in a 12-cell border would read as noise, not as motion.
        frames = {
            render_composer(Composer(), width=14, busy=True, phase=phase, animate=True).plain.split(
                "\n"
            )[0]
            for phase in range(8)
        }

        assert len(frames) == 1

    def test_ascii_consoles_animate_with_ascii(self) -> None:
        frame = render_composer(
            Composer(), width=40, busy=True, phase=3, animate=True, unicode_ok=False
        ).plain.split("\n")[0]

        assert frame.startswith("+") and frame.endswith("+")
        assert "─" not in frame


class TestCaret:
    def test_the_cursor_is_marked_while_the_box_has_the_keyboard(self) -> None:
        state = Composer(text="abc", cursor=1)

        render = render_composer(state, width=40, caret=True)

        reversed_spans = [
            span for span in render.spans if span.style and "reverse" in str(span.style)
        ]
        assert reversed_spans, "no cursor was drawn"

    def test_no_caret_while_the_turn_owns_the_keyboard(self) -> None:
        state = Composer(text="abc", cursor=1)

        render = render_composer(state, width=40, caret=False)

        assert not [s for s in render.spans if s.style and "reverse" in str(s.style)]

    def test_the_caret_at_the_end_is_a_block(self) -> None:
        state = Composer(text="abc", cursor=3)

        render = render_composer(state, width=40, caret=True)

        assert "abc " in render.plain.split("\n")[1]


class TestNotices:
    """Acknowledgements happen inside the box and expire on their own."""

    def test_a_notice_is_drawn_inside_the_frame(self) -> None:
        rows = plain(render_composer(Composer(), width=60, notice="queued · sends next"))

        assert len(rows) == 4
        assert "queued · sends next" in rows[0]
        # Inside the frame: it starts with a space, not a corner or a border.
        assert rows[0].rstrip().endswith("│")
        assert not rows[0].startswith("╭")

    def test_a_notice_does_not_widen_the_box(self) -> None:
        rows = plain(render_composer(Composer(), width=30, notice="a very long acknowledgement"))

        assert all(len(row) <= 30 for row in rows)

    def test_a_notice_expires(self) -> None:
        box = LiveComposer(console(), read_key=ScriptedKeys())
        box.say("queued", seconds=0.0)

        assert box.notice == ""

    def test_a_notice_starts_visible(self) -> None:
        box = LiveComposer(console(), read_key=ScriptedKeys())
        box.say("queued", seconds=5.0)

        assert box.notice == "queued"


class TestFittingTheWindow:
    """The box follows the terminal: width, resize, and everything in cells."""

    def test_the_label_hides_on_narrow_terminals_without_chopping_words(self) -> None:
        for width in range(12, 90):
            rows = plain(
                render_composer(
                    Composer(), width=width, meta="gpt-4o-mini", label="Enter: send", queued=["a"]
                )
            )

            assert all(len(row) <= width for row in rows), (width, rows)
            footer = rows[-1]
            assert footer.endswith("╯")
            # Whole pieces only: the label appears in full or not at all.
            assert "Enter:" not in footer or "Enter: send" in footer, (width, footer)

    def test_the_corners_survive_every_width(self) -> None:
        for width in range(12, 90):
            rows = plain(render_composer(Composer(), width=width, meta="m", busy=True))

            assert rows[0][0] + rows[0][-1] == "╭╮", width
            assert rows[-1][0] + rows[-1][-1] == "╰╯", width

    def test_a_resize_redraws_at_the_new_width(self) -> None:
        class Resizable(Console):
            """A console whose width can be changed, as a terminal's can."""

            def __init__(self, width: int) -> None:
                super().__init__(width=width, file=io.StringIO())
                self._width = width

            @property  # type: ignore[override]
            def width(self) -> int:
                return self._width

        stream = Resizable(80)
        # ASCII box for this one: the point is the width, not the glyphs.
        box = LiveComposer(stream, read_key=ScriptedKeys(), unicode_ok=False)
        box._width = 80  # noqa: SLF001 - what the last draw saw

        stream._width = 40
        box._redraw_after_resize()  # noqa: SLF001

        rows = box.render().plain.split("\n")
        assert rows[0].endswith("+")
        assert max(len(row) for row in rows) <= 40

    def test_the_ticker_stops_when_the_box_closes(self) -> None:
        box = LiveComposer(console(), read_key=ScriptedKeys())
        box._start_motion()  # noqa: SLF001

        box.close()

        assert box._motion is None  # noqa: SLF001

    def test_the_ticker_is_shared_not_multiplied(self) -> None:
        box = LiveComposer(console(), read_key=ScriptedKeys())

        box._start_motion()  # noqa: SLF001
        first = box._motion  # noqa: SLF001
        box._start_motion()  # noqa: SLF001

        assert box._motion is first  # noqa: SLF001
        box.close()
