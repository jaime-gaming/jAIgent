"""The logo: geometry, colour discipline and responsive sizing."""

from __future__ import annotations

import pytest
from rich.console import Console

from jaigent.branding import (
    ACCENT_LETTERS,
    ACCENT_STYLE,
    BASE_STYLE,
    COMPACT_GLYPHS,
    FULL_GLYPHS,
    SIZES,
    logo_width,
    mini_wordmark,
    pick_size,
    render_banner,
    render_logo,
    wordmark,
)


def render(renderable, width: int = 100, color: bool = False) -> str:
    """Render to a string the way a terminal of ``width`` columns would."""
    console = Console(width=width, no_color=not color, force_terminal=color, legacy_windows=False)
    with console.capture() as capture:
        console.print(renderable)
    return capture.get()


class TestGlyphGeometry:
    """Misaligned glyphs produce a visibly broken logo, so assert the shape."""

    @pytest.mark.parametrize("size", ["full", "compact"])
    def test_every_letter_has_the_same_height(self, size: str) -> None:
        heights = {len(glyph) for glyph in SIZES[size]}
        assert len(heights) == 1, f"{size} glyphs disagree on height: {heights}"

    @pytest.mark.parametrize("size", ["full", "compact"])
    def test_each_letter_is_rectangular(self, size: str) -> None:
        for index, glyph in enumerate(SIZES[size]):
            widths = {len(row) for row in glyph}
            assert len(widths) == 1, f"{size} letter {index} has ragged rows: {widths}"

    def test_wordmark_has_seven_letters(self) -> None:
        assert len(FULL_GLYPHS) == len("jaigent")
        assert len(COMPACT_GLYPHS) == len("jaigent")

    def test_full_is_six_rows_compact_is_three(self) -> None:
        assert len(FULL_GLYPHS[0]) == 6
        assert len(COMPACT_GLYPHS[0]) == 3


class TestWidth:
    def test_reported_width_matches_what_is_printed(self) -> None:
        for size in ("full", "compact"):
            lines = [ln for ln in render(wordmark(size), width=200).splitlines() if ln.strip()]
            assert max(len(line) for line in lines) == logo_width(size)

    def test_full_is_wider_than_compact(self) -> None:
        assert logo_width("full") > logo_width("compact")

    def test_gap_widens_the_wordmark(self) -> None:
        assert logo_width("full", gap=2) > logo_width("full", gap=1)


class TestRendering:
    def test_all_rows_are_present(self) -> None:
        out = render(wordmark("full"))
        assert len([ln for ln in out.splitlines() if ln.strip()]) == 6

    def test_letters_are_separated_by_the_gap(self) -> None:
        narrow = render(wordmark("full", gap=1), width=200)
        wide = render(wordmark("full", gap=3), width=200)
        assert len(wide.splitlines()[0]) > len(narrow.splitlines()[0])

    def test_mini_reads_as_jaigent(self) -> None:
        assert "jaigent" in render(mini_wordmark())


class TestColour:
    def test_ai_is_accented_and_the_rest_is_not(self) -> None:
        text = wordmark("full")
        styles = {span.style for span in text.spans}
        assert ACCENT_STYLE in styles
        assert BASE_STYLE in styles

    def test_accent_covers_exactly_the_ai_letters(self) -> None:
        # One span per letter per row; accent spans must equal accent letters x rows.
        text = wordmark("full")
        accent_spans = [s for s in text.spans if s.style == ACCENT_STYLE]
        assert len(accent_spans) == len(ACCENT_LETTERS) * len(FULL_GLYPHS[0])

    def test_accent_letters_are_the_a_and_the_i(self) -> None:
        assert ACCENT_LETTERS == (1, 2)  # j-A-I-gent

    def test_color_false_produces_no_styles(self) -> None:
        assert wordmark("full", color=False).spans == []

    def test_no_ansi_when_console_has_color_disabled(self) -> None:
        console = Console(width=100, no_color=True)
        out = render(render_logo(console, version="9.9.9"))
        assert "\x1b[" not in out

    def test_ansi_present_on_a_colour_terminal(self) -> None:
        console = Console(width=100, force_terminal=True)
        out = render(render_logo(console, version="9.9.9"), color=True)
        assert "\x1b[" in out


class TestResponsiveSizing:
    @pytest.mark.parametrize(
        ("width", "expected"),
        [
            (200, "full"),
            (100, "full"),
            (65, "full"),
            (60, "compact"),
            (30, "compact"),
            (20, "mini"),
        ],
    )
    def test_pick_size(self, width: int, expected: str) -> None:
        assert pick_size(width) == expected

    def test_logo_never_exceeds_the_terminal(self) -> None:
        for width in (200, 100, 64, 50, 30, 20, 10):
            console = Console(width=width, no_color=True)
            out = render(render_logo(console, version="0.1.0"), width=width)
            longest = max((len(line) for line in out.splitlines()), default=0)
            assert longest <= width, f"logo overflows at width {width}: {longest}"

    def test_explicit_size_overrides_detection(self) -> None:
        console = Console(width=200, no_color=True)
        out = render(render_logo(console, size="compact"), width=200)
        assert "┬" in out

    def test_tiny_terminal_falls_back_to_one_line(self) -> None:
        console = Console(width=20, no_color=True)
        out = render(render_logo(console, version="0.1.0"), width=20)
        assert len([ln for ln in out.splitlines() if ln.strip()]) == 1


class TestContent:
    def test_version_is_shown(self) -> None:
        console = Console(width=100, no_color=True)
        assert "1.2.3" in render(render_logo(console, version="1.2.3"))

    def test_version_is_optional(self) -> None:
        console = Console(width=100, no_color=True)
        assert "v" not in render(render_logo(console)).split("\n")[-3]

    def test_tagline_is_shown(self) -> None:
        console = Console(width=100, no_color=True)
        assert "searches the web" in render(render_logo(console))

    def test_readme_uses_the_block_wordmark(self) -> None:
        from pathlib import Path

        readme = Path(__file__).resolve().parents[1] / "README.md"
        text = readme.read_text(encoding="utf-8")
        assert "██╗" in text
        assert "searches the web · writes your files" in text
        assert "    ##    ##  ####" not in text

    def test_subtitle_is_shown(self) -> None:
        console = Console(width=100, no_color=True)
        out = render(render_logo(console, subtitle="openai/gpt-4o-mini"))
        assert "openai/gpt-4o-mini" in out


class TestBanner:
    def test_banner_is_framed(self) -> None:
        console = Console(width=100, no_color=True)
        out = render(render_banner(console, version="0.1.0"), width=100)
        assert "╭" in out and "╯" in out

    def test_banner_contains_the_logo_and_subtitle(self) -> None:
        console = Console(width=100, no_color=True)
        out = render(render_banner(console, version="0.1.0", subtitle="ws: /tmp"), width=100)
        assert "█" in out
        assert "ws: /tmp" in out

    def test_banner_fits_the_terminal(self) -> None:
        for width in (100, 80, 64, 40):
            console = Console(width=width, no_color=True)
            out = render(render_banner(console, version="0.1.0"), width=width)
            assert max(len(line) for line in out.splitlines()) <= width
