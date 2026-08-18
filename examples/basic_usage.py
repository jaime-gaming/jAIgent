"""Using jaigent from Python.

Set an API key first::

    export OPENAI_API_KEY='sk-...'
    python examples/basic_usage.py
"""

from __future__ import annotations

import sys
from pathlib import Path

from jaigent import (
    Agent,
    Approver,
    ConfigurationError,
    Mode,
    Settings,
    Tool,
    build_default_registry,
)


def simple() -> None:
    """The shortest thing that works."""
    agent = Agent(Settings.from_env())
    print(agent.chat("List the files here and tell me what this project does."))


def inspect_the_trace() -> None:
    """`run()` returns the answer plus everything the agent did to get it."""
    agent = Agent(Settings.from_env())
    result = agent.run("Find the newest file in this folder and summarise it.")

    print(result.output)
    print(f"\n{result.tool_calls} tool call(s):")
    for step in result.steps:
        print(f"  {step.step}. {step.tool}({step.arguments}) -> {step.duration:.2f}s")
    if result.usage:
        print(f"tokens: {result.usage}")


def explicit_settings() -> None:
    """Configure everything in code instead of the environment."""
    agent = Agent(
        Settings(
            provider="openai",
            model="gpt-4o-mini",
            api_key="sk-...",  # or leave it out and use the environment
            workspace=Path("/tmp/jaigent-demo"),
            max_steps=20,
            verbose=True,
        )
    )
    print(agent.chat("Create a hello.txt with a greeting."))


def custom_tool() -> None:
    """Add your own capability to the default toolset."""

    def word_count(path: str) -> str:
        return f"{len(Path(path).read_text(encoding='utf-8').split())} words"

    settings = Settings.from_env()
    registry = build_default_registry(settings)
    registry.register(
        Tool(
            name="word_count",
            description="Count the words in a text file. Use when asked about document length.",
            parameters={
                "type": "object",
                "properties": {"path": {"type": "string", "description": "File to measure."}},
                "required": ["path"],
            },
            func=word_count,
        )
    )

    agent = Agent(settings, tools=registry)
    print(agent.chat("How many words are in README.md?"))


def observed_and_steered() -> None:
    """Extra instructions plus a callback fired after each tool call."""
    agent = Agent(
        Settings.from_env(),
        instructions="Always cite sources. Prefer primary documentation over blog posts.",
        on_tool_call=lambda name, args, out: print(f"  [{name}] {str(args)[:60]}"),
    )
    print(agent.chat("What is the latest stable Python release?"))


def streaming() -> None:
    """Print the answer token by token as it is generated."""
    agent = Agent(
        Settings.from_env(),
        on_text=lambda chunk: print(chunk, end="", flush=True),
    )
    result = agent.run("Explain what this project does in two sentences.")
    print(f"\n\n{result.cost.summary()}")


def read_only() -> None:
    """Let the agent look but never touch, by refusing every mutating tool."""
    agent = Agent(Settings.from_env(), approver=Approver(Mode.DRY_RUN))
    print(agent.chat("Tidy up the README — tell me what you would change."))


EXAMPLES = {
    "simple": simple,
    "streaming": streaming,
    "read-only": read_only,
    "trace": inspect_the_trace,
    "explicit": explicit_settings,
    "custom-tool": custom_tool,
    "observed": observed_and_steered,
}


if __name__ == "__main__":
    name = sys.argv[1] if len(sys.argv) > 1 else "simple"
    if name not in EXAMPLES:
        print(f"Usage: python examples/basic_usage.py [{' | '.join(EXAMPLES)}]")
        raise SystemExit(2)

    try:
        EXAMPLES[name]()
    except ConfigurationError as exc:
        print(f"Configuration error: {exc}")
        raise SystemExit(78) from exc
