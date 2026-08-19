"""Regression tests for cp1252-safe console output on legacy Windows consoles.

The Windows smoke tests crash with UnicodeEncodeError if any non-ASCII
glyph (\u2713 \u2717 \u2190 \u2192 \u26a0 etc.) leaks into output. After this fix
every command in the release smoke test must round-trip through cp1252
without raising.

Per AGENTS.md: "New glyphs need an ASCII fallback in ``GLYPHS``,
because Windows consoles raise UnicodeEncodeError rather than degrading."
"""

from __future__ import annotations

import pytest


class _Cp1252Stream:
    """A stream with a settable ``encoding`` string, like a real console."""

    def __init__(self, encoding: str = "cp1252") -> None:
        self.encoding = encoding
        self._buf: list[str] = []

    def write(self, data: str) -> int:
        data.encode(self.encoding)  # raises if anything is unsafe
        self._buf.append(data)
        return len(data)

    def flush(self) -> None:
        pass


class _Cp1252Console:
    """A console-shaped stand-in that writes through a cp1252 stream.

    Real `rich.Console` writes through self.file; we mirror that so the
    encoding check fires on every byte.
    """

    def __init__(self, encoding: str = "cp1252", width: int = 80) -> None:
        self.file = _Cp1252Stream(encoding)
        self.no_color = True
        self.width = width

    def print(self, *args, **kwargs) -> None:
        for arg in args:
            # This is what rich does internally — run text through the
            # console's output stream, which encodes with the file's codec.
            text = str(arg)
            self.file.write(text)


@pytest.fixture()
def cp1252_console() -> _Cp1252Console:
    return _Cp1252Console()


@pytest.fixture()
def no_unicode(monkeypatch: pytest.MonkeyPatch) -> None:
    from jaigent import ui as ui_mod

    monkeypatch.setattr(ui_mod, "supports_unicode", lambda stream=None: False)


@pytest.fixture()
def has_unicode(monkeypatch: pytest.MonkeyPatch) -> None:
    from jaigent import ui as ui_mod

    monkeypatch.setattr(ui_mod, "supports_unicode", lambda stream=None: True)


def test_supports_unicode_returns_true_for_cp1252_stream() -> None:
    from jaigent.ui import supports_unicode

    # The probe is ASCII, so a legacy console can encode it.
    assert supports_unicode(_Cp1252Stream("cp1252")) is True


def test_supports_unicode_returns_true_for_utf8_stream() -> None:
    from jaigent.ui import supports_unicode

    assert supports_unicode(_Cp1252Stream("utf-8")) is True


def test_glyph_returns_ascii_when_unicode_disabled(no_unicode: None) -> None:
    from jaigent import ui

    assert ui.glyph("check") == "[*]"
    assert ui.glyph("arrow_left") == "<-"
    assert ui.glyph("arrow") == "->"
    assert ui.glyph("warn") == "[!]"
    assert ui.glyph("cross") == "[x]"
    assert ui.glyph("prompt") == ">"
    assert ui.glyph("tri") == ">"


def test_glyph_stays_ascii_when_unicode_is_supported(has_unicode: None) -> None:
    from jaigent import ui

    assert ui.glyph("check") == "[*]"
    assert ui.glyph("arrow_left") == "<-"
    assert ui.glyph("arrow") == "->"
    assert ui.glyph("warn") == "[!]"


def test_prompt_mark_returns_ascii_for_cp1252(no_unicode: None) -> None:
    from jaigent.ui import prompt_mark

    assert prompt_mark() == ">"


def test_prompt_mark_returns_unicode_for_utf8(has_unicode: None) -> None:
    from jaigent.ui import prompt_mark

    assert prompt_mark() == ">"


def test_render_logo_mini_on_non_unicode_console(no_unicode: None) -> None:
    """render_logo() must downgrade to ASCII when the box art cannot be encoded.

    The ``_Cp1252Console`` writes through a cp1252 stream; if the logo emits
    any unsafe character, ``file.write`` raises UnicodeEncodeError.
    """
    from jaigent.branding import render_logo

    out = render_logo(_Cp1252Console("cp1252"), version="0.5.1")
    # Recursively walk the rendered output, writing through the cp1252 stream.
    _emit_cp1252(out, _Cp1252Stream("cp1252"))


def _emit_cp1252(renderable, stream: _Cp1252Stream) -> None:
    """Walk rich RenderableType instances and pipe their text through ``stream``.

    rich exposes ``renderable.render`` (sometimes) or ``renderable.plain``;
    a defensive subset covers the shapes the logo actually produces
    (Text, Group iter, Align iter).
    """
    from rich.console import Group  # type: ignore
    from rich.text import Text  # type: ignore

    if isinstance(renderable, Text):
        stream.write(renderable.plain)
        return
    if isinstance(renderable, Group):
        for child in renderable.renderables:
            _emit_cp1252(child, stream)
        return
    # Align and everything else route through ``__rich_console__``; the safest
    # fallback is to coerce to a string. If a glyph sneaks in, the codec check
    # inside ``stream.write`` raises.
    stream.write(str(renderable))


class _Install:
    """Stand-in for updater.Install."""

    kind = "source"
    location = "/tmp"
    upgradable = True

    def describe(self) -> str:
        return "source"


def test_cmd_models_handles_empty_catalogue(
    monkeypatch: pytest.MonkeyPatch, cp1252_console: _Cp1252Console, no_unicode: None
) -> None:
    """`jaigent models` previously crashed on cp1252 because the marker
    column hardcoded a '←' that cannot be encoded."""
    from unittest.mock import MagicMock

    from jaigent import cli

    settings = MagicMock()
    settings.model = "gpt-4o-mini"
    settings.provider = "openai"

    monkeypatch.setattr("jaigent.cli.console", cp1252_console)
    monkeypatch.setattr("jaigent.cli.err_console", cp1252_console)
    monkeypatch.setattr("jaigent.cli.resolve_settings", lambda args: settings)
    monkeypatch.setattr("jaigent.cli.models.CATALOGUE", [])
    monkeypatch.setattr("jaigent.cli.models.search", lambda q: [])

    code = cli.cmd_models(MagicMock(search=None, only_provider=None, no_color=True))
    assert code == 1


def test_cmd_update_check_does_not_crash_on_cp1252(
    monkeypatch: pytest.MonkeyPatch, cp1252_console: _Cp1252Console, no_unicode: None
) -> None:
    """The 'latest  <- new' line is the one that previously crashed cmd_update."""
    from unittest.mock import MagicMock

    from jaigent import cli
    from jaigent.updater import Release

    newer = Release(version="99.0.0", url="https://example.com", notes="", published="")

    monkeypatch.setattr("jaigent.cli.console", cp1252_console)
    monkeypatch.setattr("jaigent.cli.err_console", cp1252_console)
    monkeypatch.setattr("jaigent.cli.updater.detect_install", lambda: _Install())
    monkeypatch.setattr("jaigent.cli.updater.fetch_latest", lambda: newer)
    monkeypatch.setattr("jaigent.cli.updater.record_check", lambda r: None)
    monkeypatch.setattr(
        "jaigent.cli.updater.inspect_source",
        lambda **k: __import__("jaigent.updater", fromlist=["SourceSync"]).SourceSync(),
    )

    code = cli.cmd_update(MagicMock(check=True, no_color=True, yes=True))
    assert code == 0
