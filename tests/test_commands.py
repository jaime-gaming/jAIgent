"""Custom slash commands: parsing, placeholders, scoping and dispatch."""

from __future__ import annotations

from pathlib import Path

import pytest

from jaigent import commands
from jaigent.commands import RESERVED, create_command, discover, parse_command, resolve
from jaigent.errors import ToolError


@pytest.fixture
def command_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("JAIGENT_HOME", str(home))
    monkeypatch.chdir(project)
    return project


def write(directory: Path, name: str, text: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.md"
    path.write_text(text, encoding="utf-8")
    return path


class TestParsing:
    def test_front_matter(self, tmp_path: Path) -> None:
        path = write(tmp_path, "x", "---\nname: review\ndescription: Review it.\n---\nBody here.\n")
        command = parse_command(path)

        assert command.name == "review"
        assert command.description == "Review it."
        assert command.template == "Body here."

    def test_name_defaults_to_the_filename(self, tmp_path: Path) -> None:
        assert parse_command(write(tmp_path, "deploy", "Do the thing.\n")).name == "deploy"

    def test_description_falls_back_to_the_first_line(self, tmp_path: Path) -> None:
        path = write(tmp_path, "x", "# Title\n\nThe summary line.\n")
        assert parse_command(path).description == "The summary line."

    def test_oversized_file_rejected(self, tmp_path: Path) -> None:
        with pytest.raises(ToolError, match="larger than"):
            parse_command(write(tmp_path, "big", "x" * 60_000))


class TestPlaceholders:
    def _command(self, tmp_path: Path, template: str):  # noqa: ANN202
        return parse_command(write(tmp_path, "c", f"---\ndescription: d\n---\n{template}\n"))

    def test_arguments(self, tmp_path: Path) -> None:
        command = self._command(tmp_path, "Review $ARGUMENTS for bugs.")
        assert command.render("the auth module") == "Review the auth module for bugs."

    def test_positional(self, tmp_path: Path) -> None:
        command = self._command(tmp_path, "Move $1 to $2.")
        assert command.render("a.txt b.txt") == "Move a.txt to b.txt."

    def test_missing_positional_collapses(self, tmp_path: Path) -> None:
        command = self._command(tmp_path, "Check $1 and $2.")
        assert command.render("only") == "Check only and ."

    def test_workspace(self, tmp_path: Path) -> None:
        command = self._command(tmp_path, "Work in $WORKSPACE.")
        assert command.render("", workspace="/srv/app") == "Work in /srv/app."

    def test_no_arguments_leaves_clean_text(self, tmp_path: Path) -> None:
        command = self._command(tmp_path, "Just do it. $ARGUMENTS")
        assert command.render("") == "Just do it."

    def test_combined(self, tmp_path: Path) -> None:
        command = self._command(tmp_path, "In $WORKSPACE review $1 (full: $ARGUMENTS)")
        out = command.render("auth.py extra", workspace="/w")

        assert "/w" in out
        assert "auth.py extra" in out


class TestDiscovery:
    def test_project_commands(self, command_home: Path) -> None:
        write(command_home / ".jaigent" / "commands", "review", "---\ndescription: R\n---\nb\n")
        found = discover()

        assert set(found) == {"review"}
        assert found["review"].scope == "project"

    def test_user_commands(self, command_home: Path, tmp_path: Path) -> None:
        write(tmp_path / "home" / "commands", "mine", "---\ndescription: M\n---\nb\n")
        assert discover()["mine"].scope == "user"

    def test_project_shadows_user(self, command_home: Path, tmp_path: Path) -> None:
        write(tmp_path / "home" / "commands", "dup", "---\ndescription: user\n---\nu\n")
        write(command_home / ".jaigent" / "commands", "dup", "---\ndescription: proj\n---\np\n")

        assert discover()["dup"].description == "proj"

    def test_reserved_names_are_skipped(self, command_home: Path) -> None:
        write(command_home / ".jaigent" / "commands", "help", "---\ndescription: no\n---\nb\n")
        assert discover() == {}

    def test_broken_file_does_not_hide_others(self, command_home: Path) -> None:
        directory = command_home / ".jaigent" / "commands"
        write(directory, "good", "---\ndescription: fine\n---\nb\n")
        (directory / "huge.md").write_text("x" * 60_000, encoding="utf-8")

        assert set(discover()) == {"good"}

    def test_empty(self, command_home: Path) -> None:
        assert discover() == {}


class TestResolve:
    def test_matches_a_command(self, command_home: Path) -> None:
        write(
            command_home / ".jaigent" / "commands",
            "review",
            "---\ndescription: R\n---\nDo $ARGUMENTS\n",
        )
        match = resolve("/review the diff")

        assert match is not None
        command, arguments = match
        assert command.name == "review"
        assert arguments == "the diff"

    def test_no_arguments(self, command_home: Path) -> None:
        write(command_home / ".jaigent" / "commands", "x", "---\ndescription: d\n---\nb\n")
        match = resolve("/x")

        assert match is not None
        assert match[1] == ""

    def test_unknown_command(self, command_home: Path) -> None:
        assert resolve("/nope") is None

    def test_plain_text_is_not_a_command(self, command_home: Path) -> None:
        assert resolve("just a prompt") is None


class TestCreate:
    def test_creates_a_project_command(self, command_home: Path) -> None:
        path = create_command("review", "Review it.", "Check $ARGUMENTS.")

        assert path.parent == command_home / ".jaigent" / "commands"
        assert discover()["review"].description == "Review it."

    def test_user_scope(self, command_home: Path, tmp_path: Path) -> None:
        path = create_command("mine", "d", "b", scope="user")
        assert path.parent == tmp_path / "home" / "commands"

    def test_leading_slash_is_stripped(self, command_home: Path) -> None:
        assert create_command("/review", "d", "b").stem == "review"

    def test_spaces_become_dashes(self, command_home: Path) -> None:
        assert create_command("my command", "d", "b").stem == "my-command"

    @pytest.mark.parametrize("name", sorted(RESERVED)[:4])
    def test_reserved_names_rejected(self, command_home: Path, name: str) -> None:
        with pytest.raises(ToolError, match="built-in"):
            create_command(name, "d", "b")

    @pytest.mark.parametrize("name", ["!!", "-x", ""])
    def test_invalid_names_rejected(self, command_home: Path, name: str) -> None:
        with pytest.raises(ToolError, match="Invalid command name"):
            create_command(name, "d", "b")

    def test_round_trip(self, command_home: Path) -> None:
        create_command("rt", "Round trip.", "Body with $ARGUMENTS.")
        command = discover()["rt"]

        assert command.description == "Round trip."
        assert command.render("args") == "Body with args."


def test_commands_dirs_follow_jaigent_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JAIGENT_HOME", str(tmp_path / "h"))
    assert dict(commands.commands_dirs())["user"] == tmp_path / "h" / "commands"
