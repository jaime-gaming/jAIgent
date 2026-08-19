"""The animated status line, phrases and platform fallbacks."""

from __future__ import annotations

import time

import pytest
from rich.console import Console

from jaigent import ui
from jaigent.ui import (
    ASCII_FRAMES,
    PHRASES,
    SPINNER_FRAMES,
    TOOL_PHRASES,
    Thinking,
    format_duration,
    format_tokens,
    glyph,
    pick_phrase,
    result_line,
    supports_unicode,
    tool_line,
)


def render(renderable, width: int = 90) -> str:
    console = Console(width=width, no_color=True)
    with console.capture() as capture:
        console.print(renderable)
    return capture.get()


class TestPhrases:
    def test_pool_is_substantial(self) -> None:
        assert len(PHRASES) >= 20

    def test_all_are_present_participles(self) -> None:
        # "Thinking…" reads right; "Think…" does not.
        assert all(p.endswith("ing") for p in PHRASES), [
            p for p in PHRASES if not p.endswith("ing")
        ]

    def test_all_capitalised_and_single_word(self) -> None:
        for phrase in PHRASES:
            assert phrase[0].isupper()
            assert " " not in phrase

    def test_pick_avoids_an_immediate_repeat(self) -> None:
        for _ in range(50):
            assert pick_phrase(exclude="Thinking") != "Thinking"

    def test_pick_survives_excluding_everything(self) -> None:
        assert pick_phrase(exclude=PHRASES[0]) in PHRASES

    def test_tool_phrases_cover_the_real_tools(self) -> None:
        for tool in ("web_search", "read_file", "write_file", "run_command", "load_skill"):
            assert tool in TOOL_PHRASES


class TestFormatting:
    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [(0, "0s"), (4, "4s"), (59, "59s"), (60, "1m"), (80, "1m 20s"), (120, "2m")],
    )
    def test_duration(self, seconds: float, expected: str) -> None:
        assert format_duration(seconds) == expected

    @pytest.mark.parametrize(
        ("seconds", "expected"),
        [
            (3600, "1h"),
            (3660, "1h 1m"),
            (3720, "1h 2m"),
            (7199, "1h 59m"),
            (7200, "2h"),
            (86_400, "24h"),
        ],
    )
    def test_duration_reaches_hours(self, seconds: float, expected: str) -> None:
        # A long agent run should not be reported as "127m 3s".
        assert format_duration(seconds) == expected

    def test_duration_drops_seconds_once_past_an_hour(self) -> None:
        # Seconds are noise at that scale, and they make the line jitter.
        assert format_duration(3661) == "1h 1m"

    @pytest.mark.parametrize(
        ("count", "expected"),
        [(0, "0"), (820, "820"), (1000, "1k"), (1234, "1.2k"), (15300, "15.3k")],
    )
    def test_tokens(self, count: int, expected: str) -> None:
        assert format_tokens(count) == expected

    @pytest.mark.parametrize(
        ("count", "expected"),
        [
            (999_999, "1000k"),
            (1_000_000, "1M"),
            (1_234_567, "1.2M"),
            (15_300_000, "15.3M"),
        ],
    )
    def test_tokens_reach_millions(self, count: int, expected: str) -> None:
        # Long sessions genuinely pass a million tokens; "1234.6k" is unreadable.
        assert format_tokens(count) == expected


class TestGlyphs:
    def test_unicode_variants(self) -> None:
        assert glyph("check", unicode_ok=True) == "✓"
        assert glyph("arrow", unicode_ok=True) == "→"

    def test_ascii_fallbacks_are_plain(self) -> None:
        for name in ui.GLYPHS:
            fallback = glyph(name, unicode_ok=False)
            assert fallback.isascii(), f"{name} fallback is not ASCII"

    def test_every_glyph_has_both_forms(self) -> None:
        for name, (fancy, plain) in ui.GLYPHS.items():
            assert fancy and plain, name

    def test_supports_unicode_on_utf8(self) -> None:
        class Stream:
            encoding = "utf-8"

        assert supports_unicode(Stream()) is True

    def test_rejects_a_legacy_codepage(self) -> None:
        class Stream:
            encoding = "cp437"  # a classic Windows console encoding

        assert supports_unicode(Stream()) is False

    def test_missing_encoding_is_treated_as_unsafe(self) -> None:
        class Stream:
            encoding = None

        assert supports_unicode(Stream()) is False


class TestThinking:
    def _status(self, **kwargs) -> Thinking:  # noqa: ANN003
        return Thinking(Console(width=90, no_color=True), animate=False, **kwargs)

    def test_renders_phrase_and_elapsed(self) -> None:
        status = self._status()
        status.update(phrase="Pondering")
        out = render(status.render())

        assert "Pondering" in out
        assert "0s" in out

    def test_shows_tokens_once_known(self) -> None:
        status = self._status()
        status.update(tokens=1500)
        assert "1.5k tokens" in render(status.render())

    def test_hides_tokens_when_zero(self) -> None:
        assert "tokens" not in render(self._status().render())

    def test_tool_started_switches_the_verb(self) -> None:
        status = self._status()
        status.tool_started("web_search")
        out = render(status.render())

        assert "Searching" in out
        assert "web_search" in out

    def test_unknown_tool_still_works(self) -> None:
        status = self._status()
        status.tool_started("mystery_tool")
        assert "Working" in render(status.render())

    def test_thinking_again_clears_the_detail(self) -> None:
        status = self._status()
        status.tool_started("read_file")
        status.thinking_again()

        assert status.state.detail == ""

    # -- narrow terminals ---------------------------------------------------
    # The line is redrawn in place by rich's Live. If it is wider than the
    # terminal it wraps, and every frame leaves a stale row behind.

    @pytest.mark.parametrize("width", [20, 30, 40, 60, 80, 120])
    def test_never_exceeds_the_terminal_width(self, width: int) -> None:
        status = Thinking(Console(width=width, no_color=True), animate=False)
        status.update(phrase="Contemplating", tokens=1_234_567, detail="web_search")

        assert status.render().cell_len <= width

    def test_extremely_narrow_terminal_still_renders_something(self) -> None:
        status = Thinking(Console(width=10, no_color=True), animate=False)
        status.update(phrase="Contemplating", tokens=1500, detail="web_search")
        line = status.render()

        assert line.cell_len <= 10
        assert line.plain.strip()

    def test_keeps_the_phrase_when_the_detail_will_not_fit(self) -> None:
        status = Thinking(Console(width=32, no_color=True), animate=False)
        status.update(phrase="Searching", tokens=1500, detail="web_search")
        out = status.render().plain

        # The verb is the point of the line; the trailing metadata is optional.
        assert "Searching" in out

    def test_full_detail_survives_a_wide_terminal(self) -> None:
        status = Thinking(Console(width=120, no_color=True), animate=False)
        status.update(phrase="Searching", tokens=1500, detail="web_search")
        out = status.render().plain

        assert "Searching" in out
        assert "1.5k tokens" in out
        assert "web_search" in out

    def test_frames_advance(self) -> None:
        status = self._status()
        first = render(status.render())[:3]
        second = render(status.render())[:3]
        assert first != second

    def test_elapsed_grows(self) -> None:
        status = self._status()
        status.state.started -= 5
        assert "5s" in render(status.render())

    def test_animate_off_is_a_no_op_context(self) -> None:
        with self._status() as status:
            assert status._live is None

    def test_animation_starts_and_stops_cleanly(self) -> None:
        console = Console(width=90, force_terminal=True)
        status = Thinking(console, animate=True, interval=0.01)
        with status:
            time.sleep(0.05)
            assert status._thread is not None

        assert status._live is None
        assert status._thread is None

    def test_stop_is_idempotent(self) -> None:
        status = self._status()
        status.stop()
        status.stop()

    def test_no_animation_when_colour_is_disabled(self) -> None:
        # Piping to a file must not emit spinner frames.
        console = Console(width=90, no_color=True, force_terminal=False)
        assert Thinking(console).animate is False

    def test_ascii_frames_on_a_legacy_console(self) -> None:
        status = self._status()
        status._unicode = False
        import itertools

        status._frames = itertools.cycle(ASCII_FRAMES)
        assert render(status.render())[0] in ASCII_FRAMES

    def test_frame_sets_are_distinct(self) -> None:
        assert set(SPINNER_FRAMES) != set(ASCII_FRAMES)


class TestLines:
    def test_tool_line(self) -> None:
        out = render(tool_line("web_search", "query='cats'"))
        assert "web_search" in out
        assert "cats" in out

    def test_tool_line_without_preview(self) -> None:
        assert "list_files" in render(tool_line("list_files", ""))

    def test_result_line_success(self) -> None:
        out = render(result_line("wrote notes.md", ok=True, unicode_ok=True))
        assert "✓" in out
        assert "wrote notes.md" in out

    def test_result_line_failure(self) -> None:
        assert "✗" in render(result_line("boom", ok=False, unicode_ok=True))

    def test_result_line_ascii(self) -> None:
        assert "OK" in render(result_line("done", ok=True, unicode_ok=False))
