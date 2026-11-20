#!/usr/bin/env python3
"""Check that every commit in the history belongs to the maintainer.

The log is the part of a project people actually read, and it should read as
one person's work. A commit written by an assistant carries that account into
every clone, mirror and archive, and no later commit takes it back out. This
script is the tripwire: it refuses anything that is not the maintainer, whether
it sits in the author field, the committer field or a trailer in the message.

Run it with no arguments to check everything reachable from HEAD, or point
``--rev`` at a range:

    python scripts/check_authors.py
    python scripts/check_authors.py --rev origin/main..HEAD
"""

from __future__ import annotations

import argparse
import subprocess
import sys
from typing import NamedTuple

MAINTAINER = "Jaime Gaming <jaimegamingpro@gmail.com>"

# Both addresses belong to the same person: the second one is what GitHub
# reports for a commit made through the web interface or with a noreply
# setting, and it appears throughout this project's history.
ALLOWED_AUTHORS = frozenset(
    {
        "jaimegamingpro@gmail.com",
        "250595486+jaime-gaming@users.noreply.github.com",
    }
)

# GitHub commits web edits and the merge commits made from its interface. It is
# not a person, but it is not an assistant either, and a web merge cannot be
# authored without it.
ALLOWED_COMMITTERS = ALLOWED_AUTHORS | {"noreply@github.com"}

# Neither separator can appear in a name, a subject or a body, so no escaping
# is needed on the way back in.
FIELD = "\x1f"
RECORD = "\x1e"
LOG_FORMAT = "%H%x1f%an%x1f%ae%x1f%cn%x1f%ce%x1f%s%x1f%B%x1e"

TRAILER = "co-authored-by:"


class Commit(NamedTuple):
    """One line of ``git log``, unpacked."""

    sha: str
    author_name: str
    author_email: str
    committer_name: str
    committer_email: str
    subject: str
    body: str


def parse(text: str) -> list[Commit]:
    """Turn the output of ``git log`` into commits, skipping blank records."""
    commits = []
    for record in text.split(RECORD):
        if not record.strip():
            continue
        fields = record.split(FIELD)
        if len(fields) != 7:  # pragma: no cover - only if the format changes
            raise ValueError(f"unexpected record with {len(fields)} fields: {record!r}")
        commits.append(Commit(*fields))
    return commits


def _address(line: str) -> str:
    """The part of ``Name <address>`` inside the angle brackets, if any."""
    start = line.find("<")
    end = line.find(">", start + 1)
    if start == -1 or end == -1:
        return ""
    return line[start + 1 : end].strip()


def problems(commits: list[Commit]) -> list[str]:
    """Every reason the history is not the maintainer's, phrased as a sentence."""
    found = []
    for commit in commits:
        short = commit.sha[:7]
        who = f"{commit.author_name} <{commit.author_email}>"
        if commit.author_email not in ALLOWED_AUTHORS:
            found.append(f"{short} is authored by {who}: {commit.subject}")
        if commit.committer_email not in ALLOWED_COMMITTERS:
            found.append(
                f"{short} is committed by {commit.committer_name} "
                f"<{commit.committer_email}>: {commit.subject}"
            )
        for line in commit.body.splitlines():
            if not line.strip().lower().startswith(TRAILER):
                continue
            if _address(line) not in ALLOWED_AUTHORS:
                found.append(f"{short} credits {line.strip()} in its message")
    return found


def read_log(rev: str) -> str:
    """``git log`` for one revision or range, in the format this script reads."""
    result = subprocess.run(
        ["git", "log", f"--format={LOG_FORMAT}", rev],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        sys.exit(f"git log {rev} failed: {result.stderr.strip()}")
    return result.stdout


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--rev",
        default="HEAD",
        help="what to check: a commit, a branch or a range (default: HEAD)",
    )
    args = parser.parse_args(argv)

    found = problems(parse(read_log(args.rev)))
    if not found:
        print(f"every commit checked belongs to {MAINTAINER}")
        return 0

    print(f"{len(found)} commit(s) do not belong to {MAINTAINER}:\n")
    for problem in found:
        print(f"  {problem}")
    print(
        "\nEvery commit in this history must be authored and committed as the\n"
        "maintainer. Set that once per checkout, before your first commit:\n\n"
        '    git config user.name "Jaime Gaming"\n'
        "    git config user.email jaimegamingpro@gmail.com\n\n"
        "CONTRIBUTING.md explains how to repair a commit that already carries\n"
        "the wrong name."
    )
    return 1


if __name__ == "__main__":
    sys.exit(main())
