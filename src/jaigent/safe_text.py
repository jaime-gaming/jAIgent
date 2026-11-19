"""Text that came from somewhere else, made safe to put on a terminal.

Two different problems, one module.

**Markup.** Rich reads tags in any string it prints: ``[red]`` colours text,
``[bold]`` bolds it, and a path like ``[notes]`` makes it raise
``MarkupError`` and take the call down with it. Everything the agent shows
that came from outside the program goes through here first:
  * a prompt the user typed,
  * a tool argument (the model wrote it),
  * a file's contents,
  * a message from a provider,
  * a tool's error output.
Passing any of those to ``console.print`` as a string means a bracket in a
filename can kill the session, and a model can colour the transcript.

**Control characters.** A file can contain ``\\x1b[2J`` (clear the screen),
``\\x1b]0;title\\x07`` (rewrite the window title) or a carriage return that
overwrites the line just printed. None of that belongs in a transcript. They
are escaped on the way in, so the reader sees the danger instead of obeying
it.

The two rules, in short: markup is escaped everywhere it is not ours, and
control characters never reach the terminal.
"""

from __future__ import annotations

import re

#: Whole ANSI escape sequences, removed before anything else. Stripping only
#: the ESC byte would leave the payload behind as visible debris: a file with
#: colour codes would print "[31m" all over the transcript.
_ANSI = re.compile(
    r"""
    \x1b\[[0-?]*[ -/]*[@-~]        # CSI: cursor moves, colours, clears
    | \x1b\][^\x07\x1b]*(?:\x07|\x1b\\)?  # OSC: window title, hyperlinks
    | \x1b[PX^_][^\x1b]*(?:\x1b\\)?        # DCS/SOS/PM/APC strings
    | \x1b[@-Z\\-_]                 # two-character escapes
    | [\x9b\x9d][0-?]*[ -/]*[@-~]     # 8-bit CSI/OSC from a broken encoding
    """,
    re.VERBOSE,
)

#: Whatever control characters are left once the sequences are gone: anything
#: below 0x20 (tab and newline have already been normalised) and DEL.
_CONTROL = re.compile(r"[\x00-\x08\x0b-\x1f\x7f-\x9f]")

#: How much of a long string is kept when it is only there for context.
DEFAULT_LIMIT = 200


def escape_markup(text: object) -> str:
    """Make ``text`` printable by rich without being interpreted.

    ``[`` becomes ``\\[``, which rich prints as a literal bracket. That is the
    whole trick: the string still reads exactly as it did, and no part of it
    can be mistaken for a directive.

    **Print it with ``highlight=False``.** Rich's automatic highlighter also
    consumes backslashes, so with highlighting on the escape shows up as a
    stray ``\\`` in front of the bracket. Every call site here says
    ``highlight=False`` for that reason.

    ``None`` becomes the empty string, so callers can pass optional values
    without a conditional.
    """
    if text is None:
        return ""
    return str(text).replace("[", "\\[")


def clean(text: object, *, limit: int | None = None, keep_newlines: bool = True) -> str:
    """Escape markup and remove terminal control characters.

    Args:
        text: Anything; it is stringified.
        limit: Keep at most this many characters, ending in ``…``. ``None``
            keeps everything.
        keep_newlines: When false, newlines and tabs collapse to single
            spaces, for a value that has to sit on one line (a table cell, a
            footer).

    Returns:
        A string that is safe to hand to rich as markup, and cannot move the
        cursor.
    """
    # Escape sequences go first: escaping a bracket that belongs to a live
    # sequence would turn "\x1b[2J" into "\x1b\[2J", a clear-screen with a
    # stray backslash in it.
    raw = _ANSI.sub("", "" if text is None else str(text))
    if not keep_newlines:
        raw = " ".join(raw.split())
    else:
        # Normalise CRLF and stray carriage returns: a lone CR moves the cursor
        # back to column zero and lets the next line overwrite this one.
        raw = raw.replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")
    # Whatever control characters are left, now that the two that mean
    # something here, newline and tab, have been dealt with.
    escaped = escape_markup(_CONTROL.sub("", raw))
    if limit is not None and len(escaped) > limit:
        escaped = escaped[: max(0, limit - 1)].rstrip() + "…"
    return escaped


def one_line(text: object, *, limit: int = DEFAULT_LIMIT) -> str:
    """A single safe line: for a footer, a trace line or a notice."""
    return clean(text, limit=limit, keep_newlines=False)


def quote(text: object, *, limit: int = DEFAULT_LIMIT) -> str:
    """``'text'``, escaped and bounded: for naming a value in a message."""
    return f"'{one_line(text, limit=limit)}'"


def printable(text: object, *, keep_newlines: bool = True) -> str:
    """Strip control characters, without escaping brackets.

    For text that rich is *not* going to parse: a ``Text`` object, a raw
    write, a markdown body. Escaping there would be wrong as well as
    unnecessary, because ``\\[`` in a ``Text`` prints a literal backslash.

    Args:
        text: Anything; it is stringified.
        keep_newlines: When false the result is a single line, for a value
            that has to sit inside one row of a trace.
    """
    raw = _ANSI.sub("", str(text))
    raw = raw.replace("\r\n", "\n").replace("\r", "\n").replace("\t", "    ")
    raw = _CONTROL.sub("", raw)
    return raw if keep_newlines else " ".join(raw.split())
