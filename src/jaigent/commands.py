"""Custom slash commands.

A command is a markdown file whose body is a prompt template. Dropping
``.jaigent/commands/review.md`` into a project gives everyone on the team
``/review`` in chat and ``jaigent /review`` on the command line.

::

    ---
    description: Review the working tree for bugs.
    ---

    Run `git diff`, then review the changes in $ARGUMENTS for correctness
    issues first, style second. Be specific about file and line.

Placeholders expanded before the prompt reaches the model:

``$ARGUMENTS``
    Everything the user typed after the command name.
``$1``, ``$2``, …
    Individual whitespace-separated arguments.
``$WORKSPACE``
    Absolute path of the current workspace.

Commands are prompt text, never code: running one can only ever send a message
to the model, using the same sandboxed tools as any other turn.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from jaigent.errors import ToolError
from jaigent.paths import scoped_dirs

COMMANDS_DIRNAME = "commands"
FRONT_MATTER_RE = re.compile(r"\A---\s*\n(.*?)\n---\s*\n?(.*)\Z", re.S)
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9._-]*$")
POSITIONAL_RE = re.compile(r"\$(\d+)")

MAX_COMMAND_BYTES = 50_000

#: Names that would shadow a built-in chat command.
RESERVED = frozenset(
    {
        "help",
        "reset",
        "tools",
        "model",
        "provider",
        "workspace",
        "cost",
        "save",
        "undo",
        "revert",
        "checkpoints",
        "rewind",
        "diff",
        "status",
        "approve",
        "commands",
        "doctor",
        "compact",
        "memory",
        "exit",
        "quit",
    }
)


@dataclass(slots=True, frozen=True)
class Command:
    """One prompt template."""

    name: str
    description: str
    template: str
    path: Path
    scope: str = "project"

    def render(self, arguments: str = "", *, workspace: str = "") -> str:
        """Expand the template's placeholders.

        Unfilled ``$1``-style placeholders collapse to an empty string rather
        than being left as literals, so a command with optional arguments reads
        cleanly when they are omitted.
        """
        parts = arguments.split()
        text = self.template

        text = POSITIONAL_RE.sub(
            lambda m: parts[int(m.group(1)) - 1] if 0 < int(m.group(1)) <= len(parts) else "",
            text,
        )
        text = text.replace("$ARGUMENTS", arguments.strip())
        text = text.replace("$WORKSPACE", workspace or str(Path.cwd()))
        return text.strip()

    def summary(self) -> str:
        return f"/{self.name}: {self.description}" if self.description else f"/{self.name}"


def _parse_front_matter(text: str) -> tuple[dict[str, str], str]:
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


def parse_command(path: Path, *, scope: str = "project") -> Command:
    """Read one command file."""
    try:
        if path.stat().st_size > MAX_COMMAND_BYTES:
            raise ToolError(f"Command {path.name} is larger than {MAX_COMMAND_BYTES:,} bytes")
        raw = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ToolError(f"Could not read command {path}: {exc}") from exc
    except UnicodeDecodeError as exc:
        raise ToolError(f"Command {path.name} is not UTF-8 text") from exc

    meta, body = _parse_front_matter(raw)
    name = (meta.get("name") or path.stem).strip().lower()
    description = meta.get("description", "").strip()

    if not description:
        for line in body.splitlines():
            candidate = line.strip()
            if candidate and not candidate.startswith("#"):
                description = candidate[:150]
                break

    return Command(
        name=name, description=description, template=body.strip(), path=path, scope=scope
    )


def commands_dirs(start: Path | None = None) -> list[tuple[str, Path]]:
    """Directories searched for commands, lowest priority first."""
    return scoped_dirs(COMMANDS_DIRNAME, start)


def discover(start: Path | None = None) -> dict[str, Command]:
    """Find every custom command, project definitions shadowing user ones."""
    found: dict[str, Command] = {}
    for scope, directory in commands_dirs(start):
        if not directory.is_dir():
            continue
        for file in sorted(directory.glob("*.md")):
            try:
                command = parse_command(file, scope=scope)
            except ToolError:
                continue  # one broken file must not hide the rest
            if NAME_RE.match(command.name) and command.name not in RESERVED:
                found[command.name] = command
    return found


def create_command(
    name: str,
    description: str,
    template: str,
    *,
    scope: str = "project",
    start: Path | None = None,
) -> Path:
    """Write a new command file and return its path."""
    clean = name.strip().lstrip("/").lower().replace(" ", "-")
    if not NAME_RE.match(clean):
        raise ToolError(
            f"Invalid command name {name!r}. Use lowercase letters, digits, dots, "
            "dashes or underscores, starting with a letter or digit."
        )
    if clean in RESERVED:
        raise ToolError(
            f"/{clean} is a built-in chat command. Pick another name so it is not shadowed."
        )

    directory = dict(commands_dirs(start))[scope]
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{clean}.md"

    content = f"---\nname: {clean}\ndescription: {description.strip()}\n---\n\n{template.strip()}\n"
    path.write_text(content, encoding="utf-8")
    return path


def resolve(text: str, start: Path | None = None) -> tuple[Command, str] | None:
    """Match ``/name rest of the line`` against the available commands.

    Returns the command and its arguments, or ``None`` if nothing matches.
    """
    stripped = (text or "").strip()
    if not stripped.startswith("/"):
        return None

    head, _, rest = stripped[1:].partition(" ")
    command = discover(start).get(head.strip().lower())
    return (command, rest.strip()) if command else None
