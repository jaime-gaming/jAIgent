"""`jaigent undo`, `checkpoints` and `rewind` at the command level.

The store itself is well covered by `tests/test_checkpoint.py`, but nothing
exercised the command. That gap is why `undo` shipped twice with a bug that only
showed up when a human ran it: undoing once worked, and the unit tests only ever
undid once.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jaigent import cli
from jaigent.checkpoint import CheckpointStore


@pytest.fixture()
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("JAIGENT_HOME", str(tmp_path / "home"))
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    ws = tmp_path / "ws"
    ws.mkdir()
    return ws


def undo(workspace: Path) -> int:
    return cli.main(["undo", "-w", str(workspace), "--no-color"])


def write(workspace: Path, name: str, text: str) -> Path:
    path = workspace / name
    path.write_text(text, encoding="utf-8")
    return path


class TestUndoStepsBack:
    def test_it_restores_the_previous_content(self, workspace: Path) -> None:
        note = write(workspace, "note.md", "first")
        store = CheckpointStore(workspace)
        store.capture([Path("note.md")], label="write_file note.md", tool="write_file")
        note.write_text("second", encoding="utf-8")

        assert undo(workspace) == 0
        assert note.read_text(encoding="utf-8") == "first"

    def test_it_deletes_a_file_that_did_not_exist_before(self, workspace: Path) -> None:
        store = CheckpointStore(workspace)
        store.capture([Path("new.md")], label="write_file new.md", tool="write_file")
        created = write(workspace, "new.md", "content")

        assert undo(workspace) == 0
        assert not created.exists()

    def test_undoing_twice_steps_back_twice(self, workspace: Path) -> None:
        # The bug that shipped: the second undo re-applied the first checkpoint.
        note = write(workspace, "note.md", "v1")
        store = CheckpointStore(workspace)

        store.capture([Path("note.md")], label="write_file note.md", tool="write_file")
        note.write_text("v2", encoding="utf-8")
        store.capture([Path("note.md")], label="write_file note.md", tool="write_file")
        note.write_text("v3", encoding="utf-8")

        assert undo(workspace) == 0
        assert note.read_text(encoding="utf-8") == "v2"

        assert undo(workspace) == 0
        assert note.read_text(encoding="utf-8") == "v1"

    def test_it_reports_when_there_is_nothing_to_undo(
        self, workspace: Path, capsys: pytest.CaptureFixture
    ) -> None:
        assert undo(workspace) == 0
        assert "nothing to undo" in capsys.readouterr().out.lower()


class TestUndoSkipsCheckpointsThatChangeNothing:
    """Pressing undo must undo something, or say why it cannot.

    Re-running the same task writes identical content, so the newest checkpoint
    reverts to a state the file is already in. Consuming one of those per press
    means the user hits undo repeatedly and watches nothing happen, with no
    indication that anything is being used up.
    """

    def test_it_reaches_past_a_no_op_checkpoint(self, workspace: Path) -> None:
        note = write(workspace, "note.md", "original")
        store = CheckpointStore(workspace)

        # A real change...
        store.capture([Path("note.md")], label="write_file note.md", tool="write_file")
        note.write_text("changed", encoding="utf-8")
        # ...then two writes of identical content, which revert to "changed".
        for _ in range(2):
            store.capture([Path("note.md")], label="write_file note.md", tool="write_file")
            note.write_text("changed", encoding="utf-8")

        assert undo(workspace) == 0
        assert note.read_text(encoding="utf-8") == "original", (
            "undo stopped on a checkpoint that reverts nothing"
        )

    def test_it_says_so_when_every_checkpoint_is_a_no_op(
        self, workspace: Path, capsys: pytest.CaptureFixture
    ) -> None:
        write(workspace, "note.md", "same")
        store = CheckpointStore(workspace)
        store.capture([Path("note.md")], label="write_file note.md", tool="write_file")

        assert undo(workspace) == 0
        out = capsys.readouterr().out.lower()
        assert "nothing" in out

    def test_a_no_op_is_not_left_to_block_the_next_undo(self, workspace: Path) -> None:
        note = write(workspace, "note.md", "original")
        store = CheckpointStore(workspace)
        store.capture([Path("note.md")], label="write_file note.md", tool="write_file")
        note.write_text("changed", encoding="utf-8")
        store.capture([Path("note.md")], label="write_file note.md", tool="write_file")
        note.write_text("changed", encoding="utf-8")

        undo(workspace)

        assert note.read_text(encoding="utf-8") == "original"
        # And the history no longer holds the checkpoints it walked through.
        assert CheckpointStore(workspace).history() == []


class TestCheckpointsCommand:
    def test_it_reports_an_empty_history(
        self, workspace: Path, capsys: pytest.CaptureFixture
    ) -> None:
        assert cli.main(["checkpoints", "-w", str(workspace), "--no-color"]) == 0
        assert "no checkpoint" in capsys.readouterr().out.lower()

    def test_it_lists_what_is_there(self, workspace: Path, capsys: pytest.CaptureFixture) -> None:
        (workspace / "note.md").write_text("v1", encoding="utf-8")
        CheckpointStore(workspace).capture(
            [Path("note.md")], label="write_file note.md", tool="write_file"
        )

        assert cli.main(["checkpoints", "-w", str(workspace), "--no-color"]) == 0
        assert "note.md" in capsys.readouterr().out

    def test_clear_empties_the_history(self, workspace: Path) -> None:
        (workspace / "note.md").write_text("v1", encoding="utf-8")
        CheckpointStore(workspace).capture(
            [Path("note.md")], label="write_file note.md", tool="write_file"
        )

        assert cli.main(["checkpoints", "--clear", "-w", str(workspace), "--no-color"]) == 0
        assert CheckpointStore(workspace).history() == []


class TestRewindCommand:
    def test_an_unknown_id_is_an_error(
        self, workspace: Path, capsys: pytest.CaptureFixture
    ) -> None:
        code = cli.main(["rewind", "deadbeef", "-w", str(workspace), "--no-color"])

        assert code == 1
        assert "no checkpoint" in capsys.readouterr().err.lower()

    def test_it_restores_by_id(self, workspace: Path) -> None:
        note = workspace / "note.md"
        note.write_text("original", encoding="utf-8")
        snapshot = CheckpointStore(workspace).capture(
            [Path("note.md")], label="write_file note.md", tool="write_file"
        )
        assert snapshot is not None
        note.write_text("changed", encoding="utf-8")

        assert cli.main(["rewind", snapshot.id, "-w", str(workspace), "--no-color"]) == 0
        assert note.read_text(encoding="utf-8") == "original"

    def test_rewind_keeps_the_checkpoint(self, workspace: Path) -> None:
        # Unlike undo, rewind is a jump, not a pop — the entry must survive so
        # the same point can be returned to again.
        note = workspace / "note.md"
        note.write_text("original", encoding="utf-8")
        snapshot = CheckpointStore(workspace).capture(
            [Path("note.md")], label="write_file note.md", tool="write_file"
        )
        assert snapshot is not None
        note.write_text("changed", encoding="utf-8")

        cli.main(["rewind", snapshot.id, "-w", str(workspace), "--no-color"])

        assert any(c.id == snapshot.id for c in CheckpointStore(workspace).history())
