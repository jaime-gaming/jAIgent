"""Skills: parsing, discovery, scoping and the load_skill tool."""

from __future__ import annotations

from pathlib import Path

import pytest

from jaigent import skills
from jaigent.errors import ToolError
from jaigent.skills import build_skill_tools, catalogue, create_skill, discover, parse_skill


@pytest.fixture
def skill_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Isolate both the user and project skill directories."""
    home = tmp_path / "home"
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setenv("JAIGENT_HOME", str(home))
    monkeypatch.chdir(project)
    return project


def write_skill(directory: Path, name: str, text: str) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.md"
    path.write_text(text, encoding="utf-8")
    return path


class TestParsing:
    def test_front_matter(self, tmp_path: Path) -> None:
        path = write_skill(
            tmp_path,
            "demo",
            "---\nname: changelog\ndescription: Write a changelog.\n---\n\nDo the thing.\n",
        )
        skill = parse_skill(path)

        assert skill.name == "changelog"
        assert skill.description == "Write a changelog."
        assert skill.body.strip() == "Do the thing."

    def test_quotes_are_stripped(self, tmp_path: Path) -> None:
        path = write_skill(tmp_path, "d", "---\ndescription: 'Quoted value'\n---\nBody\n")
        assert parse_skill(path).description == "Quoted value"

    def test_name_defaults_to_filename(self, tmp_path: Path) -> None:
        path = write_skill(tmp_path, "review", "Just a body, no front matter.\n")
        assert parse_skill(path).name == "review"

    def test_description_falls_back_to_first_line(self, tmp_path: Path) -> None:
        path = write_skill(tmp_path, "d", "# Heading\n\nThe real summary.\nMore text.\n")
        assert parse_skill(path).description == "The real summary."

    def test_no_front_matter_keeps_whole_body(self, tmp_path: Path) -> None:
        path = write_skill(tmp_path, "d", "Line one\nLine two\n")
        assert "Line two" in parse_skill(path).body

    def test_oversized_skill_is_rejected(self, tmp_path: Path) -> None:
        path = write_skill(tmp_path, "big", "x" * 200_000)
        with pytest.raises(ToolError, match="larger than"):
            parse_skill(path)

    def test_render_includes_name_and_body(self, tmp_path: Path) -> None:
        path = write_skill(tmp_path, "d", "---\nname: x\ndescription: Y\n---\nZ body\n")
        rendered = parse_skill(path).render()

        assert "# Skill: x" in rendered
        assert "Z body" in rendered


class TestDiscovery:
    def test_finds_project_skills(self, skill_home: Path) -> None:
        write_skill(skill_home / ".jaigent" / "skills", "a", "---\ndescription: A\n---\nbody\n")
        found = discover()

        assert set(found) == {"a"}
        assert found["a"].scope == "project"

    def test_finds_user_skills(self, skill_home: Path, tmp_path: Path) -> None:
        write_skill(tmp_path / "home" / "skills", "b", "---\ndescription: B\n---\nbody\n")
        found = discover()

        assert found["b"].scope == "user"

    def test_project_shadows_user(self, skill_home: Path, tmp_path: Path) -> None:
        write_skill(tmp_path / "home" / "skills", "dup", "---\ndescription: user\n---\nu\n")
        write_skill(skill_home / ".jaigent" / "skills", "dup", "---\ndescription: proj\n---\np\n")
        found = discover()

        assert len(found) == 1
        assert found["dup"].description == "proj"
        assert found["dup"].scope == "project"

    def test_no_directories_is_empty(self, skill_home: Path) -> None:
        assert discover() == {}

    def test_non_markdown_is_ignored(self, skill_home: Path) -> None:
        directory = skill_home / ".jaigent" / "skills"
        directory.mkdir(parents=True)
        (directory / "notes.txt").write_text("not a skill", encoding="utf-8")
        assert discover() == {}

    def test_a_broken_skill_does_not_break_discovery(self, skill_home: Path) -> None:
        directory = skill_home / ".jaigent" / "skills"
        write_skill(directory, "good", "---\ndescription: fine\n---\nbody\n")
        (directory / "huge.md").write_text("x" * 200_000, encoding="utf-8")

        assert set(discover()) == {"good"}

    def test_invalid_names_are_skipped(self, skill_home: Path) -> None:
        write_skill(skill_home / ".jaigent" / "skills", "bad", "---\nname: 'not valid!'\n---\nb\n")
        assert discover() == {}


class TestCatalogue:
    def test_lists_one_line_each(self, skill_home: Path) -> None:
        directory = skill_home / ".jaigent" / "skills"
        write_skill(directory, "a", "---\ndescription: First.\n---\nx\n")
        write_skill(directory, "b", "---\ndescription: Second.\n---\ny\n")

        text = catalogue(discover())

        assert "- a: First." in text
        assert "- b: Second." in text

    def test_empty(self) -> None:
        assert catalogue({}) == ""

    def test_body_is_not_in_the_catalogue(self, skill_home: Path) -> None:
        write_skill(
            skill_home / ".jaigent" / "skills",
            "a",
            "---\ndescription: Short.\n---\nSECRET_BODY_TEXT\n",
        )
        # The whole point: bodies stay out of the prompt until loaded.
        assert "SECRET_BODY_TEXT" not in catalogue(discover())


class TestLoadSkillTool:
    def test_absent_when_no_skills(self) -> None:
        assert build_skill_tools({}) == []

    def test_present_when_skills_exist(self, skill_home: Path) -> None:
        write_skill(skill_home / ".jaigent" / "skills", "a", "---\ndescription: A\n---\nbody\n")
        tools = build_skill_tools(discover())

        assert len(tools) == 1
        assert tools[0].name == "load_skill"

    def test_loads_the_body(self, skill_home: Path) -> None:
        write_skill(
            skill_home / ".jaigent" / "skills", "a", "---\ndescription: A\n---\nFULL BODY\n"
        )
        tool = build_skill_tools(discover())[0]

        assert "FULL BODY" in tool(name="a")

    def test_case_insensitive(self, skill_home: Path) -> None:
        write_skill(skill_home / ".jaigent" / "skills", "a", "---\ndescription: A\n---\nbody\n")
        tool = build_skill_tools(discover())[0]

        assert "body" in tool(name="  A  ")

    def test_unknown_skill_lists_alternatives(self, skill_home: Path) -> None:
        write_skill(skill_home / ".jaigent" / "skills", "known", "---\ndescription: K\n---\nb\n")
        tool = build_skill_tools(discover())[0]

        with pytest.raises(ToolError, match="Available skills: known"):
            tool(name="missing")

    def test_schema_enumerates_skills(self, skill_home: Path) -> None:
        write_skill(skill_home / ".jaigent" / "skills", "a", "---\ndescription: A\n---\nb\n")
        tool = build_skill_tools(discover())[0]

        assert tool.parameters["properties"]["name"]["enum"] == ["a"]


class TestCreateSkill:
    def test_creates_a_project_skill(self, skill_home: Path) -> None:
        path = create_skill("my-skill", "Does a thing.", "Step one.")

        assert path.exists()
        assert path.parent == skill_home / ".jaigent" / "skills"
        assert discover()["my-skill"].description == "Does a thing."

    def test_creates_a_user_skill(self, skill_home: Path, tmp_path: Path) -> None:
        path = create_skill("mine", "Personal.", "Body.", scope="user")
        assert path.parent == tmp_path / "home" / "skills"

    def test_spaces_become_dashes(self, skill_home: Path) -> None:
        assert create_skill("my great skill", "d", "b").stem == "my-great-skill"

    @pytest.mark.parametrize("bad", ["!!!", "-leading", ""])
    def test_invalid_names_rejected(self, skill_home: Path, bad: str) -> None:
        with pytest.raises(ToolError, match="Invalid skill name"):
            create_skill(bad, "d", "b")

    def test_round_trips_through_the_parser(self, skill_home: Path) -> None:
        create_skill("rt", "Round trip.", "The body text.")
        skill = discover()["rt"]

        assert skill.description == "Round trip."
        assert "The body text." in skill.body


class TestAgentIntegration:
    def test_registry_gains_load_skill(self, skill_home: Path) -> None:
        from jaigent.config import Settings
        from jaigent.tools import build_default_registry

        write_skill(skill_home / ".jaigent" / "skills", "a", "---\ndescription: A\n---\nb\n")
        registry = build_default_registry(Settings(api_key="k", workspace=skill_home))

        assert "load_skill" in registry

    def test_no_tool_without_skills(self, skill_home: Path) -> None:
        from jaigent.config import Settings
        from jaigent.tools import build_default_registry

        assert "load_skill" not in build_default_registry(
            Settings(api_key="k", workspace=skill_home)
        )

    def test_disabled_by_setting(self, skill_home: Path) -> None:
        from jaigent.config import Settings
        from jaigent.tools import build_default_registry

        write_skill(skill_home / ".jaigent" / "skills", "a", "---\ndescription: A\n---\nb\n")
        registry = build_default_registry(
            Settings(api_key="k", workspace=skill_home, skills_enabled=False)
        )

        assert "load_skill" not in registry

    def test_catalogue_reaches_the_system_prompt(self, skill_home: Path) -> None:
        from conftest import FakeProvider
        from jaigent.agent import Agent
        from jaigent.config import Settings

        write_skill(
            skill_home / ".jaigent" / "skills",
            "changelog",
            "---\ndescription: Write a changelog.\n---\nBODY_NOT_IN_PROMPT\n",
        )
        agent = Agent(Settings(api_key="k", workspace=skill_home), provider=FakeProvider([]))

        assert "changelog: Write a changelog." in agent.system_prompt
        assert "BODY_NOT_IN_PROMPT" not in agent.system_prompt


def test_skills_dirs_uses_jaigent_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("JAIGENT_HOME", str(tmp_path / "custom"))
    scopes = dict(skills.skills_dirs())
    assert scopes["user"] == tmp_path / "custom" / "skills"
