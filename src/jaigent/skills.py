"""Skills: reusable instruction packs the agent can load on demand.

A skill is a markdown file with YAML-ish front matter::

    ---
    name: changelog
    description: Write a release changelog from git history.
    ---

    Read the git log since the last tag, group the commits by type, and
    write the result to CHANGELOG.md following Keep a Changelog.

Skills live in ``./.jaigent/skills`` (project) and ``~/.jaigent/skills``
(personal); the project copy wins on a name clash. Their *descriptions* are
listed in the system prompt so the model knows what exists, and the body is
only pulled in when the model calls the ``load_skill`` tool. That keeps the
prompt small no matter how many skills you have — the same trick Claude Code's
skills use.

Skills are prompt text, not code: loading one can never execute anything.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from jaigent.errors import ToolError
from jaigent.paths import scoped_dirs
from jaigent.tools.base import Tool

SKILLS_DIRNAME = "skills"
FRONT_MATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.S)
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")

#: Refuse absurdly large skills; they would blow up the context window.
MAX_SKILL_BYTES = 100_000


@dataclass(slots=True, frozen=True)
class Skill:
    """One instruction pack."""

    name: str
    description: str
    body: str
    path: Path
    scope: str = "project"

    def summary(self) -> str:
        return f"{self.name}: {self.description}" if self.description else self.name

    def render(self) -> str:
        """The text handed to the model when the skill is loaded."""
        header = f"# Skill: {self.name}"
        if self.description:
            header += f"\n\n{self.description}"
        return f"{header}\n\n---\n\n{self.body.strip()}"


def _parse_front_matter(text: str) -> tuple[dict[str, str], str]:
    """Pull ``key: value`` pairs out of leading ``---`` fences.

    A deliberately tiny parser: skills only need flat string keys, and this
    avoids a YAML dependency for the sake of two fields.
    """
    match = FRONT_MATTER_RE.match(text)
    if not match:
        return {}, text

    meta: dict[str, str] = {}
    for line in match.group(1).splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or ":" not in stripped:
            continue
        key, _, value = stripped.partition(":")
        meta[key.strip().lower()] = value.strip().strip("'\"")
    return meta, match.group(2)


def parse_skill(path: Path, *, scope: str = "project") -> Skill:
    """Read one skill file.

    Raises:
        ToolError: if the file is unreadable, too large, or has no usable body.
    """
    try:
        if path.stat().st_size > MAX_SKILL_BYTES:
            raise ToolError(f"Skill {path.name} is larger than {MAX_SKILL_BYTES:,} bytes")
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ToolError(f"Could not read skill {path}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise ToolError(f"Skill {path.name} is not UTF-8 text") from exc

    meta, body = _parse_front_matter(raw)
    name = (meta.get("name") or path.stem).strip().lower()
    description = meta.get("description", "").strip()

    if not description:
        # Fall back to the first non-heading line so a bare markdown file works.
        for line in body.splitlines():
            candidate = line.strip()
            if candidate and not candidate.startswith("#"):
                description = candidate[:200]
                break

    return Skill(name=name, description=description, body=body, path=path, scope=scope)


def skills_dirs(start: Path | None = None) -> list[tuple[str, Path]]:
    """The directories searched for skills, lowest priority first."""
    return scoped_dirs(SKILLS_DIRNAME, start)


def discover(start: Path | None = None) -> dict[str, Skill]:
    """Find every available skill, project definitions shadowing user ones."""
    found: dict[str, Skill] = {}
    for scope, directory in skills_dirs(start):
        if not directory.is_dir():
            continue
        for file in sorted(directory.glob("*.md")):
            try:
                skill = parse_skill(file, scope=scope)
            except ToolError:
                continue  # a broken skill must not break the whole run
            if NAME_RE.match(skill.name):
                found[skill.name] = skill
    return found


def catalogue(skills: dict[str, Skill]) -> str:
    """The one-line-per-skill listing embedded in the system prompt."""
    if not skills:
        return ""
    lines = [f"- {skill.summary()}" for skill in sorted(skills.values(), key=lambda s: s.name)]
    return "\n".join(lines)


# ----------------------------------------------------------------------
# Tool
# ----------------------------------------------------------------------
def build_skill_tools(skills: dict[str, Skill]) -> list[Tool]:
    """Expose ``load_skill`` when at least one skill exists."""
    if not skills:
        return []

    names = ", ".join(sorted(skills))

    def load_skill(name: str) -> str:
        key = (name or "").strip().lower()
        skill = skills.get(key)
        if skill is None:
            raise ToolError(f"No skill named {name!r}. Available skills: {names}")
        return skill.render()

    return [
        Tool(
            name="load_skill",
            description=(
                "Load the full instructions for a named skill. Skills are saved procedures "
                "for recurring tasks. Call this as soon as the user's request matches one "
                f"of them, then follow the instructions you get back. Available: {names}"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": f"Skill to load. One of: {names}",
                        "enum": sorted(skills),
                    }
                },
                "required": ["name"],
            },
            func=load_skill,
        )
    ]


def create_skill(
    name: str, description: str, body: str, *, scope: str = "project", start: Path | None = None
) -> Path:
    """Write a new skill file and return its path."""
    clean = name.strip().lower().replace(" ", "-")
    if not NAME_RE.match(clean):
        raise ToolError(
            f"Invalid skill name {name!r}. Use lowercase letters, digits, dots, dashes "
            "or underscores, starting with a letter or digit."
        )

    directory = dict(skills_dirs(start))[scope]
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{clean}.md"

    content = f"---\nname: {clean}\ndescription: {description.strip()}\n---\n\n{body.strip()}\n"
    path.write_text(content, encoding="utf-8")
    return path
