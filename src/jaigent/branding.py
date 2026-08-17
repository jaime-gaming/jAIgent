"""The jaigent logo and other brand furniture.

The wordmark is stored as per-letter glyph blocks rather than as flat lines of
text. That costs a few extra lines here, but it means the accent colour can fall
exactly on the ``ai`` in j-**ai**-gent at any size, and the width is computed
rather than hand-counted.

Three sizes are available, picked automatically by :func:`render_logo` based on
the terminal width:

``full``
    Six-row block letters. The front door: shown by ``jaigent`` with no arguments.
``compact``
    Three-row line-drawing letters, for narrow terminals.
``mini``
    A single styled line, for when there is no room at all.
"""

from __future__ import annotations

from rich.align import Align
from rich.console import Console, Group, RenderableType
from rich.panel import Panel
from rich.text import Text

#: Colour of the wordmark's body.
BASE_STYLE = "bold cyan"
#: Colour of the ``ai`` in j-ai-gent.
ACCENT_STYLE = "bold magenta"
#: Letters that get the accent colour.
ACCENT_LETTERS = (1, 2)

TAGLINE = "searches the web · writes your files"

# ---------------------------------------------------------------------------
# Glyphs: one entry per letter of "jaigent", six rows each.
# ---------------------------------------------------------------------------
FULL_GLYPHS: tuple[tuple[str, ...], ...] = (
    (  # j
        "     ██╗",
        "     ██║",
        "     ██║",
        "██   ██║",
        "╚█████╔╝",
        " ╚════╝ ",
    ),
    (  # a
        " █████╗ ",
        "██╔══██╗",
        "███████║",
        "██╔══██║",
        "██║  ██║",
        "╚═╝  ╚═╝",
    ),
    (  # i
        "██╗",
        "██║",
        "██║",
        "██║",
        "██║",
        "╚═╝",
    ),
    (  # g
        " ██████╗ ",
        "██╔════╝ ",
        "██║  ███╗",
        "██║   ██║",
        "╚██████╔╝",
        " ╚═════╝ ",
    ),
    (  # e
        "███████╗",
        "██╔════╝",
        "█████╗  ",
        "██╔══╝  ",
        "███████╗",
        "╚══════╝",
    ),
    (  # n
        "███╗   ██╗",
        "████╗  ██║",
        "██╔██╗ ██║",
        "██║╚██╗██║",
        "██║ ╚████║",
        "╚═╝  ╚═══╝",
    ),
    (  # t
        "████████╗",
        "╚══██╔══╝",
        "   ██║   ",
        "   ██║   ",
        "   ██║   ",
        "   ╚═╝   ",
    ),
)

COMPACT_GLYPHS: tuple[tuple[str, ...], ...] = (
    (" ┬", " │", "└┘"),  # j
    ("┌─┐", "├─┤", "┴ ┴"),  # a
    ("┬", "│", "┴"),  # i
    ("┌─┐", "│ ┬", "└─┘"),  # g
    ("┌─┐", "├─ ", "└─┘"),  # e
    ("┌┐┌", "│││", "┘└┘"),  # n
    ("┌┬┐", " │ ", " ┴ "),  # t
)

#: Height in rows of each size, used to decide what fits.
SIZES = {"full": FULL_GLYPHS, "compact": COMPACT_GLYPHS}


def logo_width(size: str = "full", gap: int = 1) -> int:
    """Total printed width of the wordmark at ``size``."""
    glyphs = SIZES[size]
    return sum(len(g[0]) for g in glyphs) + gap * (len(glyphs) - 1)


def wordmark(size: str = "full", *, gap: int = 1, color: bool = True) -> Text:
    """Render the ``jaigent`` wordmark as a rich :class:`~rich.text.Text`.

    Args:
        size: ``"full"`` or ``"compact"``.
        gap: Blank columns between letters.
        color: Apply brand colours. When false the art is returned unstyled,
            which is what piping to a file or ``--no-color`` should produce.
    """
    glyphs = SIZES[size]
    spacer = " " * gap
    text = Text()

    for row in range(len(glyphs[0])):
        for index, glyph in enumerate(glyphs):
            if index:
                text.append(spacer)
            style = ACCENT_STYLE if index in ACCENT_LETTERS else BASE_STYLE
            text.append(glyph[row], style=style if color else "")
        text.append("\n")

    text.rstrip()
    return text


def mini_wordmark(*, color: bool = True) -> Text:
    """The one-line fallback: ``▸ jaigent``."""
    text = Text()
    text.append("▸ ", style="bold cyan" if color else "")
    text.append("j", style=BASE_STYLE if color else "")
    text.append("ai", style=ACCENT_STYLE if color else "")
    text.append("gent", style=BASE_STYLE if color else "")
    return text


def pick_size(width: int) -> str:
    """Choose the largest wordmark that fits in ``width`` columns."""
    if width >= logo_width("full") + 4:
        return "full"
    if width >= logo_width("compact") + 4:
        return "compact"
    return "mini"


def render_logo(
    console: Console,
    *,
    version: str = "",
    subtitle: str = "",
    size: str | None = None,
    color: bool | None = None,
) -> RenderableType:
    """Build the full brand block: wordmark, tagline and optional subtitle.

    The size adapts to the terminal unless ``size`` is given, and colour is
    dropped automatically when the console has it disabled (``--no-color``, or
    output redirected to a file).
    """
    use_color = (not console.no_color) if color is None else color
    chosen = size or pick_size(console.width)
    dim = "dim" if use_color else ""

    if chosen == "mini":
        line = mini_wordmark(color=use_color)
        if version:
            line.append(f"  v{version}", style=dim)
        return line

    art = wordmark(chosen, color=use_color)

    caption = Text(justify="center")
    caption.append(TAGLINE, style=dim)
    if version:
        caption.append(f"   v{version}", style=dim)

    parts: list[RenderableType] = [Align.center(art), Align.center(caption)]
    if subtitle:
        parts.append(Align.center(Text(subtitle, style=dim)))

    return Group(*parts)


def render_banner(
    console: Console,
    *,
    version: str = "",
    subtitle: str = "",
    color: bool | None = None,
) -> RenderableType:
    """The logo inside a panel — used as the chat session header."""
    use_color = (not console.no_color) if color is None else color
    logo = render_logo(console, version=version, subtitle=subtitle, color=use_color)
    return Panel(
        logo,
        border_style="cyan" if use_color else "none",
        padding=(1, 2),
    )
