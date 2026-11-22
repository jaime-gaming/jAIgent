"""Mouse and touch support for the interactive picker.

A terminal can report clicks, taps and wheel turns as escape sequences. This
module turns those into the same key names the picker already understands, so a
finger on a phone or a click with a mouse does what an arrow key and Enter do.
Just as importantly, it consumes a report whole, so its digits never leak into
the input as stray keystrokes, which is what happens if a program turns mouse
tracking on and then reads it as if it were typed.

This is part of the same interactive-flourish layer as animation: it is skipped
on a dumb terminal, under CI, with ``--no-animation`` or ``JAIGENT_NO_ANIMATION``,
with ``JAIGENT_NO_MOUSE``, or anywhere a tty is not present. It is never
load-bearing. Every action a report triggers is also reachable from the keyboard,
and if enabling it fails the picker simply carries on without it.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass

from jaigent.splash import animation_disabled

# Private-mode toggles. 1000 reports a button press and release, and the wheel;
# 1006 asks for the SGR encoding, which carries the coordinates as plain decimal
# numbers and so never collides with a byte that could be mistaken for a key.
# Motion tracking (1002/1003) is left off on purpose: nothing here follows a
# drag, and it would flood the reader with events nobody asked for.
MOUSE_ENABLE = "\x1b[?1000h\x1b[?1006h"
MOUSE_DISABLE = "\x1b[?1000l\x1b[?1006l"

# The SGR button codes this acts on.
LEFT_BUTTON = 0
WHEEL_UP = 64
WHEEL_DOWN = 65


@dataclass(frozen=True)
class MouseEvent:
    """One decoded report: which button, where, and a press rather than a release."""

    button: int
    x: int
    y: int
    press: bool


def parse_sgr(sequence: str) -> MouseEvent | None:
    """Decode an SGR mouse report, or return ``None`` if it is not one.

    Args:
        sequence: Bytes decoded to text, expected to start ``ESC [ <``.

    The shape is ``ESC [ < button ; x ; y M`` for a press and the same with a
    trailing ``m`` for a release. Anything else, including a truncated or
    malformed report, is ``None``, which the caller reads as "nothing happened".
    """
    if not sequence.startswith("\x1b[<"):
        return None
    body = sequence[3:]
    if not body or body[-1] not in "Mm":
        return None
    parts = body[:-1].split(";")
    if len(parts) != 3:
        return None
    try:
        button, x, y = (int(part) for part in parts)
    except ValueError:
        return None
    return MouseEvent(button=button, x=x, y=y, press=body[-1] == "M")


def mouse_key(event: MouseEvent | None) -> str:
    """Map a mouse event onto a key name the picker already handles.

    Wheel up and down move the selection; a left press, which is what a tap
    reports, confirms the highlighted option. Everything else (motion, a release,
    a right or middle button) maps to the empty string, which the picker reads as
    "stay where you are".

    The coordinates are ignored on purpose. The panel is drawn inline, so its
    absolute row on the screen is not something the picker can know for certain,
    and guessing which option a click landed on could silently pick the wrong
    one. Moving and confirming never guess: they act on the option that is
    visibly highlighted.
    """
    if event is None or not event.press:
        return ""
    if event.button == WHEEL_UP:
        return "up"
    if event.button == WHEEL_DOWN:
        return "down"
    if event.button == LEFT_BUTTON:
        return "enter"
    return ""


def should_enable_mouse() -> bool:
    """Whether to ask the terminal for mouse reports right now.

    Mirrors the animation gate and adds a switch of its own, so the conditions
    that quiet the motion also quiet the mouse, while somebody who wants one
    without the other can still say so.
    """
    if sys.platform == "win32":
        # No way to read the reports: msvcrt hands back one character at a time,
        # so an SGR report would arrive as the digits of its own coordinates and
        # be read as typed keys. Asking for them would only queue garbage.
        return False
    if os.getenv("JAIGENT_NO_MOUSE"):
        return False
    if animation_disabled():
        return False
    try:
        return bool(sys.stdin.isatty())
    except (OSError, ValueError, AttributeError):
        return False
