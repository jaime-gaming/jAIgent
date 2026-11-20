"""The house style of the text this project ships.

Two rules that are cheap to break by accident and expensive to notice: a
dash nobody wanted, and prose that reads as though a machine assembled it.
The dash rule is mechanical, so it is a test; the rest is a matter of
reading, which no test can do.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

#: Repository root, from this file's location.
ROOT = Path(__file__).resolve().parent.parent

#: Files whose prose is ours. Everything tracked is included unless it is
#: binary or a lock file, so a new document is covered the day it lands.
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".whl", ".gz", ".zip", ".lock"}


def text_files() -> list[Path]:
    """Every tracked text file, via git so untracked noise is not read."""
    listed = subprocess.run(
        ["git", "ls-files"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if listed.returncode != 0:  # pragma: no cover - not a checkout
        return []
    return [
        ROOT / name
        for name in listed.stdout.splitlines()
        if name and Path(name).suffix.lower() not in SKIP_SUFFIXES
    ]


class TestNoDashes:
    """An em dash or an en dash, in prose or in a string, is a rewrite.

    They were removed from this project on purpose: they made every page read
    the same, and a dash standing in for the right punctuation is how a
    sentence ends up without one. Escaped forms (``\\u2014`` in source) count
    too, because they produce the character at runtime.
    """

    def test_no_em_or_en_dashes_anywhere(self) -> None:
        files = text_files()
        assert files, "git ls-files returned nothing; has the checkout moved?"

        offenders: list[str] = []
        for path in files:
            try:
                body = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):  # pragma: no cover - binary file
                continue
            for marker in ("\u2014", "\u2013", "\\u2014", "\\u2013"):
                if marker in body:
                    line = next(
                        (i for i, text in enumerate(body.splitlines(), 1) if marker in text),
                        0,
                    )
                    offenders.append(f"{path.relative_to(ROOT)}:{line}")
                    break

        assert not offenders, "em or en dash found in: " + ", ".join(sorted(offenders))
