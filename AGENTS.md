# AGENTS.md

Instructions for AI coding agents working in this repository. Human contributors should read [CONTRIBUTING.md](CONTRIBUTING.md); everything here applies to you as well.

## What this project is

jaigent is a CLI and Python library: an LLM agent that can search the web and manipulate local files. Users bring their own API key. The project's selling points are that it is **small enough to read**, **safe by default**, and **easy to extend**. Every change should protect those three properties.

## Setup

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

## Checks — run these before you claim to be done

```bash
pytest                 # must pass, no skips introduced
ruff check .           # must be clean
ruff format .          # apply, don't just check
mypy                   # must be clean
```

All four are required. The test suite is offline and needs no API key; if a test of yours needs the network, it is the wrong test.

## Repository layout

```
src/jaigent/
├── __init__.py     # public API — update __all__ when you export something
├── agent.py        # the tool-calling loop
├── approval.py     # diff previews and the ask/auto/dry-run policy
├── branding.py     # the logo: glyphs, colours, responsive sizing
├── cli.py          # argparse + rich rendering
├── config.py       # Settings, env vars, .env loader
├── pricing.py      # token accounting and the price table
├── session.py      # saving and resuming conversations
├── prompts.py      # system prompt
├── errors.py       # exception hierarchy
├── llm/
│   ├── base.py     # LLMProvider ABC, AssistantMessage, ToolCall
│   ├── openai.py   # OpenAI-compatible chat completions
│   └── anthropic.py
└── tools/
    ├── base.py     # Tool, ToolRegistry
    ├── sandbox.py  # workspace confinement — treat as security-critical
    ├── files.py
    ├── web.py
    └── shell.py    # opt-in, dangerous
tests/              # mirrors src/, one test module per source module
examples/           # runnable demos, including a mock LLM server
```

## Conventions

**Colour.** Never hard-code a colour. Import `ACCENT`, `ACCENT_DIM`, `INK` or `MUTED`
from `branding.py` so the palette stays consistent and themeable in one place.

**Approval.** Any new tool that changes the filesystem or the machine must be added to
`MUTATING_TOOLS` in `approval.py` and given a case in `preview()` so the user sees what
is about to happen. A tool that mutates without a preview is a bug.

**Prompts with brackets.** Rich parses `[y]` as markup. When a prompt or message
contains literal square brackets, pass a `rich.text.Text` instead of a markup string —
see `Approver._read_answer`.

**Branding.** The logo lives in `branding.py` as per-letter glyph blocks, never as flat
strings — that is what keeps the accent on the `ai` in j-**ai**-gent and lets the width be
computed. If you touch the glyphs, keep every letter rectangular and all letters the same
height; `tests/test_branding.py` asserts both, plus that the logo never overflows the
terminal at any width. Anything user-facing must degrade correctly under `--no-color`.

**Style.** Python 3.10+, 100-column lines, `from __future__ import annotations` at the top of every module, type hints on all public functions, imperative-mood docstrings on public APIs. Ruff enforces the rest; don't hand-format.

**Errors.** Everything raised on purpose inherits from `JaigentError`:

- `ConfigurationError` — bad or missing settings. The message must tell the user exactly which variable to set.
- `ProviderError` — the LLM call failed. Include the HTTP status and a hint when the cause is known.
- `ToolError` — a tool failed in a way the *model* should read and recover from.
- `SandboxViolation` — a path escaped the workspace.

Never let a tool crash a run: `ToolRegistry.call` converts every exception into an `ERROR: …` string that goes back to the model. Preserve that contract.

**Error messages are prompts.** A tool's error text is consumed by an LLM that will try again. Say what was wrong *and* what to do instead ("old_text appears 3 times; include more surrounding context, or pass count=-1"). Don't just say "invalid input".

## Security rules — non-negotiable

1. **Every filesystem path goes through `resolve_in_workspace()`.** No exceptions. If you add a tool that touches a path, it calls that function before doing anything else.
2. **Never widen the sandbox** to make a feature work. If a feature seems to need it, that is a design discussion, not a patch.
3. **New dangerous capabilities are opt-in**, gated behind a `Settings` flag and marked `dangerous=True` on the `Tool`, exactly like `run_command`.
4. **Never print, log or persist an API key.** Use `Settings.redacted()` for any output that includes configuration.
5. **Don't add a hard dependency lightly.** Runtime deps are `httpx` and `rich`, and that is close to the ceiling.

Changes to `tools/sandbox.py` require accompanying tests covering traversal, absolute paths and symlink escapes.

## Adding a tool

1. Implement the function in the right `tools/` module, taking `workspace: Path` first if it touches files.
2. Add a `Tool(...)` descriptor in that module's `build_*_tools()` factory.
3. Write the description **for the model**: when to use it, not just what it does. Give every parameter a `description`, and state defaults in the text.
4. Register it in `build_default_registry()` if it should be on by default.
5. Test the happy path, each failure mode, and the sandbox boundary if applicable.

`tests/test_tools_base.py` asserts that every default tool has a non-empty description and that every parameter is documented — that test will fail if you skip step 3.

## Adding a provider

Subclass `LLMProvider` in `src/jaigent/llm/`, implement `complete`, `format_assistant_message` and `format_tool_result`, register it in `PROVIDERS`, add its defaults to `DEFAULT_MODELS` / `DEFAULT_BASE_URLS` / `API_KEY_ENV_VARS` in `config.py`, and add it to `KNOWN_PROVIDERS`. Test it with mocked HTTP following the pattern in `tests/test_llm.py`; map at least 401/404/429 to actionable messages.

## Testing expectations

- One test module per source module, mirroring the layout.
- Never call a real network or a real API. Mock `httpx` (see `_patch_client` in `tests/test_tools_web.py`) or use the `FakeProvider` in `tests/conftest.py`.
- Use the `workspace` and `settings` fixtures instead of building `tmp_path` layouts by hand; use `clean_env` whenever a test reads configuration.
- Test names describe behaviour: `test_symlink_pointing_outside_is_rejected`, not `test_sandbox_2`.
- New code comes with tests. Coverage is ~89%; don't lower it.

## Documentation

If you change behaviour a user can observe, update the docs in the same change:

- new/changed CLI flag or command → README "Usage" and "Configuration"
- new model pricing → `DEFAULT_PRICES` in `pricing.py`
- new tool → README "Tools" table
- new setting → README "Configuration" table **and** `.env.example`
- anything notable → `CHANGELOG.md` under "Unreleased"

Docs are written in English. Keep the README's tone: short sentences, real commands, no marketing.

## Git

Work on the branch you were given; never force-push a shared branch. Use [Conventional Commits](https://www.conventionalcommits.org/) — `feat:`, `fix:`, `docs:`, `test:`, `refactor:`, `chore:` — with a subject in the imperative mood under ~72 characters. Keep a commit to one logical change.

## Things not to do

- Don't commit secrets, `.env` files, or recorded API responses containing keys.
- Don't add telemetry, analytics, or any phone-home behaviour. Ever.
- Don't bundle a default API key or route requests through a proxy — users talk to their provider directly.
- Don't reformat files you didn't otherwise change; it buries the real diff.
- Don't add a web UI, a plugin system, or an async rewrite without discussing it in an issue first. Small and readable is the point.
- Don't leave `print()` debugging in the source; the CLI renders through `rich`, and traces go to stderr behind `--verbose`.
