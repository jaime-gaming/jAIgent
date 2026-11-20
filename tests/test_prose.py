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

#: The characters, built from their code points so this file does not itself
#: contain what it is looking for. Reading a literal backslash-u escape is
#: unreliable here: it is a real character in the source either way.
MARKERS = tuple(chr(point) for point in (0x2014, 0x2013)) + tuple(
    f"\\u{point:04x}" for point in (0x2014, 0x2013)
)

#: Files whose prose is ours. Everything tracked is included unless it is
#: binary or a lock file, so a new document is covered the day it lands.
SKIP_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".ico", ".whl", ".gz", ".zip", ".lock"}


def text_files() -> list[Path]:
    """Every text file git knows about, including ones not committed yet.

    ``--others --exclude-standard`` matters: a brand-new file is untracked
    until it is committed, and a check that only reads the index would pass
    on a document that is about to be committed with the very character it
    forbids. This caught itself that way once.
    """
    listed = subprocess.run(
        ["git", "ls-files", "--cached", "--others", "--exclude-standard"],
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
    sentence ends up without one. The escaped spelling in source counts too,
    because it produces the character at runtime.
    """

    def test_no_em_or_en_dashes_anywhere(self) -> None:
        files = text_files()
        assert files, "git listed no files; has the checkout moved?"

        offenders: list[str] = []
        for path in files:
            try:
                body = path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):  # pragma: no cover - binary file
                continue
            for marker in MARKERS:
                if marker in body:
                    line = next(
                        (i for i, text in enumerate(body.splitlines(), 1) if marker in text),
                        0,
                    )
                    offenders.append(f"{path.relative_to(ROOT)}:{line}")
                    break

        assert not offenders, "em or en dash found in: " + ", ".join(sorted(offenders))
