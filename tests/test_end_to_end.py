"""The whole loop, wired up the way the CLI wires it.

Every test elsewhere isolates something: the store without the agent, the agent
with a stub registry, a command with a stub store. Both bugs fixed in 0.5.3's
checkpoint work were invisible to all of them and turned up the first time the
agent was run end to end against a real workspace — a read-only tool quietly
writing checkpoints, and `undo` spending itself on one that reverts nothing.

So this module assembles the real thing: a real `Agent`, the real tool registry
writing to a real directory, and a real `CheckpointStore`. Only the model is
faked, because that is the one part that would cost money.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from conftest import FakeProvider
from jaigent.agent import Agent
from jaigent.approval import Approver, Mode
from jaigent.checkpoint import CheckpointStore
from jaigent.config import Settings
from jaigent.llm.base import AssistantMessage, ToolCall
from jaigent.tools import build_default_registry


@pytest.fixture()
def project(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("JAIGENT_HOME", str(tmp_path / "home"))
    workspace = tmp_path / "project"
    workspace.mkdir()
    (workspace / "notes.md").write_text("buy milk\nwrite report\n", encoding="utf-8")
    (workspace / "src").mkdir()
    (workspace / "src" / "app.py").write_text("print('hi')\n", encoding="utf-8")
    return workspace


def call(name: str, **arguments: object) -> AssistantMessage:
    return AssistantMessage(tool_calls=[ToolCall(f"c_{name}", name, dict(arguments))])


def run_agent(workspace: Path, script: list[AssistantMessage]) -> Agent:
    """Build the agent the way `jaigent run` does, and drive it once."""
    settings = Settings(api_key="k", workspace=workspace, checkpoints=True)
    agent = Agent(
        settings,
        tools=build_default_registry(settings),
        provider=FakeProvider(script),
        approver=Approver(Mode.AUTO),
    )
    agent.run("do the thing")
    return agent


class TestAReadThenWriteTask:
    """The shape of almost every real run: look around, then change one file."""

    @pytest.fixture()
    def done(self, project: Path) -> Path:
        run_agent(
            project,
            [
                call("list_files", path="."),
                call("read_file", path="notes.md"),
                call("write_file", path="summary.md", content="# Summary\n\nTwo tasks.\n"),
                AssistantMessage(content="Wrote summary.md."),
            ],
        )
        return project

    def test_the_file_is_written(self, done: Path) -> None:
        assert (done / "summary.md").read_text(encoding="utf-8").startswith("# Summary")

    def test_only_the_write_is_checkpointed(self, done: Path) -> None:
        history = CheckpointStore(done).history()

        assert [c.tool for c in history] == ["write_file"], (
            f"read-only tools left checkpoints: {[c.tool for c in history]}"
        )

    def test_the_checkpoint_names_the_file_it_covers(self, done: Path) -> None:
        (checkpoint,) = CheckpointStore(done).history()

        assert [state.path for state in checkpoint.files] == ["summary.md"]

    def test_the_snapshot_predates_the_write(self, done: Path) -> None:
        # It records "this file did not exist", which is what makes undo able
        # to remove it again.
        (checkpoint,) = CheckpointStore(done).history()

        assert checkpoint.files[0].digest is None

    def test_one_undo_removes_the_new_file(self, done: Path) -> None:
        store = CheckpointStore(done)
        store.restore(store.history()[0])

        assert not (done / "summary.md").exists()


class TestRunningTheSameTaskTwice:
    """The case that broke undo: identical content written again."""

    @pytest.fixture()
    def done(self, project: Path) -> Path:
        for _ in range(3):
            run_agent(
                project,
                [
                    call("list_files", path="."),
                    call("write_file", path="summary.md", content="same every time\n"),
                    AssistantMessage(content="done"),
                ],
            )
        return project

    def test_each_run_records_exactly_one_checkpoint(self, done: Path) -> None:
        assert len(CheckpointStore(done).history()) == 3

    def test_the_newest_two_would_revert_nothing(self, done: Path) -> None:
        store = CheckpointStore(done)
        history = store.history()

        for checkpoint in history[:2]:
            actionable = [a for _, a in store.diff_summary(checkpoint) if a != "unchanged"]
            assert actionable == [], "expected these to be no-ops"

    def test_the_oldest_still_removes_the_file(self, done: Path) -> None:
        store = CheckpointStore(done)
        store.restore(store.history()[-1])

        assert not (done / "summary.md").exists()


class TestEditingAnExistingFile:
    def test_undo_restores_the_original_text(self, project: Path) -> None:
        original = (project / "src" / "app.py").read_text(encoding="utf-8")
        run_agent(
            project,
            [
                call(
                    "edit_file",
                    path="src/app.py",
                    old_text="print('hi')",
                    new_text="print('hello')",
                ),
                AssistantMessage(content="edited"),
            ],
        )
        assert "hello" in (project / "src" / "app.py").read_text(encoding="utf-8")

        store = CheckpointStore(project)
        store.restore(store.history()[0])

        assert (project / "src" / "app.py").read_text(encoding="utf-8") == original

    def test_the_checkpoint_path_is_posix(self, project: Path) -> None:
        # The model writes forward slashes; what it reads back must match.
        run_agent(
            project,
            [
                call(
                    "edit_file",
                    path="src/app.py",
                    old_text="print('hi')",
                    new_text="print('yo')",
                ),
                AssistantMessage(content="edited"),
            ],
        )
        (checkpoint,) = CheckpointStore(project).history()

        assert checkpoint.files[0].path == "src/app.py"
        assert "\\" not in checkpoint.files[0].path


class TestADryRunChangesNothing:
    def test_the_file_is_untouched_but_still_snapshotted(self, project: Path) -> None:
        settings = Settings(api_key="k", workspace=project, checkpoints=True)
        agent = Agent(
            settings,
            tools=build_default_registry(settings),
            provider=FakeProvider(
                [
                    call("write_file", path="notes.md", content="OVERWRITTEN"),
                    AssistantMessage(content="would have written"),
                ]
            ),
            approver=Approver(Mode.DRY_RUN),
        )
        agent.run("overwrite the notes")

        assert (project / "notes.md").read_text(encoding="utf-8") == "buy milk\nwrite report\n"


class TestTheSandboxHoldsDuringARealRun:
    @pytest.mark.parametrize("target", ["../escape.md", "/etc/passwd"])
    def test_a_write_outside_the_workspace_is_refused(self, project: Path, target: str) -> None:
        run_agent(
            project,
            [
                call("write_file", path=target, content="pwned"),
                AssistantMessage(content="tried"),
            ],
        )

        assert not (project.parent / "escape.md").exists()

    def test_the_refusal_is_reported_back_to_the_model(self, project: Path) -> None:
        agent = run_agent(
            project,
            [
                call("write_file", path="../escape.md", content="pwned"),
                AssistantMessage(content="tried"),
            ],
        )
        # The tool result goes back as a message the model can recover from,
        # rather than crashing the run.
        sent = json.dumps(agent.provider.calls[-1])  # type: ignore[attr-defined]

        assert "ERROR" in sent
