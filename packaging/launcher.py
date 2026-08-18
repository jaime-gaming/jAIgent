"""Entry point for the standalone jaigent binary.

PyInstaller freezes this module, not the console script, so a few things that
normally happen at install time have to happen here instead: making the Windows
console speak UTF-8, and turning an unhandled crash into something a user can
act on rather than a wall of traceback.
"""

from __future__ import annotations

import contextlib
import os
import sys


def _prepare_console() -> None:
    """Make the terminal capable of rendering jaigent's output.

    Windows consoles historically default to a legacy code page and to no ANSI
    processing at all. Both are fixable at runtime, and doing so here means the
    logo and the spinner look right out of the box rather than as mojibake.
    """
    if not sys.platform.startswith("win"):
        return

    # Ask Windows for a VT-capable console (Windows 10 1511 and later).
    try:
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        for handle in (-11, -12):  # stdout, stderr
            mode = ctypes.c_uint32()
            if kernel32.GetConsoleMode(kernel32.GetStdHandle(handle), ctypes.byref(mode)):
                kernel32.SetConsoleMode(
                    kernel32.GetStdHandle(handle),
                    mode.value | 0x0004,  # ENABLE_VIRTUAL_TERMINAL_PROCESSING
                )
    except Exception:  # noqa: BLE001 - cosmetic only; never block startup
        pass

    # Re-encode the standard streams as UTF-8 so the box-drawing glyphs survive.
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            with contextlib.suppress(ValueError, OSError):
                reconfigure(encoding="utf-8", errors="replace")


def main() -> int:
    _prepare_console()

    # Frozen builds have no console script wrapper, so import lazily and give a
    # clear message if the bundle is somehow incomplete.
    try:
        from jaigent.cli import main as cli_main
    except ImportError as exc:  # pragma: no cover - only a broken build hits this
        print(f"jaigent could not start: {exc}", file=sys.stderr)
        print("This build looks incomplete. Please reinstall.", file=sys.stderr)
        return 70

    try:
        return cli_main()
    except KeyboardInterrupt:
        print()
        return 130
    except Exception as exc:  # noqa: BLE001 - last line of defence
        print(f"\njaigent hit an unexpected error: {type(exc).__name__}: {exc}", file=sys.stderr)
        print(
            "Please report this at https://github.com/jaime-gaming/jaigent/issues"
            "\nRun with JAIGENT_DEBUG=1 for the full traceback.",
            file=sys.stderr,
        )
        if os.getenv("JAIGENT_DEBUG"):
            raise
        return 1


if __name__ == "__main__":
    sys.exit(main())
