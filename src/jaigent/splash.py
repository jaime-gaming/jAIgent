"""The moment jAIgent appears.

``jaigent chat`` used to print the finished logo and jump straight to a
prompt: correct, and forgettable. This draws it instead — a bright edge sweeps
across the wordmark once and settles into the idle colours — so the first half
second says "something is running" before the user has typed anything.

The rules that make an effect safe to ship here:

* **Opt-in by capability, never by hope.** It animates only when the console is
  a real terminal, colour is on, the glyph set can be encoded, the logo fits,
  and no ``JAIGENT_NO_ANIMATION``/``CI`` says otherwise.
* **One second, once, overridable.** :data:`DURATION` is short on purpose: a
  splash that makes you wait is worse than no splash.
* **Always lands on the final frame.** Any interruption (Ctrl-C, a resize, an
  exception mid-render) leaves the static logo on screen, because the caller
  prints it afterwards regardless.
* **The last frame is the static logo.** The sweep only decides *which cells
  are already lit*; colour and layout come from ``branding`` so the two can
  never drift apart.
"""

from __future__ import annotations

import os
import sys
import time
from dataclasses import dataclass

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.text import Text

from jaigent.branding import ACCENT, ACCENT_LETTERS, FULL_GLYPHS, INK, MUTED, TAGLINE
from jaigent.ui import supports_unicode

#: How long the sweep runs, in seconds. Long enough to read as motion, short
#: enough that nobody taps their foot.
DURATION = 0.55
#: Frames per second while sweeping.
FPS = 45
#: Blank columns between letters, matching ``branding.wordmark``.
GAP = 1


@dataclass(slots=True)
class SplashResult:
    """What the caller should know after trying to animate."""

    animated: bool
    #: Why animation was skipped, for ``--verbose``. Empty when it ran.
    reason: str = ""


def animation_disabled() -> bool:
    """Whether the environment asks for no motion.

    ``JAIGENT_NO_ANIMATION`` is the explicit switch; ``CI`` and a dumb terminal
    are the polite defaults, because a sweep in a build log is just noise.
    """
    if os.getenv("JAIGENT_NO_ANIMATION"):
        return True
    if os.getenv("CI"):
        return True
    return os.getenv("TERM", "").strip().lower() == "dumb"


def can_animate(console: Console) -> tuple[bool, str]:
    """Whether a sweep would be welcome here, and why not when it would not."""
    if console.no_color:
        return False, "colour is off"
    if not console.is_terminal:
        return False, "output is not a terminal"
    if animation_disabled():
        return False, "animation disabled (JAIGENT_NO_ANIMATION/CI)"
    stream = getattr(console, "file", sys.stdout)
    if not supports_unicode(stream):
        return False, "the console cannot encode the wordmark"
    if console.width < 44:
        return False, "the terminal is too narrow"
    return True, ""


def _wordmark_rows() -> list[str]:
    """The logo as plain rows, joined exactly like ``branding.wordmark``."""
    spacer = " " * GAP
    return [spacer.join(letter[row] for letter in FULL_GLYPHS) for row in range(_height())]


def _height() -> int:
    return len(FULL_GLYPHS[0])


def _letter_spans() -> list[tuple[int, int]]:
    """``(start, end)`` column for every letter, so the sweep can style to it.

    The letters are not a uniform width, so the ranges are computed rather than
    assumed: guessing produced a wordmark with its letters jammed together.
    """
    spans: list[tuple[int, int]] = []
    column = 0
    for index, letter in enumerate(FULL_GLYPHS):
        if index:
            column += GAP
        spans.append((column, column + len(letter[0])))
        column += len(letter[0])
    return spans


def frame(progress: float) -> Group:
    """One frame of the sweep, ``progress`` in ``0..1``.

    A bright edge travels left to right. Everything behind it is already the
    final wordmark — including the accent on ``AI`` — so the last frame is
    exactly what the static logo prints.
    """
    rows = _wordmark_rows()
    spans = _letter_spans()
    width = max(len(row) for row in rows)
    edge = progress * (width + 12) - 6
    band = 6.0

    art = Text()
    for row_index, row in enumerate(rows):
        for column, cell in enumerate(row.ljust(width)):
            if cell == " ":
                art.append(" ")
                continue
            distance = edge - column
            accented = any(
                start <= column < end for start, end in (spans[index] for index in ACCENT_LETTERS)
            )
            if distance < 0:
                # Not reached yet: hinted, not hidden, so the wordmark is
                # recognisable from the first frame.
                art.append(cell, style=MUTED)
            elif distance < band:
                # The leading edge itself: the brightest point of the sweep.
                art.append(cell, style=f"bold {ACCENT}")
            else:
                art.append(cell, style=f"bold {ACCENT}" if accented else f"bold {INK}")
        if row_index != len(rows) - 1:
            art.append("\n")

    caption = Text(justify="center")
    caption.append(TAGLINE, style=MUTED)
    return Group(art, caption)


def animate(console: Console, *, duration: float = DURATION) -> SplashResult:
    """Sweep the wordmark once, in place. Returns whether it actually ran.

    Never raises: any failure leaves the console usable, and the caller is
    expected to print the static logo afterwards either way.
    """
    allowed, reason = can_animate(console)
    if not allowed:
        return SplashResult(False, reason)

    steps = max(6, int(duration * FPS))
    try:
        with Live(frame(0.0), console=console, refresh_per_second=FPS, transient=True) as live:
            for step in range(1, steps + 1):
                live.update(frame(step / steps))
                time.sleep(duration / steps)
            # Hold the final frame briefly so the eye lands on the real logo.
            time.sleep(0.08)
    except (KeyboardInterrupt, EOFError):
        # Interrupted: leave nothing half-drawn. The caller prints the logo.
        return SplashResult(False, "interrupted")
    except Exception as exc:  # noqa: BLE001 - an effect must never break a session
        return SplashResult(False, f"animation failed ({type(exc).__name__})")
    return SplashResult(True)


def splash(console: Console, *, version: str = "", duration: float = DURATION) -> RenderableType:
    """Try to animate, then return the banner to print afterwards.

    The static renderable is always returned, so a caller that ignores the
    result still shows a correct logo.
    """
    from jaigent.branding import render_banner

    animate(console, duration=duration)
    return render_banner(console, version=version)
