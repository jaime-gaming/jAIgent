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

from rich import box as _box
from rich.align import Align
from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.panel import Panel
from rich.text import Text

from jaigent.branding import (
    ACCENT,
    ACCENT_DIM,
    ACCENT_LETTERS,
    FULL_GLYPHS,
    INK,
    MUTED,
    TAGLINE,
)
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


def wordmark(progress: float, *, version: str = "") -> Group:
    """The logo text for ``progress``, styled as the sweep reaches each cell.

    Kept separate from :func:`frame` so tests can assert on spans rather than
    on escape codes.
    """
    rows = _wordmark_rows()
    spans = _letter_spans()
    width = max(len(row) for row in rows)
    edge = progress * (width + 12) - 6
    band = 6.0
    accented = [spans[index] for index in ACCENT_LETTERS]

    art = Text()
    for row_index, row in enumerate(rows):
        for column, cell in enumerate(row.ljust(width)):
            if cell == " ":
                art.append(" ")
                continue
            distance = edge - column
            is_accent = any(start <= column < end for start, end in accented)
            if distance < 0:
                # Not reached yet: hinted, not hidden, so the wordmark is
                # recognisable from the very first frame.
                art.append(cell, style=MUTED)
            elif distance < band:
                # The leading edge itself: the brightest point of the sweep.
                art.append(cell, style=f"bold {ACCENT}")
            else:
                art.append(cell, style=f"bold {ACCENT}" if is_accent else f"bold {INK}")
        if row_index != len(rows) - 1:
            art.append("\n")

    caption = Text(justify="center")
    caption.append(TAGLINE, style=MUTED)
    if version:
        caption.append(f"   v{version}", style=MUTED)
    return Group(Align.center(art), Align.center(caption))


def frame(progress: float, *, version: str = "") -> RenderableType:
    """One frame of the sweep, ``progress`` in ``0..1``.

    Every frame — including the last — is wrapped in the same panel the static
    banner uses. That is not decoration: a live region that grows between
    frames leaves the earlier, shorter frames stranded on screen, which is
    exactly what the first version of this did.
    """
    return Panel(
        wordmark(progress, version=version),
        border_style=ACCENT_DIM,
        box=_box.ROUNDED,
        padding=(0, 2),
    )


def animate(console: Console, *, version: str = "", duration: float = DURATION) -> SplashResult:
    """Sweep the wordmark once and leave the finished banner in its place.

    The live region is *not* transient. The last frame it draws is the real
    banner, so when the region ends the thing on screen is already the final
    layout and there is nothing to erase. The previous version ended a
    transient region and printed the banner afterwards, which looked right in
    a string test and left a column of half-drawn frames on a real terminal.

    Never raises: any failure leaves the console usable, and the caller prints
    the banner itself in that case.
    """
    from jaigent.branding import render_banner

    allowed, reason = can_animate(console)
    if not allowed:
        return SplashResult(False, reason)

    steps = max(6, int(duration * FPS))
    banner = render_banner(console, version=version)
    try:
        with Live(frame(0.0, version=version), console=console, refresh_per_second=FPS) as live:
            for step in range(1, steps + 1):
                live.update(frame(step / steps, version=version))
                time.sleep(duration / steps)
            # The landed frame is the product's own banner, not a lookalike.
            live.update(banner)
            time.sleep(0.05)
    except (KeyboardInterrupt, EOFError):
        # Interrupted mid-sweep: whatever is on screen stays; the caller still
        # has the banner to print, so nothing is lost.
        return SplashResult(False, "interrupted")
    except Exception as exc:  # noqa: BLE001 - an effect must never break a session
        return SplashResult(False, f"animation failed ({type(exc).__name__})")
    return SplashResult(True)


def splash(
    console: Console, *, version: str = "", duration: float = DURATION
) -> RenderableType | None:
    """Animate if possible. Returns a banner only when one still needs printing.

    ``None`` means "already on screen" — the caller prints nothing and the
    screen ends up identical either way.
    """
    from jaigent.branding import render_banner

    if animate(console, version=version, duration=duration).animated:
        return None
    return render_banner(console, version=version)
