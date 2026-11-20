"""The authorship check, which keeps a helper's name out of the log.

The script runs in CI over the whole history, so it has to be right about the
one thing it does: an identity it does not recognise is a failure, wherever in
the commit it sits.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "check_authors.py"


def load():
    spec = importlib.util.spec_from_file_location("check_authors", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


check = load()

FIELD = check.FIELD
RECORD = check.RECORD


def record(
    sha: str = "a" * 40,
    author: str = "Jaime Gaming",
    email: str = "jaimegamingpro@gmail.com",
    committer: str = "Jaime Gaming",
    committer_email: str = "jaimegamingpro@gmail.com",
    subject: str = "Say less",
    body: str = "",
) -> str:
    fields = [sha, author, email, committer, committer_email, subject, body]
    return FIELD.join(fields) + RECORD


def findings(**kwargs) -> list[str]:
    return check.problems(check.parse(record(**kwargs)))


def test_the_maintainer_passes():
    assert findings() == []


def test_the_noreply_address_passes():
    # Historical commits use the address GitHub hands out with a private email
    # setting, and they are the same person.
    assert findings(email="250595486+jaime-gaming@users.noreply.github.com") == []


def test_a_commit_made_on_the_web_passes():
    # A merge clicked in the browser is committed by GitHub. Nothing else can
    # sign it, and it names no assistant.
    assert findings(committer="GitHub", committer_email="noreply@github.com") == []


def test_a_foreign_author_is_reported():
    found = findings(author="Build Helper", email="helper@example.com")
    assert len(found) == 1
    assert found[0].startswith("aaaaaaa is authored by Build Helper <helper@example.com>")
    assert found[0].endswith(": Say less")


def test_a_foreign_committer_is_reported():
    found = findings(committer="Build Helper", committer_email="helper@example.com")
    assert len(found) == 1
    assert "committed by Build Helper" in found[0]


def test_a_trailer_naming_a_helper_is_reported():
    found = findings(body="The body.\n\nCo-authored-by: Build Helper <helper@example.com>\n")
    assert len(found) == 1
    assert (
        found[0]
        == "aaaaaaa credits Co-authored-by: Build Helper <helper@example.com> in its message"
    )


def test_a_trailer_naming_the_maintainer_is_fine():
    assert findings(body="Co-authored-by: Jaime Gaming <jaimegamingpro@gmail.com>\n") == []


def test_a_trailer_without_an_address_is_reported():
    # An address it cannot read is not an address it can vouch for.
    assert findings(body="Co-authored-by: Build Helper\n") != []


def test_the_body_survives_parsing():
    commits = check.parse(record(body="First line.\n\nSecond line.\n"))
    assert len(commits) == 1
    assert commits[0].body == "First line.\n\nSecond line.\n"
    assert commits[0].subject == "Say less"


def test_several_commits_are_read_separately():
    text = record(sha="b" * 40) + record(sha="c" * 40)
    assert [commit.sha[:1] for commit in check.parse(text)] == ["b", "c"]


def test_this_repository_passes():
    # The real history, not a fixture: the check has to be green on the branch
    # that ships it, or it is a wish rather than a guard.
    result = subprocess.run(
        [sys.executable, str(SCRIPT)],
        capture_output=True,
        text=True,
        cwd=SCRIPT.parent.parent,
    )
    assert result.returncode == 0, result.stdout + result.stderr
