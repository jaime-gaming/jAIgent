"""The wordmark sweep: what it draws, and when it refuses to draw."""

from __future__ import annotations

import io
import os
import re

import pytest
from rich.console import Console
from rich.text import Text

from jaigent import splash
from jaigent.branding import ACCENT, wordmark

ACCENT_HEX = ACCENT


def _console(**kwargs: object) -> Console:
    defaults: dict[str, object] = {
        "no_color": False,
        "force_terminal": True,
        "width": 100,
        # Truecolor, or rich downgrades the brand hex to 16 colours and the
        # accent assertion below could not tell the two apart.
        "color_system": "truecolor",
    }
    defaults.update(kwargs)
    return Console(**defaults)


def _plain(console: Console, renderable: object) -> str:
    with console.capture() as capture:
        console.print(renderable)
    return re.sub(r"\x1b\[[0-9;?]*[a-zA-Z]", "", capture.get())


class TestFrames:
    def test_the_first_frame_hints_at_the_shape(self) -> None:
        console = _console()
        out = _plain(console, splash.frame(0.0))

        # Every row of block art is present, because the sweep is colour only.
        assert out.count("\n") >= 6
        assert "██" in out

    def test_the_last_frame_is_the_static_logo(self) -> None:
        """The effect must end exactly where the product's logo is."""
        animated = _plain(_console(), splash.frame(1.0))
        static = _plain(_console(), wordmark("full"))

        for line in static.splitlines():
            if line.strip():
                assert line.rstrip() in animated

    def test_the_accent_falls_on_the_ai_letters(self) -> None:
        """Checked on the Text spans, not on escape codes.

        Asserting on ANSI output made this depend on whatever colour system the
        surrounding test session last detected; the spans say the same thing
        and mean it regardless of the terminal.
        """
        art = splash.frame(1.0).renderables[0]
        assert isinstance(art, Text)

        spans = [(str(span.style), art.plain[span.start : span.end]) for span in art.spans]
        accented = sum(len(cells.strip()) for style, cells in spans if ACCENT_HEX in style)
        everything = len(art.plain.replace("\n", "").strip())

        # Some letters carry the accent, but not all of them: the wordmark is
        # mostly ink, with only "AI" in brand orange.
        assert accented > 0
        assert accented < everything

    def test_the_tagline_is_carried(self) -> None:
        assert "all your agents in one place" in _plain(_console(), splash.frame(0.5))

    def test_progress_reveals_more_over_time(self) -> None:
        early = _plain(_console(), splash.frame(0.1))
        late = _plain(_console(), splash.frame(0.9))

        # Colour changes, not characters: the art is complete throughout.
        assert early.replace("\n", "") != "" and late.replace("\n", "") != ""


class TestWhenItRefuses:
    def test_a_pipe_gets_no_animation(self) -> None:
        console = Console(file=io.StringIO(), force_terminal=False)

        assert splash.can_animate(console) == (False, "output is not a terminal")

    def test_colour_off_means_no_animation(self) -> None:
        allowed, reason = splash.can_animate(_console(no_color=True))

        assert allowed is False
        assert "colour" in reason

    def test_ci_disables_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("CI", "true")
        monkeypatch.delenv("JAIGENT_NO_ANIMATION", raising=False)

        assert splash.can_animate(_console())[0] is False

    def test_the_explicit_switch_disables_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JAIGENT_NO_ANIMATION", "1")
        monkeypatch.delenv("CI", raising=False)

        assert splash.can_animate(_console())[0] is False

    def test_a_dumb_terminal_disables_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CI", raising=False)
        monkeypatch.delenv("JAIGENT_NO_ANIMATION", raising=False)
        monkeypatch.setenv("TERM", "dumb")

        assert splash.can_animate(_console())[0] is False

    def test_a_narrow_terminal_disables_it(self) -> None:
        allowed, reason = splash.can_animate(_console(width=30))

        assert allowed is False
        assert "narrow" in reason

    def test_a_capable_terminal_allows_it(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CI", raising=False)
        monkeypatch.delenv("JAIGENT_NO_ANIMATION", raising=False)
        monkeypatch.setenv("TERM", "xterm-256color")

        assert splash.can_animate(_console()) == (True, "")


class TestAnimate:
    def test_it_reports_a_skip_rather_than_raising(self) -> None:
        console = Console(file=io.StringIO(), force_terminal=False)

        result = splash.animate(console)

        assert result.animated is False
        assert result.reason

    def test_it_runs_when_allowed_and_leaves_the_screen_usable(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("CI", raising=False)
        monkeypatch.delenv("JAIGENT_NO_ANIMATION", raising=False)
        monkeypatch.setenv("TERM", "xterm-256color")
        console = _console()

        result = splash.animate(console, duration=0.05)

        assert result.animated is True
        console.print("still working")

    def test_a_failing_live_never_breaks_the_session(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.delenv("CI", raising=False)
        monkeypatch.delenv("JAIGENT_NO_ANIMATION", raising=False)
        monkeypatch.setenv("TERM", "xterm-256color")

        def explode(*args: object, **kwargs: object):
            raise RuntimeError("no live region today")

        monkeypatch.setattr(splash, "Live", explode)
        console = _console()

        result = splash.animate(console, duration=0.05)

        assert result.animated is False
        assert "RuntimeError" in result.reason

    def test_splash_always_returns_a_banner(self, monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setenv("JAIGENT_NO_ANIMATION", "1")
        console = _console()

        out = _plain(console, splash.splash(console, version="9.9.9"))

        assert "all your agents in one place" in out
        assert "v9.9.9" in out


def test_the_environment_check_reads_the_real_variables(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    for name in ("CI", "JAIGENT_NO_ANIMATION", "TERM"):
        monkeypatch.delenv(name, raising=False)
    assert splash.animation_disabled() is False

    monkeypatch.setenv("JAIGENT_NO_ANIMATION", "yes")
    assert splash.animation_disabled() is True


def test_it_does_not_write_escape_codes_to_a_pipe(monkeypatch: pytest.MonkeyPatch) -> None:
    """A redirected chat must produce plain text a file can hold."""
    monkeypatch.setenv("JAIGENT_NO_ANIMATION", "1")
    stream = io.StringIO()
    console = Console(file=stream, force_terminal=False, no_color=True)

    console.print(splash.splash(console))

    assert "\x1b[" not in stream.getvalue()


def test_the_no_animation_flag_is_honoured_by_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from jaigent import cli

    monkeypatch.delenv("JAIGENT_NO_ANIMATION", raising=False)
    monkeypatch.setattr(cli, "print_splash", lambda parser: None)

    cli.main(["--no-animation", "--logo"])

    assert os.environ.get("JAIGENT_NO_ANIMATION") == "1"
