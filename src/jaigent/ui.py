"""Animated terminal UI: spinners, status lines and the phrase pool.

The live status line is the thing you stare at while the agent works, so it
earns its keep: an animated glyph, a rotating verb, elapsed time and a running
token count, all on one line that erases itself when the turn ends.

Everything degrades gracefully. On a dumb terminal, under ``--no-color``, or on
a Windows console without Unicode, the animation is replaced by plain periodic
text and the fancy glyphs fall back to ASCII.
"""

from __future__ import annotations

import itertools
import random
import sys
import threading
import time
from dataclasses import dataclass, field
from types import TracebackType

from rich.console import Console
from rich.live import Live
from rich.text import Text

from jaigent.branding import ACCENT, MUTED

# ---------------------------------------------------------------------------
# Phrases
#
# Present-participle verbs, shown one at a time while the model is working. They
# exist to make waiting feel intentional rather than broken. Keep them short,
# gently absurd, and never implying a specific action the agent is not taking.
# ---------------------------------------------------------------------------
PHRASES: tuple[str, ...] = (
    "Thinking",
    "Pondering",
    "Musing",
    "Ruminating",
    "Cogitating",
    "Noodling",
    "Percolating",
    "Deliberating",
    "Considering",
    "Puzzling",
    "Scheming",
    "Conjuring",
    "Wrangling",
    "Untangling",
    "Assembling",
    "Rummaging",
    "Spelunking",
    "Marinating",
    "Simmering",
    "Brewing",
    "Whirring",
    "Computing",
    "Deducing",
    "Inferring",
    "Reticulating",
    "Herding",
    "Corralling",
    "Finessing",
    "Tinkering",
    "Contemplating",
)

#: Shown while a tool is running, keyed by tool name.
TOOL_PHRASES: dict[str, str] = {
    "web_search": "Searching",
    "fetch_page": "Reading",
    "read_file": "Reading",
    "list_files": "Looking around",
    "search_files": "Grepping",
    "write_file": "Writing",
    "edit_file": "Editing",
    "delete_file": "Deleting",
    "run_command": "Running",
    "load_skill": "Recalling",
}

#: Frames for the animated glyph, in the spirit of Claude Code's asterisk.
SPINNER_FRAMES: tuple[str, ...] = ("✢", "✳", "∗", "✻", "✽", "✻", "∗", "✳")
ASCII_FRAMES: tuple[str, ...] = ("-", "\\", "|", "/")

#: Unicode decorations with ASCII fallbacks for legacy consoles.
GLYPHS: dict[str, tuple[str, str]] = {
    "bullet": ("·", "-"),
    "arrow": ("→", "->"),
    "check": ("✓", "OK"),
    "cross": ("✗", "x"),
    "warn": ("⚠", "!"),
    "prompt": ("❯", ">"),
    "ellipsis": ("…", "..."),
}


def supports_unicode(stream: object | None = None) -> bool:
    """Whether the output encoding can render the fancy glyphs.

    Windows consoles still default to code pages that cannot encode ``✻``, and
    printing one raises ``UnicodeEncodeError`` mid-render. Detect it up front.
    """
    target = stream if stream is not None else sys.stdout
    encoding = getattr(target, "encoding", None) or ""
    if not encoding:
        return False
    try:
        "✻→✓❯".encode(encoding)
    except (UnicodeEncodeError, LookupError):
        return False
    return True


def glyph(name: str, *, unicode_ok: bool | None = None) -> str:
    """Return a decoration, falling back to ASCII where necessary."""
    fancy, plain = GLYPHS[name]
    ok = supports_unicode() if unicode_ok is None else unicode_ok
    return fancy if ok else plain


def pick_phrase(exclude: str | None = None) -> str:
    """A random working verb, avoiding an immediate repeat."""
    options = [p for p in PHRASES if p != exclude] or list(PHRASES)
    return random.choice(options)


def format_duration(seconds: float) -> str:
    """``4s``, ``1m 20s``."""
    total = int(seconds)
    if total < 60:
        return f"{total}s"
    minutes, rest = divmod(total, 60)
    return f"{minutes}m {rest}s" if rest else f"{minutes}m"


def format_tokens(count: int) -> str:
    """``820``, ``1.2k``, ``15.3k``."""
    if count < 1000:
        return str(count)
    return f"{count / 1000:.1f}k".replace(".0k", "k")


@dataclass
class StatusState:
    """Everything the status line renders."""

    phrase: str = "Thinking"
    started: float = field(default_factory=time.monotonic)
    tokens: int = 0
    detail: str = ""

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started


class Thinking:
    """An animated status line shown while the agent works.

    Renders as ``✻ Pondering… (4s · ↑ 1.2k tokens · web_search)`` and updates in
    place. Use it as a context manager; it always cleans up after itself, even
    if the body raises.

    Args:
        console: Where to draw.
        animate: Force animation on or off. Defaults to animating only on a
            real terminal with colour enabled — never when piped to a file.
        interval: Seconds between frames.
        phrase_every: Seconds before switching to a different verb.
    """

    def __init__(
        self,
        console: Console,
        *,
        animate: bool | None = None,
        interval: float = 0.12,
        phrase_every: float = 4.0,
    ) -> None:
        self.console = console
        self.interval = interval
        self.phrase_every = phrase_every
        self.state = StatusState(phrase=pick_phrase())

        if animate is None:
            animate = console.is_terminal and not console.no_color
        self.animate = bool(animate)

        self._unicode = supports_unicode(getattr(console, "file", None))
        self._frames = itertools.cycle(SPINNER_FRAMES if self._unicode else ASCII_FRAMES)
        self._live: Live | None = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self._last_phrase_change = time.monotonic()

    # ------------------------------------------------------------------
    def render(self) -> Text:
        """Build the current status line."""
        line = Text()
        line.append(f"{next(self._frames)} ", style=ACCENT)
        line.append(self.state.phrase, style=ACCENT)
        line.append(glyph("ellipsis", unicode_ok=self._unicode), style=ACCENT)

        bits = [format_duration(self.state.elapsed)]
        if self.state.tokens:
            up = "↑" if self._unicode else "^"
            bits.append(f"{up} {format_tokens(self.state.tokens)} tokens")
        if self.state.detail:
            bits.append(self.state.detail)

        sep = f" {glyph('bullet', unicode_ok=self._unicode)} "
        line.append(f"  ({sep.join(bits)})", style=MUTED)
        return line

    # ------------------------------------------------------------------
    def update(
        self, *, phrase: str | None = None, tokens: int | None = None, detail: str | None = None
    ) -> None:
        """Change what the line says. Safe to call from any thread."""
        with self._lock:
            if phrase is not None:
                self.state.phrase = phrase
                self._last_phrase_change = time.monotonic()
            if tokens is not None:
                self.state.tokens = tokens
            if detail is not None:
                self.state.detail = detail

    def tool_started(self, name: str) -> None:
        """Switch the verb to match the tool now running."""
        self.update(phrase=TOOL_PHRASES.get(name, "Working"), detail=name)

    def thinking_again(self) -> None:
        """Back to a generic verb once a tool has finished."""
        self.update(phrase=pick_phrase(self.state.phrase), detail="")

    # ------------------------------------------------------------------
    def _spin(self) -> None:
        while not self._stop.is_set():
            now = time.monotonic()
            with self._lock:
                stale = now - self._last_phrase_change > self.phrase_every
                idle = not self.state.detail
            if stale and idle:
                self.update(phrase=pick_phrase(self.state.phrase))
            if self._live is not None:
                self._live.update(self.render())
            self._stop.wait(self.interval)

    def start(self) -> Thinking:
        if not self.animate:
            return self
        self._live = Live(
            self.render(),
            console=self.console,
            refresh_per_second=1 / self.interval,
            transient=True,
        )
        self._live.start()
        self._thread = threading.Thread(target=self._spin, daemon=True)
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=1.0)
            self._thread = None
        if self._live is not None:
            self._live.stop()
            self._live = None

    def __enter__(self) -> Thinking:
        return self.start()

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.stop()


def tool_line(name: str, preview: str, *, unicode_ok: bool | None = None) -> Text:
    """One line describing a tool call, for verbose mode."""
    line = Text()
    line.append(f"  {glyph('arrow', unicode_ok=unicode_ok)} ", style=ACCENT)
    line.append(name, style=f"bold {ACCENT}")
    if preview:
        line.append(f" {preview}", style=MUTED)
    return line


def result_line(text: str, *, ok: bool = True, unicode_ok: bool | None = None) -> Text:
    """One line summarising an outcome."""
    mark = glyph("check" if ok else "cross", unicode_ok=unicode_ok)
    line = Text()
    line.append(f"  {mark} ", style="green" if ok else "red")
    line.append(text, style=MUTED)
    return line
